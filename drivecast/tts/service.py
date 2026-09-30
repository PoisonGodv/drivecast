"""TTS 编排(docs/03 §1/§4/§6):脚本 → 友好化 → 分段 → 逐 chunk 合成 → 后处理 → meta。

M6 在不朗读段名的前提下保留稳定五段来源，写章节/ID3/封面，使用 ffprobe 回填真实
时长，并把语速校准与自动质量检查持久化。断点续合成的最小单元仍是单个 chunk。
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
from pathlib import Path

from ..config import ensure_data_dirs, settings
from . import doubao, edge, friendly
from .chunk import chunk_text
from .errors import FFmpegTimeoutError, TTSProviderError, brief

SILENCE_ROLE_SWITCH_MS = 250
AUDIO_PIPELINE_VERSION = 7
SECTION_KEYS = ("S0", "S1", "S2", "S3", "S4")
OUTRO_KEY = "S5"  # 片尾参考文献口播段(scriptwriter 从已校验链接确定性拼接,不过 LLM)
CHAPTER_KEYS = SECTION_KEYS + (OUTRO_KEY,)
# 旧脚本没有结构字段时，用固定内容占比映射到五章；只切元数据，不往口播补段名。
LEGACY_SECTION_WEIGHTS = (0.07, 0.34, 0.27, 0.20, 0.12)


SPEAKERS = ("expert", "host")
PROVIDERS = {"edge": edge, "doubao": doubao}
SOURCES_FILE = "sources.json"  # chunk 目录内:每个 chunk 实际由哪个通道/音色产出


def _provider_name(name: str | None = None) -> str:
    return "doubao" if (name if name is not None else settings.tts_provider) == "doubao" else "edge"


def _fallback_name(primary: str) -> str | None:
    name = (settings.tts_fallback_provider or "").strip().lower()
    if name not in PROVIDERS or name == primary:
        return None
    return name


def _voice_rate(provider: str | None = None) -> dict[str, tuple]:
    """speaker → (voice, rate);合成时按当前配置解析,tts.json 不固化音色(docs/03 §3.5)。"""
    if _provider_name(provider) == "doubao":
        return {
            "expert": (settings.tts_doubao_voice_expert, settings.tts_doubao_rate_expert),
            "host": (settings.tts_doubao_voice_host, settings.tts_doubao_rate_host),
        }
    return {
        "expert": (settings.tts_voice_expert, settings.tts_rate_expert),
        "host": (settings.tts_voice_host, settings.tts_rate_host),
    }


def _speaker(chunk: dict) -> str:
    speaker = chunk.get("speaker", "expert")
    return speaker if speaker in SPEAKERS else "expert"


def _signature(provider: str, speaker: str) -> dict:
    voice, rate = _voice_rate(provider)[speaker]
    sig = {"provider": provider, "voice": voice, "rate": rate}
    if provider == "edge":
        sig["pitch"] = settings.tts_pitch
    return sig


def _load_sources(ep_id: str) -> dict[str, dict]:
    try:
        data = json.loads((settings.chunks_dir(ep_id) / SOURCES_FILE).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_sources(ep_id: str, sources: dict[str, dict]) -> None:
    path = settings.chunks_dir(ep_id) / SOURCES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".json.part")
    part.write_text(json.dumps(sources, ensure_ascii=False, indent=1), "utf-8")
    part.replace(path)


def _chunk_matches(chunk: dict, recorded: dict | None, wanted: dict) -> bool:
    if recorded is not None:
        return recorded == wanted
    # 旧版 tts.json 在 chunk 上固化了 voice/rate 且无来源记录:音色语速都一致才视为同源
    return chunk.get("voice") == wanted["voice"] and chunk.get("rate") == wanted["rate"]


def _voices_label(ep_id: str, chunks: list[dict]) -> str:
    sources = _load_sources(ep_id)
    voices = {(sources.get(str(c.get("seq"))) or {}).get("voice") or c.get("voice") or ""
              for c in chunks}
    return "+".join(sorted(v for v in voices if v))


def _provider_summary(ep_id: str, chunks: list[dict]) -> dict:
    sources = _load_sources(ep_id)
    rows = [sources.get(str(c.get("seq"))) or {} for c in chunks]
    providers = sorted({r.get("provider") for r in rows if r.get("provider")})
    voices: dict[str, str] = {}
    for chunk, row in zip(chunks, rows):
        if row.get("voice"):
            voices.setdefault(_speaker(chunk), row["voice"])
    return {"provider": "+".join(providers) or None, "voices": voices}


async def _run(cmd: list[str], *, timeout: float | None = None) -> tuple[int, str]:
    limit = timeout or settings.ffmpeg_timeout_sec
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=limit)
        return proc.returncode, (stdout + stderr).decode("utf-8", "ignore")
    except asyncio.TimeoutError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise FFmpegTimeoutError(
            f"{Path(cmd[0]).name} 超过 {limit:g} 秒未完成,已强制终止;可调大 FFMPEG_TIMEOUT_SEC") from None
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        raise


async def _silence_file(ms: int) -> Path:
    """段间停顿静音文件(按时长生成一次复用)。"""
    path = settings.data_dir / "audio" / f"_silence_{ms}ms.mp3"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        rc, err = await _run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", f"{ms / 1000:.3f}", "-q:a", "9", str(path),
        ])
        if rc != 0:
            raise RuntimeError(f"生成静音文件失败: {err[-300:]}")
    return path


def _weighted_groups(units: list, lengths: list[int]) -> list[list]:
    """按五段目标占比切连续单元；单元数足够时保证每章至少一个，边界确定且可复现。"""
    if len(units) < len(SECTION_KEYS):
        groups = [[unit] for unit in units]
        return groups + [[] for _ in range(len(SECTION_KEYS) - len(groups))]
    prefix = [0]
    for size in lengths:
        prefix.append(prefix[-1] + size)
    total = max(prefix[-1], 1)
    cuts = [0]
    cumulative = 0.0
    for section_index, weight in enumerate(LEGACY_SECTION_WEIGHTS[:-1], 1):
        cumulative += weight
        minimum = cuts[-1] + 1
        maximum = len(units) - (len(SECTION_KEYS) - section_index)
        target = total * cumulative
        cut = min(range(minimum, maximum + 1), key=lambda index: abs(prefix[index] - target))
        cuts.append(cut)
    cuts.append(len(units))
    return [units[start:end] for start, end in zip(cuts, cuts[1:])]


def _section_specs(script: dict) -> list[dict]:
    """返回不含朗读标签的五段内容 + 可选片尾参考资料段(S5);兼容 M6 前脚本并保持确定性。"""
    titles = settings.chapter_title_list
    raw_sections = script.get("sections") or []
    if raw_sections and tuple(s.get("key") for s in raw_sections) == SECTION_KEYS:
        specs = [
            {
                "key": key,
                "title": (section.get("title") or titles[index]).strip(),
                "content": section.get("content", ""),
                "turns": section.get("turns") or [],
            }
            for index, (key, section) in enumerate(zip(SECTION_KEYS, raw_sections))
        ]
        return specs + _outro_spec(script)

    dialogue = script.get("script_format") == "dialogue" and script.get("turns")
    units = list(script.get("turns") or []) if dialogue else [
        p.strip() for p in re.split(r"\n\s*\n", script.get("content", "")) if p.strip()
    ]
    if not units:
        raise ValueError("脚本没有可合成内容")

    lengths = [len(u.get("text", "")) if isinstance(u, dict) else len(u) for u in units]
    groups = _weighted_groups(units, lengths)

    specs = []
    for index, (key, group) in enumerate(zip(SECTION_KEYS, groups)):
        specs.append({
            "key": key,
            "title": titles[index],
            "turns": group if dialogue else [],
            "content": "\n\n".join(group) if not dialogue else "",
        })
    return specs + _outro_spec(script)


def _outro_spec(script: dict) -> list[dict]:
    """片尾参考文献口播段:独白式走 expert 正文,对话式由 host 收尾念出。"""
    outro = (script.get("refs_outro") or "").strip()
    if not outro:
        return []
    spec = {"key": OUTRO_KEY, "title": settings.chapter_title_refs, "content": "", "turns": []}
    if script.get("script_format") == "dialogue":
        spec["turns"] = [{"speaker": "host", "text": outro}]
    else:
        spec["content"] = outro
    return [spec]


def build_tts_json(script: dict) -> list[dict]:
    """脚本 → friendly → chunk，并把每个 chunk 绑定到稳定五段章节源。"""
    ep_id = script["episode_id"]
    chunks: list[dict] = []
    section_rows: list[dict] = []

    def add(section_key: str, section_title: str, speaker: str, text: str) -> None:
        for part in chunk_text(friendly.friendly(text)):
            chunks.append({
                "seq": len(chunks) + 1,
                "speaker": speaker,
                "text": part,
                "section_key": section_key,
                "section_title": section_title,
            })

    for section in _section_specs(script):
        first_seq = len(chunks) + 1
        if section["turns"]:
            for turn in section["turns"]:
                speaker = turn.get("speaker", "expert")
                add(section["key"], section["title"], speaker if speaker in SPEAKERS else "expert",
                    turn.get("text", ""))
        elif section["content"].strip():
            add(section["key"], section["title"], "expert", section["content"])
        section_rows.append({
            "key": section["key"],
            "title": section["title"],
            "first_seq": first_seq if len(chunks) >= first_seq else None,
            "last_seq": len(chunks) if len(chunks) >= first_seq else None,
        })

    if not chunks:
        raise ValueError("脚本友好化后没有可合成 chunk")
    ensure_data_dirs()
    settings.tts_path(ep_id).write_text(json.dumps({
        "episode_id": ep_id,
        "sections": section_rows,
        "chunks": chunks,
    }, ensure_ascii=False, indent=2), "utf-8")
    return chunks


def _upgrade_legacy_tts(script: dict, payload: dict) -> list[dict]:
    """给旧 tts.json 补章节归属，不重切文本/不改 seq，因此既有 chunk 可原样复用。"""
    chunks = payload.get("chunks") or []
    if not chunks or all(c.get("section_key") in CHAPTER_KEYS for c in chunks):
        return chunks
    groups = _weighted_groups(chunks, [len(c.get("text", "")) for c in chunks])
    titles = settings.chapter_title_list
    for section_index, group in enumerate(groups):
        for chunk in group:
            chunk["section_key"] = SECTION_KEYS[section_index]
            chunk["section_title"] = titles[section_index]
    payload["sections"] = _section_rows(chunks)
    settings.tts_path(script["episode_id"]).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
    )
    return chunks


def _section_rows(chunks: list[dict]) -> list[dict]:
    rows = []
    for index, key in enumerate(SECTION_KEYS):
        members = [c for c in chunks if c.get("section_key") == key]
        rows.append({
            "key": key,
            "title": members[0].get("section_title") if members else settings.chapter_title_list[index],
            "first_seq": members[0]["seq"] if members else None,
            "last_seq": members[-1]["seq"] if members else None,
        })
    return rows


async def _synth_missing(
    ep_id: str, chunks: list[dict], *, local_only: bool = False, provider: str | None = None,
) -> list[Path]:
    """只合成缺失或来源不符的 chunk(断点续核心)，返回有序路径。

    来源不符 = 该 chunk 由别的通道/音色/语速产出，一律按当前通道重合成，保证整集同一音色；
    local_only(本地升级)只复用既有块，不做来源校验也不请求 TTS。
    """
    name = _provider_name(provider)
    module = PROVIDERS[name]
    chunks_dir = settings.chunks_dir(ep_id)
    paths = [chunks_dir / f"{c['seq']:03d}.mp3" for c in chunks]
    cached_durations = await _chunk_durations(paths)
    sources = _load_sources(ep_id)
    missing = []
    adopted = False
    for chunk, path, duration in zip(chunks, paths, cached_durations):
        key = str(chunk["seq"])
        wanted = _signature(name, _speaker(chunk))
        if duration > 0 and (local_only or _chunk_matches(chunk, sources.get(key), wanted)):
            if not local_only and key not in sources:
                sources[key] = wanted
                adopted = True
            continue
        missing.append((chunk, path, wanted))
    if adopted:
        _save_sources(ep_id, sources)
    if missing and local_only:
        raise RuntimeError("本地升级缺少有效音频块，未调用 TTS")
    if missing:
        print(f"[tts] {ep_id}: {len(chunks)} 段,通道 {name},需合成 {len(missing)} 段")
    for index, (chunk, path, wanted) in enumerate(missing, 1):
        print(f"[tts] 合成 {chunk['seq']:03d}/{len(chunks)} ({len(chunk['text'])}字) "
              f"[{index}/{len(missing)}]")
        await module.synth_chunk(chunk["text"], wanted["voice"], wanted["rate"], path)
        if await _probe_duration(path) <= 0:
            path.unlink(missing_ok=True)
            raise RuntimeError(f"chunk 无法探测时长:{chunk['seq']}")
        sources[str(chunk["seq"])] = wanted
        _save_sources(ep_id, sources)
        await module.jitter_sleep()
    return paths


class TTSFallbackFailed(TTSProviderError):
    """主备通道都失败;两边都不可重试时 job 也不再重试。"""

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


async def _synth_with_fallback(ep_id: str, chunks: list[dict]) -> tuple[list[Path], dict]:
    """主通道失败(不可重试或重试耗尽)且配了 TTS_FALLBACK_PROVIDER 时，整集改用备用通道重合成。"""
    primary = _provider_name()
    try:
        return await _synth_missing(ep_id, chunks, provider=primary), {"provider": primary}
    except TTSProviderError as exc:
        fallback = _fallback_name(primary)
        if fallback is None:
            raise
        primary_error = exc
    print(f"[tts] {ep_id}: 主通道 {primary} 失败({brief(primary_error)}),整集改用 {fallback} 重合成")
    try:
        paths = await _synth_missing(ep_id, chunks, provider=fallback)
    except TTSProviderError as exc:
        raise TTSFallbackFailed(
            f"主通道 {primary} 失败:{brief(primary_error, 120)};备用通道 {fallback} 也失败:{brief(exc, 120)}",
            retryable=primary_error.retryable or exc.retryable,
        ) from exc
    return paths, {"provider": fallback, "fallback_from": primary,
                   "fallback_reason": brief(primary_error, 300)}


async def _probe_duration(path: Path) -> float:
    from ..concurrency import request_limit
    if not path.is_file() or path.stat().st_size == 0:
        return 0.0
    async with request_limit("probe"):
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise
        if proc.returncode:
            return 0.0
        try:
            value = float(json.loads(stdout.decode())["format"]["duration"])
            from .quality import positive
            return value if positive(value) else 0.0
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return 0.0


async def _chunk_durations(paths: list[Path]) -> list[float]:
    from ..concurrency import gather_cancel
    return list(await gather_cancel(*(_probe_duration(path) for path in paths)))


def _gap_ms(chunks: list[dict], index: int) -> int:
    if index >= len(chunks) - 1:
        return 0
    return (SILENCE_ROLE_SWITCH_MS if chunks[index + 1]["speaker"] != chunks[index]["speaker"]
            else settings.silence_between_chunks_ms)


def _chapter_fallback_title(key: str, index: int) -> str:
    if key == OUTRO_KEY:
        return settings.chapter_title_refs
    return settings.chapter_title_list[min(index, len(SECTION_KEYS) - 1)]


def _chapters(chunks: list[dict], durations: list[float]) -> list[dict]:
    """按 chunk 实测时长和插入静音累加时间戳，空段并入下一章而非造零长章节。"""
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    cursor = 0.0
    for index, (chunk, duration) in enumerate(zip(chunks, durations)):
        key = chunk.get("section_key", "S1")
        starts.setdefault(key, cursor)
        cursor += duration + _gap_ms(chunks, index) / 1000
        ends[key] = cursor
    chapters = []
    for index, key in enumerate(CHAPTER_KEYS):
        if key not in starts:
            continue
        chapters.append({
            "key": key,
            "title": next((c.get("section_title") for c in chunks if c.get("section_key") == key),
                          _chapter_fallback_title(key, index)),
            "start_sec": round(starts[key], 3),
            "end_sec": round(ends[key], 3),
        })
    return chapters


def _ffmetadata(chapters: list[dict], path: Path) -> None:
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;")

    lines = [";FFMETADATA1"]
    for chapter in chapters:
        lines.extend([
            "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={round(chapter['start_sec'] * 1000)}",
            f"END={max(round(chapter['end_sec'] * 1000), round(chapter['start_sec'] * 1000) + 1)}",
            f"title={escape(chapter['title'])}",
        ])
    path.write_text("\n".join(lines) + "\n", "utf-8")


async def _concat(
    chunks: list[dict], paths: list[Path], out: Path, *, title: str, album: str, track: int,
    chapters: list[dict],
) -> None:
    """插静音→concat→两趟 loudnorm→章节/ID3/封面。"""
    silence_same = await _silence_file(settings.silence_between_chunks_ms)
    silence_switch = await _silence_file(SILENCE_ROLE_SWITCH_MS)
    list_file = out.with_suffix(".concat.txt")
    metadata_file = out.with_suffix(".ffmetadata.txt")
    joined = out.with_suffix(".joined.mp3")
    lines: list[str] = []
    for index, path in enumerate(paths):
        lines.append(f"file '{path}'")
        if index < len(paths) - 1:
            gap = silence_switch if _gap_ms(chunks, index) == SILENCE_ROLE_SWITCH_MS else silence_same
            lines.append(f"file '{gap}'")
    list_file.write_text("\n".join(lines), "utf-8")
    _ffmetadata(chapters, metadata_file)

    try:
        rc, err = await _run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(joined),
        ])
        if rc != 0:
            raise RuntimeError(f"concat 失败: {err[-300:]}")

        af = (f"loudnorm=I={settings.loudness_i}:TP={settings.loudness_tp}:"
              f"LRA={settings.loudness_lra}")
        rc, err = await _run([
            "ffmpeg", "-y", "-i", str(joined), "-af", af + ":print_format=json",
            "-f", "null", "-",
        ])
        if rc != 0:
            raise RuntimeError(f"loudnorm 测量失败: {err[-300:]}")
        match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.DOTALL)
        measured = json.loads(match.group(0)) if match else {}
        if measured:
            af2 = (f"{af}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
                   f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
                   f":offset={measured['target_offset']}:linear=true")
        else:
            af2 = af
            print("[tts] loudnorm 测量值解析失败,退化为单趟动态归一")

        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-i", str(joined), "-f", "ffmetadata", "-i", str(metadata_file),
        ]
        cover = settings.audio_cover_path
        has_cover = cover.exists() and cover.stat().st_size > 0
        if has_cover:
            cmd.extend(["-i", str(cover)])
        cmd.extend([
            "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1",
            "-af", af2, "-c:a", "libmp3lame", "-q:a", "4",
        ])
        if has_cover:
            cmd.extend([
                "-map", "2:v:0", "-c:v", "png", "-disposition:v:0", "attached_pic",
                "-metadata:s:v", "title=Cover", "-metadata:s:v", "comment=Cover (front)",
            ])
        cmd.extend([
            "-metadata", f"title={title}", "-metadata", f"artist={settings.audio_artist}",
            "-metadata", f"album={album or 'DriveCast'}", "-metadata", f"track={track}", str(out),
        ])
        rc, err = await _run(cmd)
        if rc != 0:
            raise RuntimeError(f"loudnorm/元数据应用失败: {err[-500:]}")
    finally:
        list_file.unlink(missing_ok=True)
        metadata_file.unlink(missing_ok=True)
        joined.unlink(missing_ok=True)


async def _peak_db(path: Path) -> float | None:
    rc, output = await _run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", "volumedetect", "-f", "null", "-",
    ])
    if rc != 0:
        return None
    match = re.search(r"max_volume:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dB", output, re.IGNORECASE)
    if not match or match.group(1).lower() == "-inf":
        return None
    return float(match.group(1))


def _chunk_consistency(chunks: list[dict], paths: list[Path], durations: list[float]) -> dict:
    existing = sum(path.exists() and path.stat().st_size > 0 for path in paths)
    valid_duration = sum(duration > 0 for duration in durations)
    pairs = [(chunk["seq"], len(chunk.get("text", "")) / duration)
             for chunk, duration in zip(chunks, durations)
             if duration > 0 and len(chunk.get("text", "")) > 0]
    rates = [rate for _, rate in pairs]
    outliers: list[int] = []
    if len(rates) >= 4:
        median = statistics.median(rates)
        deviations = [abs(rate - median) for rate in rates]
        mad = statistics.median(deviations)
        if mad > 0:
            outliers = [seq for seq, rate in pairs
                        if abs(rate - median) / mad > 6]
    return {
        "expected": len(chunks),
        "files_present": existing,
        "durations_probed": valid_duration,
        "duration_outlier_seqs": outliers,
        "passed": existing == len(chunks) == valid_duration and not outliers,
    }


def _quality_report(
    *, duration: float, estimated_duration_sec: float | None, peak_db: float | None,
    chunks: list[dict], paths: list[Path], durations: list[float],
    target_minutes: float | None = None,
) -> dict:
    from .quality import report
    return report(duration, estimated_duration_sec, target_minutes, peak_db,
                  _chunk_consistency(chunks, paths, durations))


def _history_wpm(exclude_episode_id: str) -> list[float]:
    samples: list[tuple[float, float]] = []
    for path in settings.data_dir.joinpath("meta").glob("*.json"):
        if path.stem == exclude_episode_id:
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
            value = data.get("speech_rate_calibration", {}).get("observed_words_per_minute",
                                                                  data.get("words_per_min"))
            if isinstance(value, (int, float)) and value > 0:
                samples.append((path.stat().st_mtime, float(value)))
        except (OSError, json.JSONDecodeError):
            continue
    samples.sort(key=lambda row: row[0])
    return [value for _, value in samples[-settings.speech_rate_history_window:]]


def _calibration(ep_id: str, actual_wpm: float) -> dict:
    samples = _history_wpm(ep_id)
    samples.append(actual_wpm)
    samples = samples[-settings.speech_rate_history_window:]
    recommended = statistics.median(samples) if len(samples) >= settings.speech_rate_min_samples else settings.words_per_minute
    return {
        "observed_words_per_minute": round(actual_wpm, 1),
        "history_samples": len(samples),
        "history_window": settings.speech_rate_history_window,
        "recommended_words_per_minute": round(recommended, 1),
        "coefficient_vs_config": round(recommended / settings.words_per_minute, 4) if settings.words_per_minute else 1.0,
        "config_words_per_minute": settings.words_per_minute,
        "env_mutated": False,
    }


def _requested_minutes(script: dict, meta: dict | None = None) -> float | None:
    from .quality import positive
    target = script.get("target_minutes")
    if not positive(target) and script.get("plan_id"):
        try:
            from ..planner.service import load_plan
            plan = load_plan(script["plan_id"])
            target = next(e.target_minutes for e in plan.episodes if e.episode_id == script["episode_id"])
        except (OSError, ValueError, StopIteration):
            target = None
    if not positive(target):
        target = (meta or {}).get("target_minutes")
    return float(target) if positive(target) else None


async def synthesize_episode(script: dict, *, album: str = "", local_only: bool = False) -> dict:
    """整集合成入口；M6 旧成品会仅重跑本地后处理，不会重新请求 TTS/LLM。"""
    ep_id = script["episode_id"]
    final = settings.episode_audio_path(ep_id)
    meta_path = settings.meta_path(ep_id)
    if final.exists() and final.stat().st_size > 0 and meta_path.exists():
        existing_meta = json.loads(meta_path.read_text("utf-8"))
        if existing_meta.get("pipeline_version", 0) >= AUDIO_PIPELINE_VERSION:
            from .quality import stale
            if stale(existing_meta):
                return await recheck_quality(script)
            tts_payload = (json.loads(settings.tts_path(ep_id).read_text("utf-8"))
                           if settings.tts_path(ep_id).exists() else {})
            voices = _voices_label(ep_id, tts_payload.get("chunks", []))
            from ..persist import upsert_audio  # 迁移后旧 DB 行也补齐 M6 meta/quality
            await upsert_audio(ep_id, existing_meta, voices)
            return existing_meta

    tts_file = settings.tts_path(ep_id)
    if tts_file.exists():
        payload = json.loads(tts_file.read_text("utf-8"))
        chunks = _upgrade_legacy_tts(script, payload)
    else:
        chunks = build_tts_json(script)

    if local_only:
        paths = await _synth_missing(ep_id, chunks, local_only=True)
        synth_info: dict = {}
    else:
        paths, synth_info = await _synth_with_fallback(ep_id, chunks)
    if len(paths) != len(chunks):
        raise RuntimeError(f"chunk 数不一致:期望 {len(chunks)},实际 {len(paths)}")
    durations = await _chunk_durations(paths)
    if any(duration <= 0 for duration in durations):
        bad = [chunk["seq"] for chunk, duration in zip(chunks, durations) if duration <= 0]
        raise RuntimeError(f"chunk 无法探测时长:{bad}")
    chapters = _chapters(chunks, durations)

    await _concat(
        chunks, paths, final, title=script.get("title") or ep_id,
        album=album, track=int(script.get("seq") or 0), chapters=chapters,
    )

    duration = await _probe_duration(final)
    peak = await _peak_db(final)
    total_chars = sum(len(chunk["text"]) for chunk in chunks)
    actual_wpm = total_chars / (duration / 60) if duration else 0.0
    target_minutes = _requested_minutes(script)
    calibration = _calibration(ep_id, actual_wpm)
    history = _history_wpm(ep_id)
    recommended_wpm = float(statistics.median(history) if len(history) >= settings.speech_rate_min_samples else settings.words_per_minute)
    estimated_duration_sec = total_chars / recommended_wpm * 60 if recommended_wpm else None
    duration_error_pct = (
        (duration - estimated_duration_sec) / estimated_duration_sec * 100
        if estimated_duration_sec else 0.0
    )
    quality = _quality_report(
        duration=duration, estimated_duration_sec=estimated_duration_sec, peak_db=peak,
        chunks=chunks, paths=paths, durations=durations, target_minutes=target_minutes,
    )
    meta = {
        "pipeline_version": AUDIO_PIPELINE_VERSION,
        "episode_id": ep_id,
        "audio_path": str(final),
        "duration_sec": round(duration, 3),
        "target_minutes": target_minutes,
        "duration_estimate": {
            "estimated_sec": round(estimated_duration_sec, 3),
            "error_sec": round(duration - estimated_duration_sec, 3),
            "error_percent": round(duration_error_pct, 2),
            "calibrated": True,
            "calibrated_words_per_min": round(recommended_wpm, 1),
        },
        "char_count": total_chars,
        "chunk_count": len(chunks),
        "words_per_min": round(actual_wpm, 1),  # 兼容旧消费者
        "chapters": chapters,
        "id3": {
            "title": script.get("title") or ep_id,
            "artist": settings.audio_artist,
            "album": album or "DriveCast",
            "track": int(script.get("seq") or 0),
            "cover_path": str(settings.audio_cover_path),
        },
        "speech_rate_calibration": calibration,
        "quality": quality,
        "status": "ready",
    }
    tts_info = {**_provider_summary(ep_id, chunks), **synth_info}
    if tts_info.get("provider") or tts_info.get("voices"):
        meta["tts"] = tts_info  # 实际出声的通道/音色;回退时带 fallback_from/fallback_reason
    _atomic_meta(meta_path, meta)

    voices = _voices_label(ep_id, chunks)
    from ..persist import upsert_audio  # 延迟导入防环
    await upsert_audio(ep_id, meta, voices)
    if quality["status"] != "passed":
        print(f"[warn] {ep_id} 音频质检告警:{','.join(quality['failed_checks'])}")
    print(f"[tts] {ep_id} 完成:{duration / 60:.1f} 分钟,{len(chunks)} 段,"
          f"实测语速 {meta['words_per_min']} 字/分,章节 {len(chapters)} 个")
    return meta


def _atomic_meta(path: Path, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".json.part")
    part.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    part.replace(path)


async def recheck_quality(script: dict) -> dict:
    """只更新质量快照，不修改成品，不调用合成或转码。"""
    from .quality import report, positive
    ep_id = script["episode_id"]
    final = settings.episode_audio_path(ep_id)
    if not final.is_file():
        raise RuntimeError("无成品音频可重检")
    meta_path = settings.meta_path(ep_id)
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except (OSError, ValueError):
        from ..db import session
        from ..models import Audio, Episode
        from sqlalchemy import select
        async with session() as db:
            raw = await db.scalar(select(Audio.meta_json).join(Episode, Episode.id == Audio.episode_id).where(Episode.ext_id == ep_id))
        meta = json.loads(raw or "{}")
    duration = await _probe_duration(final)
    if duration <= 0:
        raise RuntimeError("成品音频无法探测时长")
    target = _requested_minutes(script, meta)
    estimated = meta.get("duration_estimate", {}).get("estimated_sec")
    tts_path = settings.tts_path(ep_id)
    try:
        chunks = json.loads(tts_path.read_text("utf-8")).get("chunks", [])
    except (OSError, ValueError):
        chunks = []
    paths = [settings.chunks_dir(ep_id) / f"{int(c['seq']):03d}.mp3" for c in chunks]
    if chunks and all(p.is_file() for p in paths):
        durations = await _chunk_durations(paths)
        chunk_check = _chunk_consistency(chunks, paths, durations)
    else:
        chunk_check = {"passed": None, "reason": "旧音频缺少分段验证材料"}
    meta.update(episode_id=ep_id, audio_path=str(final), duration_sec=duration)
    if positive(target):
        meta["target_minutes"] = target
    meta["quality"] = report(duration, estimated, target, await _peak_db(final), chunk_check)
    _atomic_meta(meta_path, meta)
    from ..persist import upsert_audio
    await upsert_audio(ep_id, meta, _voices_label(ep_id, chunks))
    return meta

"""音频播放、元数据/下载 + 进度上报 API(docs/02 §4 / docs/04 §2 / docs/05 M6)。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import inspect, select, text

from ..auth import get_current_user
from ..config import settings
from ..db import session
from ..models import Episode, Plan, User

router = APIRouter(prefix="/api", tags=["audio"])


class ProgressUpdate(BaseModel):
    position_sec: int = Field(ge=0)


class ProgressOut(BaseModel):
    episode_id: int
    position_sec: int
    listen_status: str
    duration_sec: int


# 优先读取文件态 meta；文件缺失/损坏时回退 DB 中的完整快照，旧 schema 仍可播放。


def _parse_meta_json(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _load_meta(ext_id: str, persisted: dict[str, Any] | None = None) -> dict[str, Any]:
    path = settings.meta_path(ext_id)
    if path.is_file():
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict):
            return data
    return persisted or {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _chapter_title(item: dict[str, Any], index: int) -> str:
    for key in ("title", "label", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"章节 {index + 1}"


def _chapters(meta: dict[str, Any], duration_sec: float) -> list[dict[str, Any]]:
    raw = meta.get("chapters")
    if not isinstance(raw, list):
        return []

    chapters: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        start = _first_number(
            item.get("start_sec"), item.get("start"), item.get("time_sec"), item.get("timestamp_sec")
        )
        if start is None or start < 0:
            continue
        if duration_sec > 0:
            start = min(start, duration_sec)
        chapter = {"title": _chapter_title(item, index), "start_sec": round(start, 3)}
        end = _number(item.get("end_sec"))
        if end is not None and end > start:
            chapter["end_sec"] = round(min(end, duration_sec) if duration_sec > 0 else end, 3)
        chapters.append(chapter)

    chapters.sort(key=lambda item: item["start_sec"])
    unique: list[dict[str, Any]] = []
    for chapter in chapters:
        if unique and chapter["start_sec"] == unique[-1]["start_sec"]:
            continue
        unique.append(chapter)
    for index, chapter in enumerate(unique):
        if "end_sec" not in chapter:
            end = unique[index + 1]["start_sec"] if index + 1 < len(unique) else duration_sec
            if end > chapter["start_sec"]:
                chapter["end_sec"] = round(end, 3)
    return unique


def _check_passed(checks: dict[str, Any], *names: str) -> bool | None:
    """兼容旧版 bool check 与 M6 `{..., passed: bool}` 结构。"""
    for name in names:
        value = checks.get(name)
        if isinstance(value, bool):
            return value
        if isinstance(value, dict) and isinstance(value.get("passed"), bool):
            return value["passed"]
    return None


def _quality(meta: dict[str, Any], duration_sec: float) -> dict[str, Any]:
    from ..tts.quality import stale
    raw = meta.get("quality")
    quality = dict(raw) if isinstance(raw, dict) else {}
    checks = quality.get("checks") if isinstance(quality.get("checks"), dict) else {}

    duration_ok = _check_passed(checks, "duration_range", "duration")
    if duration_ok is None and isinstance(quality.get("duration_ok"), bool):
        duration_ok = quality["duration_ok"]
    if duration_ok is None and duration_sec > 0:
        from ..tts.quality import duration_check
        duration_ok = duration_check(duration_sec)["passed"]

    clipping_ok = _check_passed(checks, "peak", "clipping")
    if clipping_ok is None and isinstance(quality.get("clipping_ok"), bool):
        clipping_ok = quality["clipping_ok"]
    peak_check = checks.get("peak") if isinstance(checks.get("peak"), dict) else {}
    max_volume_db = _first_number(peak_check.get("max_volume_db"), quality.get("max_volume_db"))
    if clipping_ok is None and max_volume_db is not None:
        clipping_ok = max_volume_db <= -0.5

    chunks_ok = _check_passed(checks, "chunks")
    if chunks_ok is None and isinstance(quality.get("chunks_ok"), bool):
        chunks_ok = quality["chunks_ok"]
    chunk_check = checks.get("chunks") if isinstance(checks.get("chunks"), dict) else {}
    chunk_count = meta.get("chunk_count")
    expected_chunks = chunk_check.get("expected", quality.get("expected_chunk_count",
                                                                meta.get("expected_chunk_count")))
    if chunks_ok is None and "passed" not in chunk_check and isinstance(chunk_count, int) and isinstance(expected_chunks, int):
        chunks_ok = chunk_count == expected_chunks

    normalized_checks = {
        "duration": duration_ok,
        "clipping": clipping_ok,
        "chunks": chunks_ok,
    }
    known = [value for value in normalized_checks.values() if value is not None]
    status = quality.get("status")
    if status not in {"passed", "warning", "failed", "unknown"}:
        if any(value is False for value in known):
            status = "warning"
        elif len(known) == len(normalized_checks):
            status = "passed"
        else:
            status = "unknown"

    messages = quality.get("messages", quality.get("warnings", []))
    if isinstance(messages, str):
        messages = [messages]
    if not isinstance(messages, list):
        messages = []
    failed_checks = quality.get("failed_checks")
    if not isinstance(failed_checks, list):
        failed_checks = []
    # 旧 meta 可能只列 failed_checks 而没有顶层 status；不能同时显示“通过”和告警。
    if (failed_checks or any(v is False for v in known)) and status in {"passed", "unknown"}:
        status = "warning"
    if not messages:
        from ..tts.quality import message
        messages = [message(str(name), checks) for name in failed_checks]

    return {
        "status": status,
        "checks": normalized_checks,
        "details": checks,
        "recheck_recommended": stale(meta),
        "failed_checks": [str(name) for name in failed_checks],
        "max_volume_db": max_volume_db,
        "chunk_count": chunk_count if isinstance(chunk_count, int) else None,
        "expected_chunk_count": expected_chunks if isinstance(expected_chunks, int) else None,
        "messages": [str(message) for message in messages if str(message).strip()],
    }


def _duration_estimate(meta: dict[str, Any], duration_sec: float) -> dict[str, Any]:
    raw = meta.get("duration_estimate")
    estimate = dict(raw) if isinstance(raw, dict) else {}
    estimated = _first_number(
        estimate.get("estimated_sec"), meta.get("estimated_duration_sec"), meta.get("duration_estimate_sec")
    )
    target_minutes = _number(meta.get("target_minutes"))
    if estimated is None and target_minutes is not None and target_minutes > 0:
        estimated = target_minutes * 60
    error_sec = _number(estimate.get("error_sec"))
    if error_sec is None and estimated is not None and duration_sec > 0:
        error_sec = duration_sec - estimated
    error_percent = _number(estimate.get("error_percent"))
    if error_percent is None and error_sec is not None and estimated is not None and estimated > 0:
        error_percent = error_sec / estimated * 100

    calibration = meta.get("speech_rate_calibration")
    calibration = calibration if isinstance(calibration, dict) else {}
    calibrated = estimate.get("calibrated")
    if not isinstance(calibrated, bool):
        calibrated = bool(calibration) or (
            meta.get("calibrated") if isinstance(meta.get("calibrated"), bool) else False
        )
    recommended_wpm = _first_number(
        calibration.get("recommended_words_per_minute"),
        estimate.get("calibrated_words_per_min"),
        meta.get("calibrated_words_per_min"),
    )
    actual_wpm = _first_number(
        calibration.get("observed_words_per_minute"), meta.get("words_per_min")
    )
    coefficient = _number(calibration.get("coefficient_vs_config"))
    sample_count = calibration.get("history_samples")
    env_mutated = calibration.get("env_mutated")
    if not isinstance(env_mutated, bool) and isinstance(meta.get("env_mutated"), bool):
        env_mutated = meta["env_mutated"]

    return {
        "estimated_sec": round(estimated, 1) if estimated is not None else None,
        "actual_sec": round(duration_sec, 1) if duration_sec > 0 else None,
        "error_sec": round(error_sec, 1) if error_sec is not None else None,
        "error_percent": round(error_percent, 1) if error_percent is not None else None,
        "calibrated": calibrated,
        "recommended_words_per_min": round(recommended_wpm, 1) if recommended_wpm is not None else None,
        # 保留早期 Web 契约别名，旧前端仍可消费。
        "calibrated_words_per_min": round(recommended_wpm, 1) if recommended_wpm is not None else None,
        "actual_words_per_min": round(actual_wpm, 1) if actual_wpm is not None else None,
        "coefficient_vs_config": round(coefficient, 4) if coefficient is not None else None,
        "history_samples": sample_count if isinstance(sample_count, int) else None,
        "env_mutated": env_mutated if isinstance(env_mutated, bool) else None,
    }


class AudioRecord:
    def __init__(self, file_path: str, duration_sec: int, meta: dict[str, Any] | None = None):
        self.file_path = file_path
        self.duration_sec = duration_sec
        self.meta = meta or {}


async def _read_audio_row(s, episode_id: int):
    """运行时以 0003 为准；兼容测试/旧进程直接调用播放 API 的未迁移库。"""
    def read(sync_session):
        connection = sync_session.connection()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("audio")
        }
        selected = "file_path, duration_sec"
        if "meta_json" in columns:
            selected += ", meta_json"
        return connection.execute(
            text(f"SELECT {selected} FROM audio WHERE episode_id = :episode_id"),
            {"episode_id": episode_id},
        ).mappings().first()

    return await s.run_sync(read)


async def _owned_episode(s, episode_id: int, user_id: int | None) -> Episode | None:
    """取 episode 行;user_id 非 None 时经 plan 关联校验归属(跨用户视同不存在)。"""
    stmt = select(Episode).where(Episode.id == episode_id)
    if user_id is not None:
        stmt = stmt.join(Plan, Plan.id == Episode.plan_id).where(Plan.user_id == user_id)
    return await s.scalar(stmt)


async def _audio_record(episode_id: int, user_id: int | None = None) -> tuple[Episode, AudioRecord, Path]:
    async with session() as s:
        episode = await _owned_episode(s, episode_id, user_id)
        audio_row = await _read_audio_row(s, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "episode not found"})
    if audio_row is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "audio not found"})
    audio = AudioRecord(
        str(audio_row["file_path"]),
        int(audio_row["duration_sec"] or 0),
        _parse_meta_json(audio_row.get("meta_json")),
    )
    path = Path(audio.file_path)
    if not path.is_file():
        # DB 存的是生成那台机器的绝对路径;rsync 迁移到服务器后路径失配(docs/06 §7),
        # 按 ext_id 在本机重算(与 _load_meta 对齐),命中则回写 DB 自愈。
        fallback = settings.episode_audio_path(episode.ext_id)
        if fallback.is_file():
            async with session() as s:
                await s.execute(
                    text("UPDATE audio SET file_path = :p WHERE episode_id = :id"),
                    {"p": str(fallback), "id": episode_id},
                )
                await s.commit()
            return episode, audio, fallback
        raise HTTPException(status_code=404, detail={"code": 404, "message": "audio file missing"})
    return episode, audio, path


@router.get("/episodes/{episode_id}/audio/meta")
async def get_audio_meta(episode_id: int, user: User = Depends(get_current_user)) -> dict[str, Any]:
    """播放页所需章节、质检和时长校准信息;兼容仅含 M1 字段的旧 meta。"""
    episode, audio, path = await _audio_record(episode_id, user.id)
    meta = _load_meta(episode.ext_id, audio.meta)
    duration = _number(meta.get("duration_sec")) or float(audio.duration_sec or 0)
    size_bytes = path.stat().st_size
    return {
        "episode_id": episode_id,
        "ext_id": episode.ext_id,
        "duration_sec": round(duration, 1),
        "size_bytes": size_bytes,
        "chapters": _chapters(meta, duration),
        "quality": _quality(meta, duration),
        "duration_estimate": _duration_estimate(meta, duration),
        "download_url": f"/api/episodes/{episode_id}/audio/download",
    }


@router.api_route("/episodes/{episode_id}/audio/download", methods=["GET", "HEAD"])
async def download_audio(episode_id: int, user: User = Depends(get_current_user)) -> FileResponse:
    """显式 MP3 下载出口;与流式播放分开设置 attachment。鉴权支持 ?token=(<a href> 无请求头)。"""
    episode, _, path = await _audio_record(episode_id, user.id)
    filename = f"{episode.ext_id}.mp3"
    return FileResponse(path=path, media_type="audio/mpeg", filename=filename, stat_result=path.stat())


@router.api_route("/episodes/{episode_id}/audio", methods=["GET", "HEAD"])
async def stream_audio(episode_id: int, user: User = Depends(get_current_user)) -> FileResponse:
    """音频流式播放,浏览器自动发 Range;FileResponse 在当前 starlette 版本原生支持 206(docs/02 §4)。

    <audio src> 无法自定义请求头,鉴权支持 ?token=(见 drivecast/auth.py 白名单)。
    """
    _, _, path = await _audio_record(episode_id, user.id)

    # Starlette FileResponse 已处理 Range/206/Content-Range;验证见验证清单 curl。
    return FileResponse(
        path=path,
        media_type="audio/mpeg",
        filename=path.name,
        stat_result=path.stat(),
        content_disposition_type="inline",
    )


@router.post("/episodes/{episode_id}/progress", response_model=ProgressOut)
async def report_progress(
    episode_id: int, body: ProgressUpdate, user: User = Depends(get_current_user)
) -> dict:
    """上报播放 position;听完置 listen_status=done(docs/02 §4 / docs/04 §2.3)。"""
    async with session() as s:
        ep = await _owned_episode(s, episode_id, user.id)
        if ep is None:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "episode not found"})
        audio_row = (
            await s.execute(
                text("SELECT duration_sec FROM audio WHERE episode_id = :episode_id"),
                {"episode_id": episode_id},
            )
        ).mappings().first()
        duration = int(audio_row["duration_sec"] or 0) if audio_row else 0

        # 钳到 [0, duration):进度条抖动/seek 事件序错会上报 >= 时长的值,
        # 下一场播放 currentTime 被顶到末尾立即触发 ended,表现为"秒跳下一集"。
        # "听完"仍按原始上报判定,只有落库的续播位置被钳回。
        position = max(0, body.position_sec)
        finished = bool(duration) and position >= duration
        if duration > 0:
            position = min(position, max(0, duration - 1))

        ep.position_sec = position
        if finished:
            ep.listen_status = "done"
        elif position > 0:
            ep.listen_status = "listening"
        await s.commit()
        return {
            "episode_id": episode_id,
            "position_sec": ep.position_sec,
            "listen_status": ep.listen_status,
            "duration_sec": duration,
        }

"""edge-tts 封装(docs/03 §3):流式写盘 + 全局信号量限流 + 指数退避。"""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

import edge_tts

from ..config import settings
from ..concurrency import request_limit


def _get_sem() -> asyncio.Semaphore:
    return request_limit("tts")


async def synth_chunk(text: str, voice: str, rate: str, out_path: Path) -> None:
    """合成单个 chunk 到 mp3。写入 .part 临时文件,成功后原子 replace,防半文件残留。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = out_path.with_suffix(f"{out_path.suffix}.part")
    max_retry = settings.tts_chunk_max_retries
    async with _get_sem():
        last: BaseException | None = None
        for attempt in range(1, max_retry + 1):
            try:
                comm = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=settings.tts_pitch)
                await comm.save(str(part_path))
                if part_path.exists() and part_path.stat().st_size > 0:
                    part_path.replace(out_path)
                    return
                raise RuntimeError("edge-tts 产物为空")
            except asyncio.CancelledError:
                part_path.unlink(missing_ok=True)
                raise
            except Exception as exc:  # noqa: BLE001 — 限流/网络统一退避
                part_path.unlink(missing_ok=True)
                last = exc
                if attempt == max_retry:
                    break
                delay = 2 ** (attempt - 1) + random.uniform(0, 0.5)
                print(f"[tts] chunk {out_path.name} 第{attempt}次失败({type(exc).__name__}), {delay:.1f}s 后重试")
                await asyncio.sleep(delay)
        raise RuntimeError(f"chunk 合成失败({max_retry} 次): {out_path}") from last


async def jitter_sleep() -> None:
    """chunk 间随机停顿 0.3-0.8s,模拟自然请求(docs/03 §3.3)。"""
    await asyncio.sleep(random.uniform(0.3, 0.8))

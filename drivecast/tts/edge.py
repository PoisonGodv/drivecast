"""edge-tts 封装(docs/03 §3):流式写盘 + 全局信号量限流 + 指数退避。"""
from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path

import aiohttp
import edge_tts

from ..config import settings
from ..concurrency import request_limit
from .errors import TTSFatalError, TTSRetryExhausted, brief


def _communicate(text: str, voice: str, rate: str) -> edge_tts.Communicate:
    try:
        return edge_tts.Communicate(text, voice=voice, rate=rate, pitch=settings.tts_pitch)
    except (ValueError, TypeError) as exc:  # 构造期校验音色/语速格式,重试无意义
        raise TTSFatalError(
            f"edge-tts 参数无效({brief(exc)}),请检查 TTS_VOICE_*/TTS_RATE_*/TTS_PITCH 配置") from exc


def _fatal(exc: Exception) -> TTSFatalError | None:
    """库内已对 403 做过一次时钟校正重试,仍 401/403 说明被拒,退避也无用。"""
    if isinstance(exc, aiohttp.ClientResponseError) and exc.status in (401, 403):
        return TTSFatalError(f"edge-tts 服务拒绝访问(HTTP {exc.status}),可能被限制或需升级 edge-tts")
    return None

logger = logging.getLogger(__name__)


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
                comm = _communicate(text, voice, rate)
                await comm.save(str(part_path))
                if part_path.exists() and part_path.stat().st_size > 0:
                    part_path.replace(out_path)
                    return
                raise RuntimeError("edge-tts 产物为空")
            except (asyncio.CancelledError, TTSFatalError):
                part_path.unlink(missing_ok=True)
                raise
            except Exception as exc:  # noqa: BLE001 — 限流/网络统一退避
                part_path.unlink(missing_ok=True)
                fatal = _fatal(exc)
                if fatal is not None:
                    raise fatal from exc
                last = exc
                if attempt == max_retry:
                    break
                delay = 2 ** (attempt - 1) + random.uniform(0, 0.5)
                logger.warning(f"chunk {out_path.parent.name}/{out_path.name} 第{attempt}次失败({type(exc).__name__}), {delay:.1f}s 后重试")
                await asyncio.sleep(delay)
        raise TTSRetryExhausted(
            f"edge-tts 片段 {out_path.stem} 连续 {max_retry} 次失败:{brief(last or '')}") from last


async def jitter_sleep() -> None:
    """chunk 间随机停顿 0.3-0.8s,模拟自然请求(docs/03 §3.3)。"""
    await asyncio.sleep(random.uniform(0.3, 0.8))

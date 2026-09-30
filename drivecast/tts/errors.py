"""TTS 通道错误分级:可重试(网络/限流)与不可重试(鉴权/欠费/音色无效)分开,消息直接给用户看。"""
from __future__ import annotations


class TTSProviderError(RuntimeError):
    """通道级失败;str(exc) 是可直接展示在单集卡片上的中文说明。"""
    retryable = True


class TTSFatalError(TTSProviderError):
    """重试无意义的失败:立即中止,不走 chunk 退避也不走 job 重试。"""
    retryable = False


class TTSRetryExhausted(TTSProviderError):
    """单 chunk 退避重试耗尽(多为网络/限流),job 级仍可稍后重试。"""


class FFmpegTimeoutError(RuntimeError):
    """ffmpeg 子进程超时被强杀。"""


def brief(exc: BaseException | str, limit: int = 160) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text if len(text) <= limit else text[:limit] + "…"

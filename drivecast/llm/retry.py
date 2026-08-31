"""指数退避重试(docs/02 §5):仅对网络错误/5xx/429 重试,4xx 直接抛。"""
from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
import openai

T = TypeVar("T")

_RETRYABLE = (
    httpx.HTTPError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,   # 5xx
    openai.RateLimitError,        # 429
)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code >= 500 or exc.status_code == 429
    return isinstance(exc, _RETRYABLE)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    what: str = "llm-call",
) -> T:
    """1s/2s/4s 指数退避;不可重试错误直接抛。"""
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — 统一在此分类
            last = exc
            if not is_retryable(exc) or attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            print(f"[retry] {what} 第{attempt}次失败({type(exc).__name__}), {delay:.0f}s 后重试")
            await asyncio.sleep(delay)
    raise last  # pragma: no cover

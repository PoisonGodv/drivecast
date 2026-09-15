"""进程内共享请求预算；不同事件循环不复用已绑定的 asyncio 原语。"""
from __future__ import annotations

import asyncio

from .config import settings


def request_limit(kind: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    # 生命周期跟随 loop，不把已关闭的测试/CLI loop 留在模块全局。
    limits = getattr(loop, "_drivecast_request_limits", None)
    if limits is None:
        limits = {}
        setattr(loop, "_drivecast_request_limits", limits)
    if kind not in limits:
        size = settings.llm_max_concurrency if kind == "llm" else (4 if kind == "probe" else 1)
        limits[kind] = asyncio.Semaphore(size)
    return limits[kind]


async def gather_cancel(*aws):
    """保留原始异常，同时回收所有兄弟任务（包括嵌套拆段）。"""
    tasks = [asyncio.ensure_future(aw) for aw in aws]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

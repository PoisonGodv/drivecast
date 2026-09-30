"""统一入口:uvicorn 启动 Web 服务 + 内嵌常驻 worker(docs/05 M3)。

access log 的 ?token= 脱敏过滤器在 drivecast.web.main 模块级安装,
这里无需(也不能)重复挂载。
"""
from __future__ import annotations

import sys

import uvicorn

from drivecast.auth import check_auth_secret_for_host
from drivecast.config import settings
from drivecast.logging_setup import setup_logging


class _Server(uvicorn.Server):
    """收到退出信号立即通知 SSE 收尾:uvicorn 关停时不会给流式响应发 disconnect。"""

    def handle_exit(self, sig, frame) -> None:
        from drivecast.web.api_plans import server_stopping

        loop = getattr(self, "_loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(server_stopping.set)
        super().handle_exit(sig, frame)

    async def serve(self, sockets=None) -> None:
        import asyncio

        self._loop = asyncio.get_running_loop()
        await super().serve(sockets)


if __name__ == "__main__":
    setup_logging()
    try:
        check_auth_secret_for_host(settings.web_host)
    except RuntimeError as exc:
        sys.exit(f"[auth] {exc}")
    config = uvicorn.Config(
        "drivecast.web.main:app",
        host=settings.web_host,
        port=settings.web_port,
        reload=False,
        # 兜底:仍有长连接不收尾时最多等这么久,保证 lifespan 能及时把在途 job 放回队列
        timeout_graceful_shutdown=settings.web_graceful_shutdown_sec,
    )
    _Server(config).run()

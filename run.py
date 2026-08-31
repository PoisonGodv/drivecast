"""统一入口:uvicorn 启动 Web 服务 + 内嵌常驻 worker(docs/05 M3)。"""
from __future__ import annotations

import uvicorn

from drivecast.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "drivecast.web.main:app",
        host=settings.web_host,
        port=settings.web_port,
        reload=False,
    )

"""FastAPI 装配(docs/02 §2/§4 + docs/04 §5):路由挂载、StaticFiles、SPA fallback、常驻 worker。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..config import ensure_data_dirs, settings
from ..db import close_db, init_db
from ..jobs import worker
from . import api_audio, api_plans, api_review

_worker_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动:建目录/建表，可选常驻 worker；关闭时释放任务与 DB。"""
    global _worker_task
    ensure_data_dirs()
    await init_db()

    # 维护/验收可通过 WORKER_ENABLED=false 禁止消费昂贵任务。
    _worker_task = (
        asyncio.create_task(worker.run_forever())
        if settings.worker_enabled
        else None
    )

    yield

    # shutdown 优雅取消
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    await close_db()


app = FastAPI(title="DriveCast", lifespan=lifespan)

# API 路由先于 static mount,避免被 StaticFiles 拦截
app.include_router(api_plans.router)
app.include_router(api_audio.router)
app.include_router(api_review.router)

# 静态资源挂 /static(与前端引用路径一致);SPA fallback 兜底 index.html(docs/04 §5)
app.mount(
    "/static",
    StaticFiles(directory=settings.static_dir, check_dir=False),
    name="static",
)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    """非 API 路径一律回 index.html(hash 路由由前端接管)。"""
    from fastapi.responses import FileResponse
    return FileResponse(settings.static_dir / "index.html")

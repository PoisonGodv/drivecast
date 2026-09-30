"""FastAPI 装配(docs/02 §2/§4 + docs/04 §5):路由挂载、StaticFiles、SPA fallback、常驻 worker。"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import traceback
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from ..auth import get_current_user, login_ready_usernames
from ..config import ensure_data_dirs, settings
from ..db import close_db, init_db
from ..jobs import worker
from . import api_audio, api_auth, api_directions, api_plans, api_review

# uvicorn access log 会打出完整 query(SSE/audio 的 ?token= 会泄进日志),
# 给 uvicorn.access 挂 logger 级过滤器把 token 值替换成 ***。装在 app 模块级:
# 无论经 run.py 还是 `uvicorn drivecast.web.main:app` 等任何方式启动都生效。
# 业务代码不打印 request.url。
_TOKEN_QS = re.compile(r"([?&])token=[^&\s]+")


class _RedactTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(
                _TOKEN_QS.sub(r"\1token=***", arg) if isinstance(arg, str) else arg
                for arg in args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTokenFilter())

_worker_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动:建目录/建表，可选常驻 worker；关闭时释放任务与 DB。"""
    global _worker_task
    ensure_data_dirs()
    await init_db()
    if not await login_ready_usernames():
        print(
            "[auth] 还没有可登录的账号(admin 仍是锁定占位)。请先执行:\n"
            "  .venv/bin/python -m drivecast reset-password admin",
            file=sys.stderr,
        )

    # 维护/验收可通过 WORKER_ENABLED=false 禁止消费昂贵任务。
    if settings.worker_enabled:
        _worker_task = asyncio.create_task(worker.run_forever())

        def _log_worker_dead(task: asyncio.Task) -> None:
            # run_forever 自带异常兜底,正常情况下到不了这里;
            # 真到这说明消费循环彻底退出——job 将无人消费(UI 停留"排队中"),必须打出来。
            if not task.cancelled() and task.exception() is not None:
                print("[worker] 常驻消费 task 已死亡,队列将不再被消费!", file=sys.stderr)
                traceback.print_exception(task.exception())

        _worker_task.add_done_callback(_log_worker_dead)

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

# API 路由先于 static mount,避免被 StaticFiles 拦截。
# 业务路由统一挂 get_current_user(Bearer 头;SSE/audio 两类 GET 额外走 ?token= 白名单,
# 见 drivecast/auth.py)。api_auth 自身不挂:login 必须匿名可达,/me 在 handler 内声明依赖。
app.include_router(api_auth.router)
app.include_router(api_plans.router, dependencies=[Depends(get_current_user)])
app.include_router(api_audio.router, dependencies=[Depends(get_current_user)])
app.include_router(api_review.router, dependencies=[Depends(get_current_user)])
app.include_router(api_directions.router, dependencies=[Depends(get_current_user)])

class NoCacheStaticFiles(StaticFiles):
    """无构建前端禁止强缓存,确保更新即生效,彻底杜绝 Safari 混用旧 JS。"""
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# 静态资源挂 /static(与前端引用路径一致);SPA fallback 兜底 index.html(docs/04 §5)
app.mount(
    "/static",
    NoCacheStaticFiles(directory=settings.static_dir, check_dir=False),
    name="static",
)


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    """非 API 路径一律回 index.html(hash 路由由前端接管;no-store 强制刷新)。"""
    from fastapi.responses import FileResponse
    return FileResponse(
        settings.static_dir / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )

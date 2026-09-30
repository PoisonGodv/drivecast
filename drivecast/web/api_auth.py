"""认证 API:POST /api/auth/login、GET /api/auth/me。

login 是唯一不挂鉴权的业务端点;失败统一 401 模糊文案
(用户不存在与密码错同文案、同耗时——对 dummy 哈希跑一次 verify)。
另带进程内防暴力猜解:按「客户端 IP + 用户名」连续失败计数 + 指数退避锁定
(纯标准库,仅单进程内共享;多 worker 各自计数、重启清零,不替代网关限流)。
只使用 ASGI client 地址;反向代理可信来源和转发头解析由 ASGI 服务器配置。
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import DUMMY_HASH, create_token, get_current_user, login_ready_usernames, verify_password
from ..db import session
from ..models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 连续失败 N 次后锁定;每多失败一次锁定时长翻倍,封顶 15 分钟
LOGIN_LOCK_AFTER = 5
LOGIN_LOCK_BASE_SEC = 30
LOGIN_LOCK_MAX_SEC = 900

# 空闲 1 小时后忘记失败历史;容量满时拒绝新 key,不驱逐未过期锁定。
LOGIN_STATE_TTL_SEC = 3600
LOGIN_MAX_KEYS = 4096
_LOGIN_MAX_EXPONENT = math.ceil(math.log2(LOGIN_LOCK_MAX_SEC / LOGIN_LOCK_BASE_SEC))


@dataclass
class _LoginState:
    fails: int = 0
    until: float = 0.0
    expires_at: float = 0.0
    in_flight: bool = False


# 同一个 peer/用户名同时只允许一次验证;同步锁仅保护内存操作,不跨 await。
# threading.Lock 也覆盖同进程多线程/多事件循环;不提供跨进程限流。
_login_failures: dict[str, _LoginState] = {}
_login_mutex = threading.Lock()
# 保留 shield 后的验证任务,客户端断开也不能提前释放验证名额。
_login_tasks: set[asyncio.Task] = set()


def _client_key(request: Request, username: str) -> str:
    """不自行信任任何转发头;缺少 peer 时共用保守的未知来源桶。"""
    ip = request.client.host if request.client else "?"
    return f"{ip}|{username}"


def _begin_login(key: str) -> float:
    """原子检查并预占验证名额;返回需等待秒数(0 表示已占用)。"""
    now = time.monotonic()
    with _login_mutex:
        state = _login_failures.get(key)
        if state is not None and not state.in_flight and now >= max(state.expires_at, state.until):
            del _login_failures[key]
            state = None
        if state is None:
            if len(_login_failures) >= LOGIN_MAX_KEYS:
                # 仅在容量压力下扫表;条目总数有上界,不清除仍在验证的条目。
                expired = [k for k, v in _login_failures.items()
                           if not v.in_flight and now >= max(v.expires_at, v.until)]
                for expired_key in expired:
                    del _login_failures[expired_key]
                if len(_login_failures) >= LOGIN_MAX_KEYS:
                    return 1.0
            state = _LoginState()
            _login_failures[key] = state
        if state.in_flight:
            return max(1.0, state.until - now)
        if state.until > now:
            return state.until - now
        state.in_flight = True
        return 0.0


def _finish_login(key: str, *, success: bool) -> None:
    now = time.monotonic()
    with _login_mutex:
        if success:
            _login_failures.pop(key, None)
            return
        state = _login_failures[key]
        # 计数和指数均封顶,长时间攻击也不会产生巨大整数。
        state.fails = min(state.fails + 1, LOGIN_LOCK_AFTER + _LOGIN_MAX_EXPONENT)
        lock = 0.0
        if state.fails >= LOGIN_LOCK_AFTER:
            lock = min(LOGIN_LOCK_BASE_SEC * 2 ** (state.fails - LOGIN_LOCK_AFTER),
                       LOGIN_LOCK_MAX_SEC)
        state.until = now + lock
        state.expires_at = now + LOGIN_STATE_TTL_SEC
        state.in_flight = False


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: int
    username: str


class LoginOut(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


@router.post("/login")
async def login(body: LoginIn, request: Request) -> LoginOut:
    key = _client_key(request, body.username)
    wait = _begin_login(key)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail={"code": 429, "message": "尝试过于频繁,请稍后再试"},
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
    task = asyncio.create_task(_verify_login(body, key))
    _login_tasks.add(task)
    task.add_done_callback(_login_task_done)
    user = await asyncio.shield(task)
    if user is None:
        raise HTTPException(
            status_code=401, detail={"code": 401, "message": "用户名或密码错误"}
        )
    token, expires_in = create_token(user.id)
    return LoginOut(token=token, expires_in=expires_in, user=UserOut(id=user.id, username=user.username))


def _login_task_done(task: asyncio.Task) -> None:
    _login_tasks.discard(task)
    if not task.cancelled():
        task.exception()  # 客户端已断开时也回收异常;正常 await 仍会收到异常。


async def _verify_login(body: LoginIn, key: str) -> User | None:
    success = False
    try:
        async with session() as s:
            user = (
                await s.execute(select(User).where(User.username == body.username))
            ).scalar_one_or_none()
        # 不存在的用户对固定 dummy 哈希跑同一套 pbkdf2,拉平失败响应时间;
        # 丢线程池避免阻塞事件循环(SSE/音频流)。
        stored = user.password_hash if user is not None else DUMMY_HASH
        password_ok = await asyncio.to_thread(verify_password, body.password, stored)
        success = user is not None and password_ok
        return user if success else None
    finally:
        # DB/验证异常也按失败收尾,不能留下永久占用,也不能免费重试。
        _finish_login(key, success=success)


@router.get("/status")
async def auth_status() -> dict:
    """匿名可达:只回答「是否已有可登录账号」,供登录页给出首次启动引导,不暴露用户名。"""
    return {"setup_required": not await login_ready_usernames()}


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(id=user.id, username=user.username)

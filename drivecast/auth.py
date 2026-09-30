"""认证核心:pbkdf2 密码哈希 + HS256 JWT(PyJWT)+ FastAPI 依赖。

- 密码:hashlib.pbkdf2_hmac(sha256, 600k 迭代, 16B 随机 salt),纯标准库,无 C 依赖。
- JWT:PyJWT 签发/校验,sub=str(user.id),7 天过期(AUTH_TOKEN_EXPIRE_DAYS)。
- get_current_user 是唯一鉴权入口:Authorization: Bearer 优先;
  仅 GET/HEAD 且路径命中白名单(SSE / <audio> 下载,EventSource/<audio> 无法自定义头)
  时回退 ?token= 查询参数。白名单绝不能放宽到写端点(token 可被 CSRF 式利用)。

用法:
  app.include_router(api.router, dependencies=[Depends(get_current_user)])
  async def handler(user: User = Depends(get_current_user)): ...  # 请求级缓存,不重复查库
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, Request
from sqlalchemy import select

from .config import settings
from .db import session
from .models import User

PBKDF2_ITERATIONS = 600_000
LOCKED_HASH = "!locked"  # 迁移占位:verify_password 恒 False,reset-password 后置真实哈希
# 固定 dummy 哈希:用户不存在时也跑一次完整 pbkdf2,拉平登录失败响应时间
DUMMY_HASH = "pbkdf2_sha256$600000$a5140af1f47364e9939100ad2f0137ce$b2d4320e728ccef38793bd83f7bf07714ec0541a5d17fda9adee996842c85be2"

# ?token= 仅限这两类前端拿不到请求头的 GET 端点(EventSource / <audio src> / <a href>)
_QUERY_TOKEN_PATHS = re.compile(r"^/api/(plans/\d+/events|episodes/\d+/audio(/download)?)$")

_random_secret: str | None = None

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def check_auth_secret_for_host(host: str) -> None:
    """非本机监听却没配 AUTH_SECRET:拒绝启动(每次重启全员被踢,且多进程间 token 互不认)。"""
    if settings.auth_secret.strip() or host in _LOOPBACK_HOSTS:
        return
    raise RuntimeError(
        f"WEB_HOST={host} 对外监听但未配置 AUTH_SECRET。请在 .env 设置:\n"
        "  AUTH_SECRET=$(python -c \"import secrets; print(secrets.token_hex(32))\")"
    )


async def login_ready_usernames() -> list[str]:
    """可登录(已设真实密码)的用户名;为空说明需要先 reset-password。"""
    async with session() as s:
        rows = (await s.execute(select(User.username, User.password_hash))).all()
    return [name for name, pw in rows if pw and pw.startswith("pbkdf2_sha256$")]


# ---------- 密码哈希 ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations_s, salt_hex, hash_hex = stored.split("$")
        iterations = int(iterations_s)
    except (ValueError, AttributeError):
        return False  # '!locked' 占位 / 损坏哈希,恒拒绝
    if scheme != "pbkdf2_sha256" or iterations <= 0:
        return False
    expected = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    )
    return hmac.compare_digest(expected.hex(), hash_hex)


# ---------- JWT ----------

def _auth_secret() -> str:
    """AUTH_SECRET 优先;未配置则进程级随机(重启全员登出,fail-safe)并告警一次。"""
    secret = settings.auth_secret.strip()
    if secret:
        return secret
    global _random_secret
    if _random_secret is None:
        _random_secret = secrets.token_hex(32)
        print(
            "[auth] AUTH_SECRET 未配置,已生成进程级随机密钥;重启后所有 token 将失效。"
            "生产/长期运行请在 .env 配置 AUTH_SECRET。",
            file=sys.stderr,
        )
    return _random_secret


def create_token(user_id: int) -> tuple[str, int]:
    """签发 token,返回 (token, expires_in 秒)。"""
    expires_in = settings.auth_token_expire_days * 86400
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": now + timedelta(seconds=expires_in),
    }
    return jwt.encode(payload, _auth_secret(), algorithm="HS256"), expires_in


def decode_token(token: str) -> int | None:
    """校验并解析 user_id;任何失败(过期/伪造/坏 sub)返回 None。"""
    try:
        payload = jwt.decode(token, _auth_secret(), algorithms=["HS256"])
        return int(payload.get("sub"))
    except (jwt.PyJWTError, TypeError, ValueError):
        return None


# ---------- FastAPI 依赖 ----------

def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    # 查询参数例外:仅 GET/HEAD、仅白名单路径;其余端点带 ?token= 视同无凭证
    if request.method in ("GET", "HEAD") and _QUERY_TOKEN_PATHS.match(request.url.path):
        return request.query_params.get("token") or None
    return None


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": 401, "message": "未登录或登录已过期"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(request: Request) -> User:
    """所有受保护 API 的鉴权依赖;按 user_id 现查 DB(不缓存,CLI 改密/删户即时生效)。"""
    token = _extract_token(request)
    if not token:
        raise _unauthorized()
    user_id = decode_token(token)
    if user_id is None:
        raise _unauthorized()
    async with session() as s:
        user = (await s.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise _unauthorized()
    return user

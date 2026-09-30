"""认证核心测试:哈希、JWT、login/me、Bearer 与 ?token= 白名单、用户管理 CLI。

租户隔离(跨用户 404)属业务路由改造范围,另测;这里只钉死认证边界:
- 登录失败模糊化(不存在用户与错密码同文案、同一次 pbkdf2 计算量)
- '!locked' 迁移占位不可登录
- ?token= 仅对 GET/HEAD 白名单路径生效,写端点/非白名单带 token 一律 401
- CLI create-user/reset-password:无注册 API,用户只能从这里来
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from auth_test_support import real_auth_scope
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivecast import db
from drivecast.auth import (
    LOCKED_HASH,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)
from drivecast.config import settings
from drivecast.models import Audio, Episode, Plan, Topic, User
from drivecast.web.main import app


class HashTest(unittest.TestCase):
    def test_roundtrip_and_rejections(self) -> None:
        stored = hash_password("s3cret-密码")
        self.assertTrue(stored.startswith("pbkdf2_sha256$600000$"))
        self.assertTrue(verify_password("s3cret-密码", stored))
        self.assertFalse(verify_password("wrong", stored))
        # 迁移占位/损坏哈希恒拒绝
        self.assertFalse(verify_password("anything", LOCKED_HASH))
        self.assertFalse(verify_password("anything", "garbage"))
        self.assertFalse(verify_password("anything", "pbkdf2_sha256$abc$zz$zz"))

    def test_unique_salts(self) -> None:
        self.assertNotEqual(hash_password("same"), hash_password("same"))


class AuthApiTest(unittest.TestCase):
    def setUp(self) -> None:
        real_auth_scope(self)

        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        asyncio.run(db.init_db())
        asyncio.run(self._seed())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        from drivecast.web import api_auth
        self.enterContext(mock.patch.dict(api_auth._login_failures, {}, clear=True))

    def tearDown(self) -> None:
        asyncio.run(db.close_db())
        settings.data_dir = self.old_data_dir
        self.tmp.cleanup()

    async def _seed(self) -> None:
        audio_path = settings.data_dir / "audio" / "episodes" / "ep_auth.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"ID3" + b"audio-bytes" * 16)
        async with db.session() as s:
            alice = User(username="alice", password_hash=hash_password("alice-pw"))
            s.add(alice)
            s.add(User(username="locked", password_hash=LOCKED_HASH))
            await s.flush()
            self.alice_id = alice.id
            topic = Topic(title="认证契约", direction="backend_arch", user_id=alice.id)
            s.add(topic)
            await s.flush()
            plan = Plan(ext_id="plan_auth", topic_id=topic.id, title="t", status="active", user_id=alice.id)
            s.add(plan)
            await s.flush()
            episode = Episode(ext_id="ep_auth", plan_id=plan.id, seq=1, title="t", status="ready")
            s.add(episode)
            await s.flush()
            s.add(Audio(
                episode_id=episode.id,
                file_path=str(audio_path),
                duration_sec=60,
                size_bytes=audio_path.stat().st_size,
            ))
            await s.commit()

    # ---------- 首次启动引导 / 密钥守卫 ----------

    def test_auth_status_reports_setup_required_only_without_loginable_user(self) -> None:
        res = self.client.get("/api/auth/status")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"setup_required": False})  # alice 已有真实密码

        async def _lock_all() -> None:
            async with db.session() as s:
                for user in (await s.scalars(select(User))).all():
                    user.password_hash = LOCKED_HASH
                await s.commit()

        asyncio.run(_lock_all())
        self.assertEqual(self.client.get("/api/auth/status").json(), {"setup_required": True})

    def test_non_loopback_host_requires_auth_secret(self) -> None:
        from drivecast.auth import check_auth_secret_for_host

        with mock.patch.object(settings, "auth_secret", ""):
            for host in ("127.0.0.1", "localhost", "::1"):
                check_auth_secret_for_host(host)
            with self.assertRaises(RuntimeError):
                check_auth_secret_for_host("0.0.0.0")
        check_auth_secret_for_host("0.0.0.0")  # real_auth_scope 已配置密钥

    # ---------- login ----------

    def test_login_success_returns_usable_token(self) -> None:
        res = self.client.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertEqual(payload["token_type"], "bearer")
        self.assertEqual(payload["expires_in"], 7 * 86400)
        self.assertEqual(payload["user"], {"id": self.alice_id, "username": "alice"})

        me = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {payload['token']}"})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json(), {"id": self.alice_id, "username": "alice"})

    def test_login_failures_are_uniform_and_vague(self) -> None:
        cases = [
            {"username": "alice", "password": "wrong"},   # 错密码
            {"username": "nobody", "password": "wrong"},  # 用户不存在
            {"username": "locked", "password": "anything"},  # '!locked' 迁移占位
        ]
        for body in cases:
            res = self.client.post("/api/auth/login", json=body)
            self.assertEqual(res.status_code, 401, body)
            self.assertEqual(res.json()["detail"], {"code": 401, "message": "用户名或密码错误"})

    def test_login_lockout_after_repeated_failures(self) -> None:
        """连续失败锁定防在线暴力猜解;成功登录清零计数。"""
        from drivecast.web import api_auth

        clock = self.enterContext(mock.patch.object(api_auth, "time", new=SimpleNamespace(
            monotonic=mock.Mock(return_value=1000.0))))
        wrong = {"username": "alice", "password": "wrong"}
        for _ in range(api_auth.LOGIN_LOCK_AFTER):
            res = self.client.post("/api/auth/login", json=wrong)
            self.assertEqual(res.status_code, 401)
        # 超限后锁定:错密码与正确密码都 429(锁定先于凭证校验)
        res = self.client.post("/api/auth/login", json=wrong)
        self.assertEqual(res.status_code, 429)
        self.assertGreater(int(res.headers["Retry-After"]), 0)
        res = self.client.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
        self.assertEqual(res.status_code, 429)
        # 锁定到期后正确密码可登录,且计数清零
        clock.monotonic.return_value += api_auth.LOGIN_LOCK_BASE_SEC + 1
        res = self.client.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("testclient|alice", api_auth._login_failures)

    def test_token_expiry_rejected(self) -> None:
        token, _ = create_token(self.alice_id)
        self.assertEqual(decode_token(token), self.alice_id)
        self.assertIsNone(decode_token(token + "x"))       # 签名破坏
        self.assertIsNone(decode_token("not-a-jwt"))       # 完全非法

    # ---------- Bearer 边界 ----------

    def test_protected_endpoints_require_token(self) -> None:
        for method, path in [("get", "/api/plans"), ("post", "/api/topics"),
                             ("get", "/api/review/today"), ("get", "/api/episodes/1/audio/meta")]:
            res = getattr(self.client, method)(path, **({"json": {}} if method == "post" else {}))
            self.assertEqual(res.status_code, 401, f"{method} {path}")
            self.assertEqual(res.json()["detail"]["code"], 401)

        res = self.client.get("/api/auth/me")
        self.assertEqual(res.status_code, 401)

    def test_wrong_bearer_token_rejected(self) -> None:
        bad_headers = [
            {"Authorization": "Bearer garbage"},                     # 完全非法
            {"Authorization": "Bearer " + "x" * 64},                 # 伪造格式
            {"Authorization": "Basic dXNlcjpwdw=="},                 # 非Bearer方案
            {"Authorization": "Bearer"},                             # 无凭证部分
        ]
        for headers in bad_headers:
            res = self.client.get("/api/plans", headers=headers)
            self.assertEqual(res.status_code, 401, headers)
            self.assertEqual(res.json()["detail"]["code"], 401)

        # 合法签名但指向不存在用户 → 401
        ghost, _ = create_token(999)
        self.assertEqual(
            self.client.get("/api/plans", headers={"Authorization": f"Bearer {ghost}"}).status_code,
            401,
        )

        # 过期 token → 401(负有效期签出已过期载荷,decode 必须拒绝)
        old_expire = settings.auth_token_expire_days
        settings.auth_token_expire_days = -1
        try:
            expired, _ = create_token(self.alice_id)
        finally:
            settings.auth_token_expire_days = old_expire
        self.assertIsNone(decode_token(expired))
        self.assertEqual(
            self.client.get("/api/plans", headers={"Authorization": f"Bearer {expired}"}).status_code,
            401,
        )

        # 对照:正确 Bearer 正常放行
        good, _ = create_token(self.alice_id)
        self.assertEqual(
            self.client.get("/api/plans", headers={"Authorization": f"Bearer {good}"}).status_code,
            200,
        )

    # ---------- ?token= 白名单 ----------

    def test_query_token_allows_audio_stream_and_download(self) -> None:
        token, _ = create_token(self.alice_id)
        stream = self.client.get("/api/episodes/1/audio", params={"token": token},
                                 headers={"Range": "bytes=0-4"})
        self.assertEqual(stream.status_code, 206)  # Range 命中 → Partial Content
        self.assertEqual(stream.content, b"ID3au")  # bytes=0-4 共 5 字节
        download = self.client.get("/api/episodes/1/audio/download", params={"token": token})
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content[:3], b"ID3")

    def test_query_token_allows_sse_events(self) -> None:
        token, _ = create_token(self.alice_id)
        # 不存在的 plan:生成器推 not_found 后自然结束,流可完整收下(不断连挂死)
        with self.client.stream("GET", "/api/plans/999/events", params={"token": token}) as res:
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.headers["content-type"].startswith("text/event-stream"))
            lines = [line for _, line in zip(range(4), res.iter_lines())]
        self.assertTrue(lines[0].startswith("retry:"))

    def test_query_token_rejected_outside_whitelist(self) -> None:
        token, _ = create_token(self.alice_id)
        # 非白名单 GET:带 token 也 401
        self.assertEqual(self.client.get("/api/plans", params={"token": token}).status_code, 401)
        self.assertEqual(self.client.get("/api/review/today", params={"token": token}).status_code, 401)
        self.assertEqual(
            self.client.get("/api/episodes/1/audio/meta", params={"token": token}).status_code, 401
        )
        # 白名单路径上的写方法不存在(405 之前先被 401 拦:依赖在路由级挂载)
        # 坏 token 在白名单上同样 401
        self.assertEqual(
            self.client.get("/api/episodes/1/audio", params={"token": "broken"}).status_code, 401
        )
        # 指向不存在用户的合法签名 token → 401
        ghost, _ = create_token(999)
        self.assertEqual(
            self.client.get("/api/episodes/1/audio", params={"token": ghost}).status_code, 401
        )

    def test_spa_fallback_and_static_stay_public(self) -> None:
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])


class CliUserTest(unittest.TestCase):
    """用户管理 CLI(系统无注册 API,用户只能从 create-user 来):

    - 成功路径:--password / 环境变量 DRIVECAST_USER_PASSWORD 两种来源,明文不落库,建完即可登录;
    - 失败路径:重名/改密对象不存在 → 非零退出且不产生副作用;无 tty 又无密码 → 非零退出不落库;
    - reset-password 是 0006 迁移 admin('!locked' 占位)后置密码的唯一通道。
    """

    def setUp(self) -> None:
        real_auth_scope(self)

        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        asyncio.run(db.init_db())  # CLI create-user 会自己再 init_db,这里先建好便于种子
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        from drivecast.web import api_auth
        self.enterContext(mock.patch.dict(api_auth._login_failures, {}, clear=True))

    def tearDown(self) -> None:
        asyncio.run(db.close_db())
        settings.data_dir = self.old_data_dir
        self.tmp.cleanup()

    def _cli(self, *argv: str) -> tuple[object, str]:
        """以 python -m drivecast 入口跑子命令,返回 (exit_code, 输出文本)。

        正常结束 code 为 None;sys.exit("msg") 的 code 是消息字符串(真值),
        该消息由解释器打到 stderr,一并捕获便于断言。
        """
        from drivecast.__main__ import main

        buf, err = StringIO(), StringIO()
        code = None
        with mock.patch("sys.argv", ["drivecast", *argv]), \
                redirect_stdout(buf), redirect_stderr(err):
            try:
                main()
            except SystemExit as exc:
                code = exc.code
                print(exc.code, file=err)
        return code, buf.getvalue() + err.getvalue()

    async def _get_user(self, username: str) -> User | None:
        async with db.session() as s:
            return (
                await s.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()

    def _login(self, username: str, password: str) -> int:
        return self.client.post(
            "/api/auth/login", json={"username": username, "password": password}
        ).status_code

    def test_create_user_success_and_login(self) -> None:
        code, out = self._cli("create-user", "carol", "--password", "pw-123456")
        self.assertFalse(code, out)  # None/0 = 成功
        self.assertIn("carol", out)

        user = asyncio.run(self._get_user("carol"))
        self.assertIsNotNone(user)
        self.assertIn(f"id={user.id}", out)
        self.assertIn(f"id={user.id}", out)
        self.assertNotIn("pw-123456", user.password_hash)          # 明文绝不落库
        self.assertTrue(verify_password("pw-123456", user.password_hash))
        self.assertFalse(verify_password("anything-else", user.password_hash))

        # CLI 建的用户立刻能走通 HTTP 登录 → Bearer 访问受保护端点
        res = self.client.post(
            "/api/auth/login", json={"username": "carol", "password": "pw-123456"}
        )
        self.assertEqual(res.status_code, 200)
        me = self.client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {res.json()['token']}"}
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], "carol")

    def test_create_user_duplicate_exits_nonzero_without_side_effect(self) -> None:
        self._cli("create-user", "carol", "--password", "pw-123456")
        code, out = self._cli("create-user", "carol", "--password", "hijacked-pw")
        self.assertTrue(code)          # 非零退出
        self.assertIn("已存在", out)

        # 密码未被覆盖:原密码仍可登录,后来的密码不行
        self.assertEqual(self._login("carol", "pw-123456"), 200)
        self.assertEqual(self._login("carol", "hijacked-pw"), 401)

    def test_create_user_password_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"DRIVECAST_USER_PASSWORD": "env-pw-1"}):
            code, out = self._cli("create-user", "dave")
        self.assertFalse(code, out)
        user = asyncio.run(self._get_user("dave"))
        self.assertTrue(verify_password("env-pw-1", user.password_hash))

    def test_create_user_without_password_in_non_tty_exits_nonzero(self) -> None:
        with mock.patch.dict(os.environ, {"DRIVECAST_USER_PASSWORD": ""}), \
                mock.patch("sys.stdin", new=SimpleNamespace(isatty=lambda: False)):
            code, out = self._cli("create-user", "erin")
        self.assertTrue(code)
        self.assertIn("--password", out)   # 报错要给出可操作的指引
        self.assertIsNone(asyncio.run(self._get_user("erin")))  # 不落半截用户

    def test_reset_password_unlocks_migrated_admin(self) -> None:
        # 0006 迁移出的 admin 是 '!locked' 占位:任何密码都登不上
        async def _seed_locked_admin() -> None:
            async with db.session() as s:
                admin = await s.scalar(select(User).where(User.username == "admin"))
                self.assertIsNotNone(admin)
                self.assertEqual(admin.password_hash, LOCKED_HASH)
                await s.commit()

        asyncio.run(_seed_locked_admin())
        self.assertEqual(self._login("admin", "whatever"), 401)

        code, out = self._cli("reset-password", "admin", "--password", "new-pw-9")
        self.assertFalse(code, out)
        self.assertIn("仍然有效", out)      # 提示旧 token 无撤销机制

        self.assertEqual(self._login("admin", "whatever"), 401)  # 旧密码仍不行
        self.assertEqual(self._login("admin", "new-pw-9"), 200)

    def test_reset_password_unknown_user_exits_nonzero(self) -> None:
        code, out = self._cli("reset-password", "ghost", "--password", "x-pw")
        self.assertTrue(code)
        self.assertIn("不存在", out)


if __name__ == "__main__":
    unittest.main()

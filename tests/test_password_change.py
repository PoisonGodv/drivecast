"""自助改密与 token 吊销:token_version 迁移、ver 校验、POST /api/auth/password、CLI 重置吊销。"""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

import jwt
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from auth_test_support import real_auth_scope
from drivecast import db
from drivecast.__main__ import cmd_reset_password
from drivecast.auth import create_token, decode_claims, hash_password, verify_password
from drivecast.config import settings
from drivecast.models import User
from drivecast.web import api_auth
from drivecast.web.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _migrate(path: Path, revision: str = "head") -> None:
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, revision)


def _columns(path: Path) -> dict[str, tuple]:
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as c:
            return {r[1]: r for r in c.exec_driver_sql("PRAGMA table_info(user)")}
    finally:
        engine.dispose()


class TokenVersionMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        asyncio.run(db.close_db())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "drivecast.db"
        self.enterContext(mock.patch.object(settings, "data_dir", Path(self.tmp.name)))
        self.addCleanup(lambda: asyncio.run(db.close_db()))

    def test_0006_database_gains_non_null_zero_default(self) -> None:
        _migrate(self.db_path, "0006")
        engine = create_engine(f"sqlite:///{self.db_path}")
        with engine.begin() as c:
            c.exec_driver_sql("INSERT INTO user(username, password_hash) VALUES ('bob', 'h')")
        engine.dispose()
        asyncio.run(db.init_db())
        column = _columns(self.db_path)["token_version"]
        self.assertEqual(column[3], 1)  # notnull
        self.assertEqual(str(column[4]), "0")

        async def versions():
            async with db.session() as s:
                return dict((await s.execute(select(User.username, User.token_version))).all())

        self.assertEqual(asyncio.run(versions()), {"admin": 0, "bob": 0})

    def test_downgrade_and_reupgrade_are_idempotent(self) -> None:
        _migrate(self.db_path)
        _migrate(self.db_path)
        config = Config()
        config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{self.db_path}")
        command.downgrade(config, "0007")
        self.assertNotIn("token_version", _columns(self.db_path))
        _migrate(self.db_path)
        self.assertIn("token_version", _columns(self.db_path))

    def test_unversioned_create_all_without_token_version_is_upgraded(self) -> None:
        from drivecast.models import Base

        engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(engine)
        with engine.begin() as c:
            c.exec_driver_sql("ALTER TABLE user DROP COLUMN token_version")
        engine.dispose()
        asyncio.run(db.init_db())
        self.assertIn("token_version", _columns(self.db_path))


class PasswordChangeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        real_auth_scope(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        asyncio.run(db.init_db())
        self.alice_id = asyncio.run(self._seed())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.enterContext(mock.patch.dict(api_auth._login_failures, {}, clear=True))

    def tearDown(self) -> None:
        asyncio.run(db.close_db())
        settings.data_dir = self.old_data_dir
        self.tmp.cleanup()

    async def _seed(self) -> int:
        async with db.session() as s:
            alice = User(username="alice", password_hash=hash_password("alice-old-pw"))
            s.add(alice)
            await s.commit()
            return alice.id

    async def _user(self) -> User:
        async with db.session() as s:
            return await s.get(User, self.alice_id)

    def _login(self, password: str = "alice-old-pw"):
        return self.client.post("/api/auth/login", json={"username": "alice", "password": password})

    def _me(self, token: str) -> int:
        return self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code

    def _change(self, token: str, old: str = "alice-old-pw", new: str = "alice-new-pw"):
        return self.client.post(
            "/api/auth/password",
            json={"old_password": old, "new_password": new},
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_change_revokes_other_sessions_and_returns_fresh_token(self) -> None:
        tab_a = self._login().json()["token"]
        tab_b = self._login().json()["token"]
        self.assertEqual(decode_claims(tab_a), (self.alice_id, 0))

        res = self._change(tab_a)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["user"], {"id": self.alice_id, "username": "alice"})
        self.assertEqual(decode_claims(body["token"]), (self.alice_id, 1))

        self.assertEqual(self._me(body["token"]), 200)
        self.assertEqual(self._me(tab_a), 401)
        self.assertEqual(self._me(tab_b), 401)
        # ?token= 白名单路径同样校验版本
        self.assertEqual(self.client.get(f"/api/plans/1/events?token={tab_b}").status_code, 401)

        user = asyncio.run(self._user())
        self.assertEqual(user.token_version, 1)
        self.assertTrue(verify_password("alice-new-pw", user.password_hash))
        self.assertEqual(self._login("alice-old-pw").status_code, 401)
        relogin = self._login("alice-new-pw")
        self.assertEqual(relogin.status_code, 200)
        self.assertEqual(decode_claims(relogin.json()["token"]), (self.alice_id, 1))

    def test_open_sse_stream_ends_after_token_revoked(self) -> None:
        from drivecast.models import Plan, Topic
        from drivecast.web import api_plans

        async def _run():
            async with db.session() as s:
                t = Topic(title="t", direction="backend_arch", user_id=self.alice_id)
                s.add(t)
                await s.flush()
                p = Plan(ext_id="plan_sse", topic_id=t.id, title="t", status="active", user_id=self.alice_id)
                s.add(p)
                await s.commit()
                plan_id = p.id
            stream = api_plans._plan_events(plan_id, self.alice_id, 0)
            first = [await stream.__anext__(), await stream.__anext__()]  # retry + 首个 plan_status
            async with db.session() as s:
                (await s.get(User, self.alice_id)).token_version = 1
                await s.commit()
            rest = [chunk async for chunk in stream]  # 版本已变:下一轮直接结束
            return first, rest

        with mock.patch.object(asyncio, "sleep", new=mock.AsyncMock()):
            first, rest = asyncio.run(asyncio.wait_for(_run(), timeout=10))
        self.assertIn("plan_status", first[1])
        self.assertEqual(rest, [])

    def test_wrong_old_password_is_400_and_changes_nothing(self) -> None:
        token = self._login().json()["token"]
        res = self._change(token, old="not-my-password")
        # 不能用 401:前端会把 401 当作会话失效
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["detail"]["message"], "原密码错误")
        self.assertEqual(self._me(token), 200)
        self.assertEqual(asyncio.run(self._user()).token_version, 0)

    def test_new_password_rules(self) -> None:
        token = self._login().json()["token"]
        for new, message in (("short7!", "至少 8 位"), ("alice-old-pw", "不能与原密码相同")):
            res = self._change(token, new=new)
            self.assertEqual(res.status_code, 400)
            self.assertIn(message, res.json()["detail"]["message"])
        self.assertEqual(self._change(token, new="x" * 257).status_code, 422)
        self.assertEqual(asyncio.run(self._user()).token_version, 0)
        # 规则校验不消耗猜解名额
        self.assertNotIn(f"password|{self.alice_id}", api_auth._login_failures)

    def test_requires_authentication(self) -> None:
        res = self.client.post(
            "/api/auth/password", json={"old_password": "alice-old-pw", "new_password": "alice-new-pw"}
        )
        self.assertEqual(res.status_code, 401)
        token, _ = create_token(self.alice_id)
        # 写端点不接受 ?token=
        res = self.client.post(
            f"/api/auth/password?token={token}",
            json={"old_password": "alice-old-pw", "new_password": "alice-new-pw"},
        )
        self.assertEqual(res.status_code, 401)

    def test_repeated_wrong_old_password_locks_out(self) -> None:
        token = self._login().json()["token"]
        for _ in range(api_auth.LOGIN_LOCK_AFTER):
            self.assertEqual(self._change(token, old="guess-guess").status_code, 400)
        res = self._change(token)  # 正确原密码也被锁
        self.assertEqual(res.status_code, 429)
        self.assertGreater(int(res.headers["Retry-After"]), 0)
        self.assertEqual(asyncio.run(self._user()).token_version, 0)

    def test_version_raced_by_cli_reset_invalidates_request(self) -> None:
        token = self._login().json()["token"]
        user = asyncio.run(self._user())

        # 鉴权通过后、写库前被 CLI 抢先重置:条件更新落空,不得覆盖新密码
        real_verify = api_auth.verify_password

        def verify_then_race(password, stored):
            ok = real_verify(password, stored)
            with contextlib.closing(sqlite3.connect(settings.data_dir / "drivecast.db")) as conn:
                conn.execute("UPDATE user SET token_version = 5 WHERE id = ?", (self.alice_id,))
                conn.commit()
            return ok

        with mock.patch.object(api_auth, "verify_password", side_effect=verify_then_race):
            res = self._change(token)
        self.assertEqual(res.status_code, 401)
        after = asyncio.run(self._user())
        self.assertEqual(after.token_version, 5)
        self.assertEqual(after.password_hash, user.password_hash)

    def test_legacy_token_without_ver_is_version_zero(self) -> None:
        now = datetime.now(timezone.utc)
        legacy = jwt.encode(
            {"sub": str(self.alice_id), "iat": int(now.timestamp()), "exp": now + timedelta(days=1)},
            settings.auth_secret, algorithm="HS256",
        )
        self.assertEqual(decode_claims(legacy), (self.alice_id, 0))
        self.assertEqual(self._me(legacy), 200)
        self.assertEqual(self._change(legacy).status_code, 200)
        self.assertEqual(self._me(legacy), 401)

    def test_malformed_ver_is_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        for ver in ("0", 0.0, None, True):
            bad = jwt.encode(
                {"sub": str(self.alice_id), "ver": ver, "exp": now + timedelta(days=1)},
                settings.auth_secret, algorithm="HS256",
            )
            self.assertIsNone(decode_claims(bad), ver)
            self.assertEqual(self._me(bad), 401)

    def test_cli_reset_password_revokes_existing_tokens(self) -> None:
        token = self._login().json()["token"]
        with redirect_stdout(StringIO()) as out:
            asyncio.run(cmd_reset_password(Namespace(username="alice", password="cli-reset-pw")))
        self.assertIn("已失效", out.getvalue())
        self.assertEqual(self._me(token), 401)
        self.assertEqual(asyncio.run(self._user()).token_version, 1)
        fresh = self._login("cli-reset-pw").json()["token"]
        self.assertEqual(self._me(fresh), 200)


if __name__ == "__main__":
    unittest.main()

"""登录限流独立 unittest:真实哈希/临时数据库,不依赖主 app 或鉴权 override。"""
from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from drivecast.auth import decode_token, hash_password
from drivecast.models import Base, User
from drivecast.web import api_auth


class LoginStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.dict(api_auth._login_failures, {}, clear=True))
        self.clock = mock.Mock(return_value=10000.0)
        patcher = mock.patch.object(api_auth, "time", SimpleNamespace(monotonic=self.clock))
        patcher.start()
        self.addCleanup(patcher.stop)

    def record_failure(self, key: str = "peer|alice") -> None:
        self.assertEqual(api_auth._begin_login(key), 0)
        api_auth._finish_login(key, success=False)

    def test_lock_boundary_backoff_and_bounded_counter(self) -> None:
        for _ in range(api_auth.LOGIN_LOCK_AFTER):
            self.record_failure()
        state = api_auth._login_failures["peer|alice"]
        self.assertEqual(api_auth._begin_login("peer|alice"), api_auth.LOGIN_LOCK_BASE_SEC)
        self.clock.return_value = state.until - 0.001
        self.assertGreater(api_auth._begin_login("peer|alice"), 0)
        self.clock.return_value = state.until
        self.record_failure()
        self.assertEqual(state.until - self.clock(), api_auth.LOGIN_LOCK_BASE_SEC * 2)
        for _ in range(100):
            self.clock.return_value = state.until
            self.record_failure()
        self.assertEqual(state.fails, api_auth.LOGIN_LOCK_AFTER + api_auth._LOGIN_MAX_EXPONENT)
        self.assertEqual(state.until - self.clock(), api_auth.LOGIN_LOCK_MAX_SEC)

    def test_idle_ttl_boundary_and_success_clear(self) -> None:
        self.record_failure()
        state = api_auth._login_failures["peer|alice"]
        self.clock.return_value = state.expires_at - 0.001
        self.assertEqual(api_auth._begin_login("peer|alice"), 0)
        self.assertIs(api_auth._login_failures["peer|alice"], state)
        api_auth._finish_login("peer|alice", success=False)
        self.clock.return_value = state.expires_at
        self.assertEqual(api_auth._begin_login("peer|alice"), 0)
        self.assertIsNot(api_auth._login_failures["peer|alice"], state)
        self.assertEqual(api_auth._login_failures["peer|alice"].fails, 0)
        api_auth._finish_login("peer|alice", success=True)
        self.assertNotIn("peer|alice", api_auth._login_failures)

    def test_capacity_never_evicts_locks_or_in_flight(self) -> None:
        with mock.patch.object(api_auth, "LOGIN_MAX_KEYS", 2):
            for _ in range(api_auth.LOGIN_LOCK_AFTER):
                self.record_failure("locked")
            self.assertEqual(api_auth._begin_login("busy"), 0)
            locked = api_auth._login_failures["locked"]
            for i in range(100):
                self.assertGreater(api_auth._begin_login(f"new-{i}"), 0)
            self.assertEqual(len(api_auth._login_failures), 2)
            self.assertIs(api_auth._login_failures["locked"], locked)
            self.clock.return_value = locked.expires_at
            self.assertEqual(api_auth._begin_login("fresh"), 0)
            self.assertNotIn("locked", api_auth._login_failures)
            self.assertTrue(api_auth._login_failures["busy"].in_flight)
            self.assertGreater(api_auth._begin_login("busy"), 0)
            self.assertGreater(api_auth._begin_login("overflow"), 0)

    def test_active_lock_survives_shorter_ttl(self) -> None:
        with mock.patch.object(api_auth, "LOGIN_STATE_TTL_SEC", 1), \
                mock.patch.object(api_auth, "LOGIN_MAX_KEYS", 1):
            for _ in range(api_auth.LOGIN_LOCK_AFTER):
                self.record_failure()
            self.clock.return_value += 2
            self.assertGreater(api_auth._begin_login("peer|alice"), 0)
            self.assertGreater(api_auth._begin_login("new"), 0)

    def test_threaded_admission_is_atomic(self) -> None:
        with ThreadPoolExecutor(max_workers=16) as pool:
            waits = list(pool.map(lambda _: api_auth._begin_login("peer|alice"), range(100)))
        self.assertEqual(waits.count(0), 1)
        self.assertTrue(all(wait >= 1 for wait in waits if wait != 0))

    def test_missing_peer_and_forwarded_headers(self) -> None:
        request = Request({"type": "http", "headers": [(b"x-forwarded-for", b"attacker")]})
        self.assertEqual(api_auth._client_key(request, "alice"), "?|alice")


class LoginApiLimitsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.enterContext(mock.patch.dict(api_auth._login_failures, {}, clear=True))
        self.enterContext(mock.patch.object(api_auth, "_login_tasks", set()))
        self.enterContext(mock.patch("drivecast.config.settings.auth_secret", "login-limits-test-secret-only-not-for-deployment"))
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'test.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add(User(username="alice", password_hash=hash_password("correct-password")))
            await session.commit()
        self.session_patch = mock.patch.object(api_auth, "session", self.sessions)
        self.session_patch.start()
        self.clock = mock.Mock(return_value=10000.0)
        self.clock_patch = mock.patch.object(api_auth, "time", SimpleNamespace(monotonic=self.clock))
        self.clock_patch.start()
        app = FastAPI()
        app.include_router(api_auth.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.10", 1234)),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        if api_auth._login_tasks:
            await asyncio.gather(*list(api_auth._login_tasks), return_exceptions=True)
        await self.client.aclose()
        self.clock_patch.stop()
        self.session_patch.stop()
        api_auth._login_failures.clear()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def post(self, password: str = "wrong", xff: str = "198.51.100.1", username: str = "alice"):
        return await self.client.post(
            "/api/auth/login", json={"username": username, "password": password},
            headers={"X-Forwarded-For": xff, "Forwarded": f"for={xff}"},
        )

    async def test_rotating_xff_cannot_bypass_lock_expiry_and_success_clear(self) -> None:
        for i in range(api_auth.LOGIN_LOCK_AFTER):
            response = await self.post(xff=f"198.51.100.{i}")
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["detail"], {"code": 401, "message": "用户名或密码错误"})
        self.assertEqual(list(api_auth._login_failures), ["192.0.2.10|alice"])
        for password in ("wrong", "correct-password"):
            response = await self.post(password, xff="203.0.113.99")
            self.assertEqual(response.status_code, 429)
            self.assertGreater(int(response.headers["Retry-After"]), 0)
        self.clock.return_value = api_auth._login_failures["192.0.2.10|alice"].until
        response = await self.post("correct-password")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(decode_token(response.json()["token"]), response.json()["user"]["id"])
        self.assertNotIn("192.0.2.10|alice", api_auth._login_failures)
        self.assertEqual((await self.post()).status_code, 401)
        self.assertEqual(api_auth._login_failures["192.0.2.10|alice"].fails, 1)

    async def test_unknown_user_keeps_dummy_hash_and_uniform_failure(self) -> None:
        verify = api_auth.verify_password
        with mock.patch.object(api_auth, "verify_password", wraps=verify) as checked:
            absent = await self.post(username="nobody")
            self.assertEqual(checked.call_args.args[1], api_auth.DUMMY_HASH)
            wrong = await self.post()
        self.assertEqual(absent.status_code, 401)
        self.assertEqual(absent.json(), wrong.json())

    async def test_concurrent_requests_and_cancellation_hold_verification_slot(self) -> None:
        started = threading.Event()
        release = threading.Event()
        verify = api_auth.verify_password
        loop_thread = threading.get_ident()
        worker_threads = []

        def slow_verify(password, stored):
            worker_threads.append(threading.get_ident())
            started.set()
            if not release.wait(5):
                raise RuntimeError("test verification barrier timed out")
            return verify(password, stored)

        request = Request({"type": "http", "client": ("192.0.2.10", 1234), "headers": []})
        body = api_auth.LoginIn(username="alice", password="wrong")
        with mock.patch.object(api_auth, "verify_password", side_effect=slow_verify):
            first = asyncio.create_task(api_auth.login(body, request))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                responses = await asyncio.gather(*(self.post(xff=f"203.0.113.{i}") for i in range(20)))
                self.assertTrue(all(response.status_code == 429 for response in responses))
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertEqual((await self.post()).status_code, 429)
                self.assertTrue(api_auth._login_failures["192.0.2.10|alice"].in_flight)
            finally:
                release.set()
                await asyncio.gather(first, *list(api_auth._login_tasks), return_exceptions=True)
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], loop_thread)
        state = api_auth._login_failures["192.0.2.10|alice"]
        self.assertFalse(state.in_flight)
        self.assertEqual(state.fails, 1)
        self.assertEqual((await self.post()).status_code, 401)

    async def test_verification_exception_is_counted_and_releases_slot(self) -> None:
        with mock.patch.object(api_auth, "verify_password", side_effect=RuntimeError("hash unavailable")):
            with self.assertRaisesRegex(RuntimeError, "hash unavailable"):
                await self.post()
        state = api_auth._login_failures["192.0.2.10|alice"]
        self.assertFalse(state.in_flight)
        self.assertEqual(state.fails, 1)
        self.assertEqual((await self.post("correct-password")).status_code, 200)


if __name__ == "__main__":
    unittest.main()

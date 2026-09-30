"""运维:healthz / chunk 回收 / 磁盘水位 / logging 初始化。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from drivecast import db, ops
from drivecast.config import settings
from drivecast.jobs import worker
from drivecast.models import Episode, Job, Plan, Topic
from drivecast.tts.service import AUDIO_PIPELINE_VERSION
from drivecast.web import main as web_main

_OLD = time.time() - 10 * 86400


async def _seed_plan() -> int:
    await db.init_db()
    async with db.session() as s:
        topic = Topic(title="ops", direction="backend_arch")
        s.add(topic)
        await s.flush()
        plan = Plan(ext_id="p", topic_id=topic.id, title="p", status="active", episode_count=4)
        s.add(plan)
        await s.commit()
        return plan.id


async def _seed_episode(plan_id: int, ext_id: str, *, status="ready", final=True, mtime=_OLD,
                        version=AUDIO_PIPELINE_VERSION, seq=1) -> Path:
    async with db.session() as s:
        s.add(Episode(ext_id=ext_id, plan_id=plan_id, seq=seq, title=ext_id, status=status))
        await s.commit()
    chunks = settings.chunks_dir(ext_id)
    chunks.mkdir(parents=True)
    (chunks / "001.mp3").write_bytes(b"x")
    if final:
        path = settings.episode_audio_path(ext_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp3")
        os.utime(path, (mtime, mtime))
    meta = settings.meta_path(ext_id)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"pipeline_version": version}), "utf-8")
    return chunks


class _DataDirCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.enterContext(patch.object(settings, "data_dir", Path(self.tmp.name)))
        self.plan_id = await _seed_plan()

    async def asyncTearDown(self):
        await db.close_db()
        self.tmp.cleanup()
        worker._claimed_jobs.clear()
        worker._shutdown_jobs.clear()

    async def _episode(self, ext_id: str, **kwargs) -> Path:
        return await _seed_episode(self.plan_id, ext_id, **kwargs)


class ChunkGcTest(_DataDirCase):
    async def test_only_old_ready_idle_episodes_are_collected(self):
        old = await self._episode("old", seq=1)
        fresh = await self._episode("fresh", mtime=time.time(), seq=2)
        partial = await self._episode("partial", status="synth_partial", seq=3)
        no_final = await self._episode("nofinal", final=False, seq=4)
        busy = await self._episode("busy", seq=5)
        outdated = await self._episode("outdated", version=AUDIO_PIPELINE_VERSION - 1, seq=6)
        async with db.session() as s:
            s.add(Job(kind="quality_recheck", ref_id="busy", status="running",
                      payload=json.dumps({"plan_id": "p", "seq": 5})))
            await s.commit()

        self.assertEqual(await ops.gc_chunks(dry_run=True), ["old"])
        self.assertTrue(old.exists())
        self.assertEqual(await ops.gc_chunks(), ["old"])
        self.assertFalse(old.exists())
        self.assertTrue(settings.episode_audio_path("old").exists())
        for kept in (fresh, partial, no_final, busy, outdated):
            self.assertTrue(kept.exists(), kept)

    async def test_retention_zero_disables(self):
        chunks = await self._episode("old")
        with patch.object(settings, "chunks_retention_days", 0):
            self.assertEqual(await ops.gc_chunks(), [])
        self.assertTrue(chunks.exists())

    async def test_worker_idle_loop_runs_gc(self):
        chunks = await self._episode("old")
        with patch.object(settings, "worker_poll_interval_sec", 0.001):
            task = asyncio.create_task(worker._dispatch(forever=True))
            for _ in range(200):
                if not chunks.exists():
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(chunks.exists())

class DiskAlertTest(_DataDirCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.enterContext(patch.object(ops, "_last_disk_warn", None))
        self.enterContext(patch.object(ops, "_disk_low", False))

    async def test_low_disk_warns_once_per_hour_and_recovers(self):
        with patch.object(ops, "disk_free_mb", return_value=10.0), \
                self.assertLogs("drivecast.ops", "WARNING") as logs:
            self.assertFalse(ops.disk_ok())
            self.assertFalse(ops.disk_ok())
        self.assertEqual(len(logs.records), 1)
        with patch.object(ops, "disk_free_mb", return_value=10.0), \
                patch.object(ops, "_last_disk_warn", time.monotonic() - ops.DISK_WARN_INTERVAL_SEC - 1), \
                self.assertLogs("drivecast.ops", "WARNING"):
            ops.disk_ok()
        with patch.object(ops, "disk_free_mb", return_value=10.0 ** 6), \
                self.assertLogs("drivecast.ops", "INFO") as logs:
            self.assertTrue(ops.disk_ok())
        self.assertIn("恢复", logs.output[0])

    async def test_worker_skips_audio_lane_when_disk_low(self):
        async with db.session() as s:
            s.add(Episode(ext_id="e1", plan_id=self.plan_id, seq=1, title="e1"))
            s.add(Job(kind="tts", ref_id="e1", payload=json.dumps({"plan_id": "p", "seq": 1})))
            s.add(Job(kind="script", ref_id="e1", payload=json.dumps({"plan_id": "p", "seq": 1})))
            await s.commit()
        ran = []

        async def fake(job):
            ran.append(job.kind)
            await worker._finish(job.id, "done")

        with patch.dict(worker._EXECUTORS, script=fake, tts=fake), \
                patch.object(settings, "worker_poll_interval_sec", 0.001), \
                patch.object(ops, "disk_free_mb", return_value=1.0), \
                self.assertLogs("drivecast.ops", "WARNING"):
            task = asyncio.create_task(worker._dispatch(forever=True))
            for _ in range(200):
                if ran:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(ran, ["script"])
        async with db.session() as s:
            tts_status = await s.scalar(select(Job.status).where(Job.kind == "tts"))
        self.assertEqual(tts_status, "pending")


class ChunkGcCliTest(unittest.TestCase):
    def test_cli_gc_dry_run(self):
        from drivecast.__main__ import main
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(settings, "data_dir", Path(tmp)))
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)
        handlers = list(root.handlers)
        self.addCleanup(lambda: [root.removeHandler(h) for h in list(root.handlers) if h not in handlers])

        async def seed():
            await _seed_episode(await _seed_plan(), "old")
            await db.close_db()

        asyncio.run(seed())
        out = StringIO()
        with patch("sys.argv", ["drivecast", "gc", "--dry-run"]), redirect_stdout(out), \
                redirect_stderr(StringIO()):
            main()
        self.assertIn("old", out.getvalue())
        self.assertTrue(settings.chunks_dir("old").exists())


class HealthzTest(unittest.TestCase):
    def setUp(self):
        # cleanup 后进先出:先停 app(worker),再关 DB,最后删临时目录
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(settings, "data_dir", Path(tmp)))
        self.enterContext(patch.object(settings, "worker_poll_interval_sec", 0.01))
        self.addCleanup(lambda: asyncio.run(db.close_db()))

    @property
    def client(self) -> TestClient:
        if not hasattr(self, "_c"):
            self._c = self.enterContext(TestClient(web_main.app))
        return self._c

    def test_ok_without_auth_and_no_leaks(self):
        resp = self.client.get("/api/healthz")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body, {"status": "ok",
                                "checks": {"db": True, "worker": True, "disk": True, "queue": True}})

    def test_disk_low_is_degraded_200(self):
        with patch.object(ops, "disk_free_mb", return_value=1.0):
            resp = self.client.get("/api/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "degraded")
        self.assertFalse(resp.json()["checks"]["disk"])

    def test_stalled_queue_is_degraded(self):
        async def seed():
            await db.init_db()
            async with db.session() as s:
                s.add(Job(kind="script", ref_id="gone", created_at="2000-01-01 00:00:00",
                          payload=json.dumps({"plan_id": "gone", "seq": 1})))
                await s.commit()
            await db.close_db()
        asyncio.run(seed())
        # 让 worker 不认领,job 保持 pending
        with patch.object(worker, "_next_pending", return_value=None):
            body = self.client.get("/api/healthz").json()
        self.assertFalse(body["checks"]["queue"])
        self.assertEqual(body["status"], "degraded")

    def test_dead_worker_is_503(self):
        with patch.object(web_main, "worker_alive", return_value=False):
            resp = self.client.get("/api/healthz")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["status"], "down")
        with patch.object(web_main, "worker_alive", return_value=False), \
                patch.object(settings, "worker_enabled", False):
            self.assertEqual(self.client.get("/api/healthz").status_code, 200)

    def test_db_failure_is_503(self):
        async def broken():
            return False
        with patch.object(ops, "_db_ok", broken):
            resp = self.client.get("/api/healthz")
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(resp.json()["checks"]["db"])


class LoggingSetupTest(unittest.TestCase):
    def test_single_line_format_and_idempotent(self):
        from drivecast.logging_setup import setup_logging
        root = logging.getLogger()
        before = list(root.handlers), root.level
        try:
            setup_logging("INFO")
            setup_logging("INFO")
            ours = [h for h in root.handlers if getattr(h, "_drivecast_handler", False)]
            self.assertEqual(len(ours), 1)
            err = StringIO()
            with redirect_stderr(err):
                logging.getLogger("drivecast.jobs.worker").info("job#7 完成")
            line = err.getvalue().strip()
            self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ INFO drivecast\.jobs\.worker: job#7 完成$")
        finally:
            for h in list(root.handlers):
                if h not in before[0]:
                    root.removeHandler(h)
            root.setLevel(before[1])

    def test_access_log_token_redaction_still_installed(self):
        access = logging.getLogger("uvicorn.access")
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s "%s %s"',
                                   ("1.2.3.4", "GET", "/api/x?token=secret&a=1"), None)
        for f in access.filters:
            f.filter(record)
        self.assertNotIn("secret", record.getMessage())

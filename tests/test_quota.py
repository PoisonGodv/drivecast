"""每日额度 / 用量记账 / 跨用户公平调度;临时库,不调任何真实 LLM/TTS。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from auth_test_support import real_auth_scope
from drivecast import db, quota
from drivecast.auth import create_token, hash_password
from drivecast.config import settings
from drivecast.jobs import worker
from drivecast.models import Episode, Job, Plan, Topic, UsageEvent, User
from drivecast.planner import service as planner
from drivecast.schemas import EpisodeRec, PlanRec
from drivecast.web.main import app


def _plan_rec(ext: str, count: int, minutes: int) -> PlanRec:
    return PlanRec(
        plan_id=ext, title=ext, direction="backend_arch", user_topic=ext, target_minutes=minutes,
        episodes=[EpisodeRec(episode_id=f"{ext}_ep{i}", seq=i, title=f"第{i}集", target_minutes=minutes)
                  for i in range(1, count + 1)],
    )


class QuotaTest(unittest.TestCase):
    def setUp(self) -> None:
        real_auth_scope(self)
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(settings, "data_dir", Path(self.tmp)))
        self.enterContext(mock.patch.object(settings, "quota_daily_episode_minutes", 100))
        self.enterContext(mock.patch.object(settings, "quota_exempt_usernames", "admin"))
        self.addCleanup(lambda: asyncio.run(db.close_db()))
        asyncio.run(db.init_db())
        asyncio.run(self._seed())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self._as(self.alice_id)

    def _as(self, uid: int) -> None:
        token, _ = create_token(uid)
        self.client.headers["Authorization"] = f"Bearer {token}"

    async def _seed(self) -> None:
        async with db.session() as s:
            alice = User(username="alice", password_hash=hash_password("alice-test-pw"))
            bob = User(username="bob", password_hash=hash_password("bob-test-pw"))
            s.add_all([alice, bob])
            await s.flush()
            self.alice_id, self.bob_id = alice.id, bob.id
            topic = Topic(title="额度", direction="backend_arch", user_id=alice.id)
            s.add(topic)
            await s.flush()
            self.topic_id = topic.id
            plan = Plan(ext_id="plan_q", topic_id=topic.id, title="额度计划",
                        status="active", episode_count=3, user_id=alice.id)
            s.add(plan)
            await s.flush()
            self.plan_id = plan.id
            eps = [Episode(ext_id=f"plan_q_ep{i}", plan_id=plan.id, seq=i, title=f"第{i}集")
                   for i in (1, 2, 3)]
            s.add_all(eps)
            await s.commit()
            self.ep_ids = [e.id for e in eps]
        planner.save_plan(_plan_rec("plan_q", 3, 30))

    async def _add_jobs(self, *jobs: Job) -> None:
        async with db.session() as s:
            s.add_all(jobs)
            await s.commit()

    def _script_job(self, seq: int, status: str = "done", user_id: int | None = None) -> Job:
        return Job(kind="script", ref_id=f"plan_q_ep{seq}", status=status,
                   user_id=user_id or self.alice_id,
                   payload=json.dumps({"plan_id": "plan_q", "seq": seq}))

    # ---------- 额度 ----------

    def test_create_plan_over_quota_returns_429_with_remaining(self) -> None:
        resp = self.client.post("/api/plans", json={
            "topic_id": self.topic_id, "episode_count": 6, "target_minutes": 20})
        self.assertEqual(resp.status_code, 429)
        message = resp.json()["detail"]["message"]
        self.assertIn("剩余 100 分钟", message)
        self.assertIn("120", message)

        ok = self.client.post("/api/plans", json={
            "topic_id": self.topic_id, "episode_count": 4, "target_minutes": 20})
        self.assertEqual(ok.status_code, 201)
        # 大纲在途即预占 4×20,再开一个 40 分钟计划会超
        usage = self.client.get("/api/usage").json()
        self.assertEqual(usage["minutes_used"], 80)
        self.assertEqual(usage["minutes_remaining"], 20)
        async def _topic():
            async with db.session() as s:
                t = Topic(title="另一个", direction="backend_arch", user_id=self.alice_id)
                s.add(t)
                await s.commit()
                return t.id
        other = asyncio.run(_topic())
        again = self.client.post("/api/plans", json={
            "topic_id": other, "episode_count": 4, "target_minutes": 10})
        self.assertEqual(again.status_code, 429)

    def test_failed_and_cancelled_jobs_do_not_consume(self) -> None:
        asyncio.run(self._add_jobs(
            self._script_job(1, "done"), self._script_job(2, "failed"),
            self._script_job(3, "cancelled"),
        ))
        usage = self.client.get("/api/usage").json()
        self.assertEqual((usage["minutes_used"], usage["minutes_limit"], usage["minutes_remaining"]),
                         (30, 100, 70))

    def test_generate_episode_and_generate_all_enforce_quota(self) -> None:
        asyncio.run(self._add_jobs(self._script_job(1, "done"), self._script_job(1, "done"),
                                   self._script_job(1, "done")))  # 已用 90
        resp = self.client.post(f"/api/episodes/{self.ep_ids[1]}/generate")
        self.assertEqual(resp.status_code, 429)
        self.assertIn("剩余 10 分钟", resp.json()["detail"]["message"])

        # generate_all 只算将新建的:集 1 已有成品音频不计,集 2(上次失败)+集 3 共 60
        audio = settings.episode_audio_path("plan_q_ep1")
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"ID3")
        asyncio.run(self._add_jobs(Job(kind="script", ref_id="plan_q_ep2", status="failed",
                                       user_id=self.alice_id,
                                       payload=json.dumps({"plan_id": "plan_q", "seq": 2}))))
        with mock.patch.object(settings, "quota_daily_episode_minutes", 150):
            # 剩 60:集 2+3 共 60 → 放行
            ok = self.client.post(f"/api/plans/{self.plan_id}/generate_all")
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(self.client.get("/api/usage").json()["minutes_remaining"], 0)
            # 同集重复触发复用在途 job,不重复计额
            self.assertEqual(self.client.post(f"/api/episodes/{self.ep_ids[1]}/generate").status_code, 200)

    def test_regenerate_over_quota_keeps_artifacts(self) -> None:
        asyncio.run(self._add_jobs(self._script_job(1, "done"), self._script_job(2, "done"),
                                   self._script_job(3, "done")))
        script = settings.script_path("plan_q_ep1")
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("{}", "utf-8")
        resp = self.client.post(f"/api/episodes/{self.ep_ids[0]}/regenerate", json={"reason": "错"})
        self.assertEqual(resp.status_code, 429)
        self.assertTrue(script.exists())

    def test_retry_enforces_quota(self) -> None:
        async def _fail_plan():
            async with db.session() as s:
                p = await s.get(Plan, self.plan_id)
                p.status = "failed"
                s.add(Job(kind="plan", ref_id="plan_q", status="failed", user_id=self.alice_id,
                          payload=json.dumps({"plan_id": "plan_q", "episode_count": 6,
                                              "target_minutes": 20})))
                await s.commit()
        asyncio.run(_fail_plan())
        resp = self.client.post(f"/api/plans/{self.plan_id}/retry")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(asyncio.run(self._plan_status()), "failed")

    async def _plan_status(self) -> str:
        async with db.session() as s:
            return (await s.get(Plan, self.plan_id)).status

    def test_exempt_user_and_zero_limit_are_unlimited(self) -> None:
        asyncio.run(self._add_jobs(*(self._script_job(1, "done") for _ in range(5))))
        with mock.patch.object(settings, "quota_exempt_usernames", "admin, alice"):
            usage = self.client.get("/api/usage").json()
            self.assertIsNone(usage["minutes_remaining"])
            self.assertEqual(usage["minutes_used"], 150)
            self.assertEqual(self.client.post(f"/api/episodes/{self.ep_ids[1]}/generate").status_code, 200)
        with mock.patch.object(settings, "quota_daily_episode_minutes", 0):
            self.assertIsNone(self.client.get("/api/usage").json()["minutes_limit"])

    def test_quota_is_per_user(self) -> None:
        asyncio.run(self._add_jobs(*(self._script_job(1, "done") for _ in range(4))))
        self._as(self.bob_id)
        self.assertEqual(self.client.get("/api/usage").json()["minutes_remaining"], 100)

    # ---------- 用量记账 ----------

    def test_deleting_plan_does_not_refund_consumed_minutes(self) -> None:
        asyncio.run(self._add_jobs(self._script_job(1, "done"), self._script_job(2, "running"),
                                   self._script_job(3, "pending")))
        self.assertEqual(self.client.get("/api/usage").json()["minutes_used"], 90)
        self.assertEqual(self.client.delete(f"/api/plans/{self.plan_id}").status_code, 200)
        # done+running 已消耗 60 分钟保留;未开跑的 pending 退回
        usage = self.client.get("/api/usage").json()
        self.assertEqual((usage["minutes_used"], usage["minutes_remaining"]), (60, 40))

    def test_retry_with_existing_script_is_not_charged_again(self) -> None:
        asyncio.run(self._add_jobs(self._script_job(1, "done"), self._script_job(2, "done"),
                                   self._script_job(3, "done")))  # 已用 90,剩 10
        script = settings.script_path("plan_q_ep2")
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("{}", "utf-8")
        # 集 2 脚本已在(如 TTS 通道故障后重试):只重做 TTS,不再扣 30 分钟
        resp = self.client.post(f"/api/episodes/{self.ep_ids[1]}/generate")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.client.get("/api/usage").json()["minutes_used"], 90)
        with mock.patch.object(settings, "quota_daily_episode_minutes", 120):
            # generate_all:集 2 在途、集 1/3 脚本缺失 → 只计集 1+3 共 60 > 剩 30
            self.assertEqual(self.client.post(f"/api/plans/{self.plan_id}/generate_all").status_code, 429)
            for seq in (1, 3):
                path = settings.script_path(f"plan_q_ep{seq}")
                path.write_text("{}", "utf-8")
            self.assertEqual(self.client.post(f"/api/plans/{self.plan_id}/generate_all").status_code, 200)

    def test_usage_events_reported_and_cli(self) -> None:
        async def _record():
            await quota.record_usage(self.alice_id, "llm_tokens", 1200, "plan_q_ep1")
            await quota.record_usage(self.alice_id, "tts_chars", 3456, "plan_q_ep1")
            await quota.record_usage(self.alice_id, "llm_tokens", 0, "noop")  # 0 不落行
        asyncio.run(_record())
        asyncio.run(self._add_jobs(self._script_job(1, "done")))
        usage = self.client.get("/api/usage").json()
        self.assertEqual((usage["llm_tokens"], usage["tts_chars"]), (1200, 3456))

        from drivecast.__main__ import cmd_usage
        out = StringIO()
        with redirect_stdout(out):
            asyncio.run(cmd_usage(Namespace(days=3)))
        line = next(ln for ln in out.getvalue().splitlines() if "alice" in ln)
        self.assertEqual(line.split()[-3:], ["30", "1200", "3456"])

    def test_worker_records_script_tokens_and_tts_chars(self) -> None:
        async def _write(plan, ep):
            return {"usage_tokens": 777}
        async def _synth(script, album=""):
            return {"char_count": 4321}
        job = self._script_job(1, "running")
        tts_job = Job(kind="tts", ref_id="plan_q_ep1", status="running", user_id=self.alice_id,
                      payload=json.dumps({"plan_id": "plan_q", "seq": 1}))
        asyncio.run(self._add_jobs(job, tts_job))
        with mock.patch.object(worker.scriptwriter, "write_script", _write), \
                mock.patch.object(worker.scriptwriter, "load_script", return_value={"episode_id": "plan_q_ep1"}), \
                mock.patch.object(worker.tts, "synthesize_episode", _synth):
            asyncio.run(worker._run_script_job(job))
            asyncio.run(worker._run_tts_job(tts_job))

        async def _events():
            async with db.session() as s:
                return [(e.user_id, e.kind, e.amount, e.ref_id)
                        for e in (await s.scalars(select(UsageEvent).order_by(UsageEvent.id))).all()]
        self.assertEqual(asyncio.run(_events()), [
            (self.alice_id, "llm_tokens", 777, "plan_q_ep1"),
            (self.alice_id, "tts_chars", 4321, "plan_q_ep1"),
        ])

    # ---------- 公平调度 ----------

    def test_user_b_single_job_claimed_before_user_a_second_plan(self) -> None:
        async def _seed_and_claim():
            async with db.session() as s:
                for ext, uid in (("plan_a1", self.alice_id), ("plan_a2", self.alice_id),
                                 ("plan_b1", self.bob_id)):
                    s.add(Plan(ext_id=ext, topic_id=self.topic_id, title=ext, status="active",
                               episode_count=1, user_id=uid))
                # A 先排两个计划,B 后排一个
                for ext, uid in (("plan_a1", self.alice_id), ("plan_a2", self.alice_id),
                                 ("plan_b1", self.bob_id)):
                    s.add(Job(kind="script", ref_id=f"{ext}_ep1", user_id=uid,
                              payload=json.dumps({"plan_id": ext, "seq": 1})))
                await s.commit()
            first = await worker._next_pending(("plan", "script"))
            second = await worker._next_pending(("plan", "script"), {"plan_a1"})
            third = await worker._next_pending(("plan", "script"), {"plan_a1", "plan_b1"})
            return first.ref_id, second.ref_id, third.ref_id
        self.assertEqual(asyncio.run(_seed_and_claim()),
                         ("plan_a1_ep1", "plan_b1_ep1", "plan_a2_ep1"))

    def test_fair_order_prefers_fewest_running_then_earliest(self) -> None:
        a1, a2, b1, c1 = (Job(id=i, user_id=u) for i, u in ((1, 1), (2, 1), (5, 2), (9, 3)))
        order = worker._fair_order([a1, a2, b1, c1], {1: 1, 2: 1})
        self.assertEqual([j.id for j in order], [9, 1, 2, 5])


if __name__ == "__main__":
    unittest.main()

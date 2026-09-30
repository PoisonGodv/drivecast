"""大纲确认流:draft 期可删改,确认后才转 active 入队写稿;跨用户一律 404。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from auth_test_support import real_auth_scope
from drivecast import db
from drivecast.auth import create_token, hash_password
from drivecast.config import settings
from drivecast.jobs import worker
from drivecast.models import Episode, Job, Plan, Topic, User
from drivecast.planner import service as planner
from drivecast.schemas import EpisodeRec, PlanRec
from drivecast.web.main import app


class OutlineConfirmTest(unittest.TestCase):
    def setUp(self) -> None:
        real_auth_scope(self)
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(settings, "data_dir", Path(self.tmp)))
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
            topic = Topic(title="分布式锁", direction="backend_arch", user_id=alice.id)
            s.add(topic)
            await s.flush()
            plan = Plan(ext_id="plan_20260930_01", topic_id=topic.id, title="分布式锁",
                        status="generating", episode_count=0, user_id=alice.id)
            s.add(plan)
            await s.commit()
            self.plan_id = plan.id
        rec = PlanRec(
            plan_id="plan_20260930_01", title="分布式锁速成", direction="backend_arch",
            user_topic="分布式锁", created_at="2026-09-30T08:00:00",
            episodes=[
                EpisodeRec(episode_id=f"ep_20260930_01_{i:02d}", seq=i, title=f"第{i}集",
                           goal="g", objectives=["o"], outline="大纲",
                           depends_on=[f"ep_20260930_01_{i - 1:02d}"] if i > 1 else [],
                           scheduled_date=f"2026-09-{29 + i:02d}" if i < 2 else "2026-10-01")
                for i in (1, 2, 3)
            ],
        )
        planner.save_plan(rec)
        from drivecast.persist import upsert_plan
        await upsert_plan(rec)

    async def _rows(self, model, *where):
        async with db.session() as s:
            return list((await s.execute(select(model).where(*where))).scalars().all())

    def _episodes(self) -> list[Episode]:
        return sorted(asyncio.run(self._rows(Episode)), key=lambda e: e.seq)

    def _plan(self) -> Plan:
        return asyncio.run(self._rows(Plan, Plan.id == self.plan_id))[0]

    def test_outline_lands_in_draft_and_generation_is_blocked(self) -> None:
        self.assertEqual(self._plan().status, "draft")
        detail = self.client.get(f"/api/plans/{self.plan_id}").json()
        self.assertEqual(detail["status"], "draft")
        self.assertEqual(self.client.post(f"/api/plans/{self.plan_id}/generate_all").status_code, 409)
        ep_id = self._episodes()[0].id
        self.assertEqual(self.client.post(f"/api/episodes/{ep_id}/generate").status_code, 409)
        self.assertEqual(asyncio.run(self._rows(Job)), [])

    def test_edit_delete_renumber_then_confirm(self) -> None:
        eps = self._episodes()
        res = self.client.patch(f"/api/plans/{self.plan_id}/outline", json={
            "title": "新标题",
            "episodes": [{"id": eps[0].id, "title": " 改后第一集 ", "goal": "新目标",
                          "objectives": ["a", " ", "b"]},
                         {"id": eps[2].id, "outline": "新大纲"}],
            "delete": [eps[1].id],
        })
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual([e["seq"] for e in body["episodes"]], [1, 2])
        self.assertEqual([e["ext_id"] for e in body["episodes"]],
                         ["ep_20260930_01_01", "ep_20260930_01_03"])

        rec = planner.load_plan("plan_20260930_01")
        self.assertEqual(rec.title, "新标题")
        self.assertEqual([(e.episode_id, e.seq) for e in rec.episodes],
                         [("ep_20260930_01_01", 1), ("ep_20260930_01_03", 2)])
        self.assertEqual(rec.episodes[0].title, "改后第一集")
        self.assertEqual(rec.episodes[0].goal, "新目标")
        self.assertEqual(rec.episodes[0].objectives, ["a", "b"])
        self.assertEqual(rec.episodes[1].outline, "新大纲")
        self.assertEqual(rec.episodes[1].depends_on, ["ep_20260930_01_01"])
        self.assertEqual(rec.episodes[1].scheduled_date, "2026-10-01")

        eps = self._episodes()
        self.assertEqual([(e.ext_id, e.seq) for e in eps],
                         [("ep_20260930_01_01", 1), ("ep_20260930_01_03", 2)])
        self.assertEqual(eps[0].title, "改后第一集")
        self.assertEqual(json.loads(eps[0].objectives), ["a", "b"])
        plan = self._plan()
        self.assertEqual((plan.title, plan.episode_count, plan.status), ("新标题", 2, "draft"))

        res = self.client.post(f"/api/plans/{self.plan_id}/confirm")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(len(res.json()["job_ids"]), 2)
        self.assertEqual(self._plan().status, "active")
        jobs = asyncio.run(self._rows(Job))
        self.assertEqual(sorted((j.ref_id, j.kind, j.user_id) for j in jobs), [
            ("ep_20260930_01_01", "script", self.alice_id),
            ("ep_20260930_01_03", "script", self.alice_id),
        ])
        seqs = sorted(json.loads(j.payload)["seq"] for j in jobs)
        self.assertEqual(seqs, [1, 2])

    def test_cannot_delete_every_episode_or_reference_foreign_ids(self) -> None:
        ids = [e.id for e in self._episodes()]
        res = self.client.patch(f"/api/plans/{self.plan_id}/outline", json={"delete": ids})
        self.assertEqual(res.status_code, 422)
        res = self.client.patch(f"/api/plans/{self.plan_id}/outline",
                                json={"episodes": [{"id": 9999, "title": "x"}]})
        self.assertEqual(res.status_code, 422)
        res = self.client.patch(f"/api/plans/{self.plan_id}/outline",
                                json={"episodes": [{"id": ids[0], "title": "  "}]})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(len(self._episodes()), 3)

    def test_confirm_is_idempotent_and_locks_outline(self) -> None:
        self.assertEqual(self.client.post(f"/api/plans/{self.plan_id}/confirm").status_code, 200)
        self.assertEqual(self.client.post(f"/api/plans/{self.plan_id}/confirm").status_code, 409)
        self.assertEqual(len(asyncio.run(self._rows(Job))), 3)
        res = self.client.patch(f"/api/plans/{self.plan_id}/outline",
                                json={"episodes": [{"id": self._episodes()[0].id, "title": "x"}]})
        self.assertEqual(res.status_code, 409)

    def test_cross_user_is_404(self) -> None:
        self._as(self.bob_id)
        ep_id = self._episodes()[0].id
        self.assertEqual(self.client.get(f"/api/plans/{self.plan_id}/outline").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/plans/{self.plan_id}/outline",
                                           json={"episodes": [{"id": ep_id, "title": "x"}]}).status_code, 404)
        self.assertEqual(self.client.post(f"/api/plans/{self.plan_id}/confirm").status_code, 404)
        self.assertEqual(self.client.post("/api/plans/9999/confirm").status_code, 404)
        self.assertEqual(self._plan().status, "draft")
        self.assertEqual(self._episodes()[0].title, "第1集")

    def test_get_outline_returns_editable_fields(self) -> None:
        res = self.client.get(f"/api/plans/{self.plan_id}/outline")
        self.assertEqual(res.status_code, 200, res.text)
        ep = res.json()["episodes"][0]
        self.assertEqual((ep["goal"], ep["outline"], ep["objectives"]), ("g", "大纲", ["o"]))
        self.assertIsInstance(ep["id"], int)

    def test_draft_plan_rejects_episode_enqueue(self) -> None:
        from drivecast.jobs import pipeline

        with self.assertRaises(RuntimeError):
            asyncio.run(pipeline.enqueue_episode("plan_20260930_01", 1, "ep_20260930_01_01"))

    def test_draft_plan_is_alive_for_worker_and_deletable(self) -> None:
        async def _scenario() -> None:
            async with db.session() as s:
                refs = await worker._alive_ref_ids(s)
            self.assertIn("plan_20260930_01", refs)
            self.assertTrue(await worker._plan_alive("plan_20260930_01"))

        asyncio.run(_scenario())
        res = self.client.delete(f"/api/plans/{self.plan_id}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["deleted_episodes"], 3)
        self.assertFalse(settings.plan_path("plan_20260930_01").exists())

    def test_resumed_plan_job_on_draft_does_not_enqueue(self) -> None:
        # 模拟大纲落库后、plan job 收尾前进程崩溃:孤儿复位后重跑该 job
        async def _scenario() -> None:
            async with db.session() as s:
                s.add(Job(kind="plan", ref_id="plan_20260930_01", user_id=self.alice_id,
                          payload=json.dumps({"plan_id": "plan_20260930_01", "topic": "分布式锁",
                                              "direction": "backend_arch"})))
                await s.commit()
            job = await worker._next_pending()
            self.assertIsNotNone(job)
            await worker._execute_one(job)

        asyncio.run(_scenario())
        jobs = asyncio.run(self._rows(Job))
        self.assertEqual([(j.kind, j.status) for j in jobs], [("plan", "done")])
        self.assertEqual(self._plan().status, "draft")


if __name__ == "__main__":
    unittest.main()

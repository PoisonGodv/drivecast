"""Tenant isolation through real Bearer authentication and temporary databases.

Cross-user IDs must look absent; lists, review state, creation and planner
coverage remain scoped even when users share a knowledge-point identifier.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime
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
from drivecast.content.loader import get_question_bank
from drivecast.jobs import pipeline
from drivecast.library import service
from drivecast.models import Audio, Episode, Job, Plan, Question, ReviewReplay, Topic, User
from drivecast.web.main import app


class IsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        real_auth_scope(self)
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(settings, "data_dir", Path(self.tmp)))
        self.enterContext(mock.patch.object(service, "_now", lambda: datetime(2026, 8, 20, 9)))
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
        highs = [q["id"] for q in get_question_bank("backend_arch") if q["frequency"] == "high"]
        audio_a, audio_b = (settings.episode_audio_path(e) for e in ("ep_a", "ep_b"))
        for path in (audio_a, audio_b):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"ID3" + b"isolation-bytes" * 8)
        async with db.session() as s:
            alice = User(username="alice", password_hash=hash_password("alice-test-pw"))
            bob = User(username="bob", password_hash=hash_password("bob-test-pw"))
            s.add_all([alice, bob])
            await s.flush()
            self.alice_id, self.bob_id = alice.id, bob.id
            topic_a = Topic(title="A 的主题", direction="backend_arch", user_id=alice.id)
            topic_b = Topic(title="B 的主题", direction="backend_arch", user_id=bob.id)
            s.add_all([topic_a, topic_b])
            await s.flush()
            plan_a = Plan(ext_id="plan_a", topic_id=topic_a.id, title="A 计划",
                          status="active", episode_count=1, user_id=alice.id)
            plan_b = Plan(ext_id="plan_b", topic_id=topic_b.id, title="B 计划",
                          status="active", episode_count=1, user_id=bob.id)
            s.add_all([plan_a, plan_b])
            await s.flush()
            ep_a = Episode(ext_id="ep_a", plan_id=plan_a.id, seq=1, title="A 集",
                           status="ready", scheduled_date="2026-08-20", covers=json.dumps(highs[:1]))
            ep_b = Episode(ext_id="ep_b", plan_id=plan_b.id, seq=1, title="B 集",
                           status="ready", scheduled_date="2026-08-20")
            s.add_all([ep_a, ep_b])
            await s.flush()
            s.add_all([
                Audio(episode_id=ep_a.id, file_path=str(audio_a), duration_sec=600,
                      size_bytes=audio_a.stat().st_size),
                Audio(episode_id=ep_b.id, file_path=str(audio_b), duration_sec=500,
                      size_bytes=audio_b.stat().st_size),
            ])
            questions_a = [Question(episode_id=ep_a.id, kp_id="kp_shared", question=f"a{i}", framework="[]")
                           for i in range(2)]
            questions_b = [Question(episode_id=ep_b.id, kp_id="kp_shared", question=f"b{i}", framework="[]")
                           for i in range(2)]
            s.add_all(questions_a + questions_b)
            await s.commit()
            self.plan_a_id, self.plan_b_id = plan_a.id, plan_b.id
            self.ep_a_id, self.ep_b_id = ep_a.id, ep_b.id
            self.question_a_id, self.question_b_id = questions_a[0].id, questions_b[0].id
        self.job_a_id = await pipeline.enqueue_episode("plan_a", 1, "ep_a", user_id=self.alice_id)
        self.job_b_id = await pipeline.enqueue_episode("plan_b", 1, "ep_b", user_id=self.bob_id)

    def test_plan_access_isolated(self) -> None:
        self._as(self.bob_id)
        res = self.client.get(f"/api/plans/{self.plan_a_id}")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["detail"]["message"], "plan not found")
        self.assertEqual(self.client.delete(f"/api/plans/{self.plan_a_id}").status_code, 404)
        self.assertEqual(self.client.post(f"/api/plans/{self.plan_a_id}/generate_all").status_code, 404)
        self.assertEqual(self.client.get(f"/api/plans/{self.plan_b_id}").status_code, 200)

    def test_plan_list_only_own(self) -> None:
        for uid, plan_id in ((self.bob_id, self.plan_b_id), (self.alice_id, self.plan_a_id)):
            self._as(uid)
            self.assertEqual([r["id"] for r in self.client.get("/api/plans").json()], [plan_id])

    def test_episode_endpoints_cross_user_404(self) -> None:
        self._as(self.bob_id)
        cases = [("get", "audio/meta", None), ("get", "audio", None),
                 ("get", "audio/download", None), ("get", "script", None),
                 ("post", "progress", {"position_sec": 5}), ("post", "generate", None),
                 ("post", "audio/recheck", None),
                 ("post", "regenerate", {"reason": "x"}), ("post", "qc", {"passed": True})]
        for method, suffix, body in cases:
            path = f"/api/episodes/{self.ep_a_id}/{suffix}"
            res = getattr(self.client, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(res.status_code, 404, path)
        self.assertEqual(self.client.get(f"/api/episodes/{self.ep_b_id}/audio").status_code, 200)
        self.assertEqual(self.client.post(f"/api/episodes/{self.ep_b_id}/progress",
                                         json={"position_sec": 10}).status_code, 200)

    def test_job_access_isolated(self) -> None:
        self._as(self.bob_id)
        self.assertEqual(self.client.get(f"/api/jobs/{self.job_a_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/jobs/{self.job_b_id}").status_code, 200)
        self.assertEqual(self.client.get("/api/jobs/999999").status_code, 404)

    def test_sse_cross_user_emits_not_found(self) -> None:
        from drivecast.web.api_plans import _plan_events

        async def frames(uid: int, limit: int) -> list[str]:
            gen = _plan_events(self.plan_a_id, uid)
            out = []
            try:
                for _ in range(limit):
                    try:
                        out.append(await asyncio.wait_for(gen.__anext__(), timeout=5))
                    except StopAsyncIteration:
                        break
            finally:
                await gen.aclose()
            return out

        bob = asyncio.run(frames(self.bob_id, 5))
        self.assertTrue(bob[0].startswith("retry:"))
        self.assertIn('"status": "not_found"', "".join(bob))
        self.assertEqual(len(bob), 2)
        alice = asyncio.run(frames(self.alice_id, 3))
        self.assertTrue(alice[0].startswith("retry:"))
        self.assertNotIn("not_found", "".join(alice))
        self.assertIn('"status": "active"', "".join(alice))
        self.assertIn('"episode_id": %d' % self.ep_a_id, "".join(alice))

    def test_review_queries_isolated(self) -> None:
        self._as(self.bob_id)
        for path in ("/api/review/today", "/api/review/questions", "/api/review/weak-points"):
            rows = self.client.get(path).json()
            self.assertTrue(rows, path)
            for row in rows:
                if "plan_id" in row:
                    self.assertEqual(row["plan_id"], self.plan_b_id, path)
                if "recommended_episode" in row:
                    self.assertEqual(row["recommended_episode"]["plan_id"], self.plan_b_id, path)
        weak = self.client.get("/api/review/weak-points").json()
        self.assertEqual([r["kp_id"] for r in weak], ["kp_shared"])
        self.assertEqual(weak[0]["recommended_episode"]["episode_id"], self.ep_b_id)
        daily = self.client.get("/api/review/daily").json()
        self.assertEqual([ep["plan_id"] for ep in daily["new_episodes"]], [self.plan_b_id])
        self.assertEqual(daily["due_count"], 0)
        self.assertEqual(daily["new_count"], 2)

    def test_grade_question_cross_user_404(self) -> None:
        self._as(self.bob_id)
        for qid, status in ((self.question_a_id, 404), (self.question_b_id, 200)):
            self.assertEqual(self.client.post(f"/api/questions/{qid}/grade",
                                             json={"mastery": "mastered"}).status_code, status)

    def test_same_kp_replay_coexists_per_user(self) -> None:
        for uid, qid in ((self.alice_id, self.question_a_id), (self.bob_id, self.question_b_id)):
            self._as(uid)
            self.assertEqual(self.client.post(f"/api/questions/{qid}/grade",
                                             json={"mastery": "unknown"}).status_code, 200)

        async def replays():
            async with db.session() as s:
                rows = (await s.scalars(select(ReviewReplay).order_by(ReviewReplay.id))).all()
                return [(r.user_id, r.kp_id, r.episode_id) for r in rows]

        rows = asyncio.run(replays())
        self.assertEqual(len(rows), 2, "同 kp_id 同日两用户必须可共存")
        self.assertEqual(sorted(rows), [(self.alice_id, "kp_shared", self.ep_a_id),
                                        (self.bob_id, "kp_shared", self.ep_b_id)])
        for uid, eid in ((self.alice_id, self.ep_a_id), (self.bob_id, self.ep_b_id)):
            self._as(uid)
            queue = self.client.get("/api/review/replay-queue").json()
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]["episode_id"], eid)

    def test_interview_date_and_sprint_isolated(self) -> None:
        self._as(self.alice_id)
        self.assertEqual(self.client.put("/api/review/interview-date",
                                         json={"interview_date": "2026-08-25"}).status_code, 200)
        self.assertEqual(self.client.get("/api/review/sprint").status_code, 200)
        self._as(self.bob_id)
        self.assertIsNone(self.client.get("/api/review/interview-date").json()["interview_date"])
        self.assertEqual(self.client.get("/api/review/sprint").status_code, 409)

    def test_coverage_only_counts_own_covers(self) -> None:
        real_loader = service.get_question_bank
        for uid, covered in ((self.alice_id, 1), (self.bob_id, 0)):
            self._as(uid)
            with mock.patch.object(service, "get_question_bank",
                                   side_effect=lambda d: real_loader(d) if d == "backend_arch" else []):
                result = self.client.get("/api/review/coverage").json()
            self.assertEqual(result["directions"][0]["covered"], covered)

    def test_create_flow_writes_user_id(self) -> None:
        self._as(self.bob_id)
        res = self.client.post("/api/topics", json={"title": "B 新主题", "direction": "ai_llm"})
        self.assertEqual(res.status_code, 201)
        topic_id = res.json()["id"]
        created = self.client.post("/api/plans", json={"topic_id": topic_id})
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["status"], "pending")

        async def owners():
            async with db.session() as s:
                plan = await s.get(Plan, body["plan_id"])
                job = await s.get(Job, body["job_id"])
                topic = await s.get(Topic, topic_id)
                return plan.user_id, job.user_id, topic.user_id

        self.assertEqual(asyncio.run(owners()), (self.bob_id,) * 3)
        self.assertEqual(self.client.get(f"/api/plans/{body['plan_id']}").status_code, 200)
        self._as(self.alice_id)
        self.assertEqual(self.client.get(f"/api/plans/{body['plan_id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/jobs/{body['job_id']}").status_code, 404)
        res = self.client.post("/api/plans", json={"topic_id": topic_id})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["detail"]["message"], "topic not found")


class CoveredQuestionScopingTest(unittest.TestCase):
    """Shared plan directory must not leak another user's grounding coverage."""
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(settings, "data_dir", Path(tmp)))
        self.addCleanup(lambda: asyncio.run(db.close_db()))
        asyncio.run(db.init_db())
        asyncio.run(self._seed())

    async def _seed(self) -> None:
        plans_dir = settings.data_dir / "plans"
        plans_dir.mkdir(parents=True)
        async with db.session() as s:
            self.user_ids = []
            for username, covers in (("alice", ["q1", "q2"]), ("bob", ["q3"])):
                user = User(username=username, password_hash=hash_password(f"{username}-test-pw"))
                s.add(user)
                await s.flush()
                self.user_ids.append(user.id)
                topic = Topic(title=username, direction="backend_arch", user_id=user.id)
                s.add(topic)
                await s.flush()
                ext_id = f"plan_{username}"
                s.add(Plan(ext_id=ext_id, topic_id=topic.id, title=username, user_id=user.id))
                (plans_dir / f"{ext_id}.json").write_text(
                    json.dumps({"episodes": [{"covers": covers}]}), "utf-8")
            await s.commit()

    def test_covered_ids_scoped_by_user(self) -> None:
        from drivecast.planner import service as planner
        self.assertEqual(asyncio.run(planner._covered_question_ids(self.user_ids[0])), {"q1", "q2"})
        self.assertEqual(asyncio.run(planner._covered_question_ids(self.user_ids[1])), {"q3"})

    async def _grounding(self, uid: int) -> str:
        from drivecast.planner import service as planner
        questions = [
            {"id": "q1", "question": "甲题", "topic": ["T"], "frequency": "high"},
            {"id": "q3", "question": "乙题", "topic": ["T"], "frequency": "high"},
        ]
        with mock.patch.object(planner, "get_question_bank", return_value=questions):
            return await planner._grounding_prompt("backend_arch", uid)

    def test_grounding_marks_only_own_coverage(self) -> None:
        p1 = asyncio.run(self._grounding(self.user_ids[0]))
        p2 = asyncio.run(self._grounding(self.user_ids[1]))
        self.assertIn("甲题 [已覆盖]", p1)
        self.assertNotIn("乙题 [已覆盖]", p1)
        self.assertIn("乙题 [已覆盖]", p2)
        self.assertNotIn("甲题 [已覆盖]", p2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivecast import db
from drivecast.config import settings
from drivecast.models import Audio, Episode, Job, Plan, Question, Topic
from drivecast.web.main import app


class WebContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        asyncio.run(db.init_db())
        self.audio_path = settings.data_dir / "audio" / "episodes" / "ep_web.mp3"
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        self.audio_path.write_bytes(b"ID3" + b"audio-bytes" * 32)
        asyncio.run(self._seed())
        self.client = TestClient(app)

    def tearDown(self) -> None:
        asyncio.run(db.close_db())
        settings.data_dir = self.old_data_dir
        self.tmp.cleanup()

    async def _seed(self) -> None:
        async with db.session() as s:
            topic = Topic(title="Web 契约", direction="backend_arch")
            s.add(topic)
            await s.flush()
            plan = Plan(
                ext_id="plan_web", topic_id=topic.id, title="重复标题也不靠标题找 ID",
                status="active", episode_count=1,
            )
            s.add(plan)
            await s.flush()
            episode = Episode(
                ext_id="ep_web", plan_id=plan.id, seq=1, title="音频契约",
                status="ready",
            )
            s.add(episode)
            await s.flush()
            meta = {
                "pipeline_version": 6,
                "duration_sec": 600.0,
                "target_minutes": 9,
                "chunk_count": 5,
                "chapters": [
                    {"title": "开场", "start_sec": 0.0, "end_sec": 120.0},
                    {"title": "核心", "start_sec": 120.0},
                ],
                "quality": {
                    "status": "warning",
                    "failed_checks": ["target_duration_error"],
                    "checks": {
                        "duration_range": {"passed": True},
                        "target_duration_error": {"passed": False},
                        "peak": {"max_volume_db": -1.2, "passed": True},
                        "chunks": {"expected": 5, "passed": True},
                    },
                },
                "speech_rate_calibration": {
                    "observed_words_per_minute": 288.2,
                    "history_samples": 4,
                    "recommended_words_per_minute": 292.5,
                    "coefficient_vs_config": 0.975,
                    "env_mutated": False,
                },
            }
            s.add(Audio(
                episode_id=episode.id,
                file_path=str(self.audio_path),
                duration_sec=600,
                size_bytes=self.audio_path.stat().st_size,
                meta_json=json.dumps(meta, ensure_ascii=False),
                quality_status="warning",
            ))
            s.add_all([
                Question(episode_id=episode.id, kp_id="kp_web", question="q1", framework="[]"),
                Question(episode_id=episode.id, kp_id="kp_web", question="q2", framework="[]"),
            ])
            await s.commit()

    def test_review_contracts_expose_numeric_plan_id(self) -> None:
        today = self.client.get("/api/review/today")
        self.assertEqual(today.status_code, 200)
        self.assertEqual(today.json()[0]["plan_id"], 1)

        weak = self.client.get("/api/review/weak-points")
        self.assertEqual(weak.status_code, 200)
        self.assertEqual(weak.json()[0]["recommended_episode"]["plan_id"], 1)

        replay = self.client.get("/api/review/replay-queue")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()[0]["plan_id"], 1)

        self.client.put("/api/review/interview-date", json={"interview_date": "2026-08-25"})
        sprint = self.client.get("/api/review/sprint")
        self.assertEqual(sprint.status_code, 200)
        self.assertEqual(sprint.json()["directions"][0]["episodes"][0]["plan_id"], 1)

    def test_audio_meta_uses_persisted_snapshot_and_normalizes_m6(self) -> None:
        response = self.client.get("/api/episodes/1/audio/meta")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([chapter["title"] for chapter in payload["chapters"]], ["开场", "核心"])
        self.assertEqual(payload["quality"]["checks"], {
            "duration": True,
            "clipping": True,
            "chunks": True,
        })
        self.assertEqual(payload["quality"]["failed_checks"], ["target_duration_error"])
        self.assertEqual(payload["quality"]["status"], "warning")
        self.assertTrue(payload["quality"]["messages"])
        estimate = payload["duration_estimate"]
        self.assertEqual(estimate["estimated_sec"], 540.0)
        self.assertEqual(estimate["error_percent"], 11.1)
        self.assertEqual(estimate["recommended_words_per_min"], 292.5)
        self.assertEqual(estimate["coefficient_vs_config"], 0.975)
        self.assertFalse(estimate["env_mutated"])

    def test_legacy_quality_failures_never_infer_passed(self) -> None:
        from drivecast.web.api_audio import _quality

        payload = _quality({
            "chunk_count": 2,
            "quality": {
                "failed_checks": ["target_duration_error"],
                "checks": {
                    "duration_range": {"passed": True},
                    "peak": {"max_volume_db": -1.0, "passed": True},
                    "chunks": {"expected": 2, "passed": True},
                },
            },
        }, 600.0)
        self.assertEqual(payload["status"], "warning")
        self.assertTrue(payload["messages"])

    def test_corrupt_file_meta_falls_back_to_db_and_old_audio_streams(self) -> None:
        meta_path = settings.meta_path("ep_web")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text("{broken", "utf-8")
        response = self.client.get("/api/episodes/1/audio/meta")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["chapters"][0]["title"], "开场")

        stream = self.client.get("/api/episodes/1/audio", headers={"Range": "bytes=0-4"})
        self.assertEqual(stream.status_code, 206)
        self.assertEqual(stream.headers["content-disposition"].split(";")[0], "inline")
        self.assertEqual(stream.content, self.audio_path.read_bytes()[:5])

        download = self.client.get("/api/episodes/1/audio/download")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.headers["content-disposition"].split(";")[0], "attachment")


class PlanLifecycleContractsTest(unittest.TestCase):
    """计划取消/恢复、播放位置钳制、音频路径自愈的 Web 契约。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        asyncio.run(db.init_db())
        self.audio_path = settings.episode_audio_path("ep_c1")
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        self.audio_path.write_bytes(b"ID3" + b"audio-bytes" * 8)
        asyncio.run(self._seed())
        self.client = TestClient(app)

    def tearDown(self) -> None:
        asyncio.run(db.close_db())
        settings.data_dir = self.old_data_dir
        self.tmp.cleanup()

    async def _seed(self) -> None:
        from drivecast.jobs import pipeline
        from drivecast.schemas import EpisodeRec, PlanRec

        plan_rec = PlanRec(
            plan_id="plan_cancel", title="取消契约", direction="backend_arch",
            user_topic="取消", episodes=[
                EpisodeRec(episode_id="ep_c1", seq=1, title="第一集"),
                EpisodeRec(episode_id="ep_c2", seq=2, title="第二集"),
            ],
        )
        plans_dir = settings.data_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        (plans_dir / "plan_cancel.json").write_text(plan_rec.model_dump_json(), "utf-8")

        async with db.session() as s:
            topic = Topic(title="取消契约", direction="backend_arch")
            s.add(topic)
            await s.flush()
            plan = Plan(ext_id="plan_cancel", topic_id=topic.id, title="取消契约",
                        status="active", episode_count=2)
            s.add(plan)
            await s.flush()
            ep1 = Episode(ext_id="ep_c1", plan_id=plan.id, seq=1, title="第一集", status="ready")
            ep2 = Episode(ext_id="ep_c2", plan_id=plan.id, seq=2, title="第二集", status="pending")
            s.add_all([ep1, ep2])
            await s.flush()
            s.add(Audio(episode_id=ep1.id, file_path=str(self.audio_path),
                        duration_sec=600, size_bytes=self.audio_path.stat().st_size))
            await s.commit()
        await pipeline.enqueue_episode("plan_cancel", 2, "ep_c2")

    async def _row(self, model, pk: int):
        async with db.session() as s:
            return await s.get(model, pk)

    def test_cancel_plan_clears_pending_jobs_and_marks_episodes(self) -> None:
        res = self.client.post("/api/plans/1/cancel")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["cancelled_jobs"], 1)
        self.assertEqual(body["cancelled_episodes"], 1)

        self.assertEqual((asyncio.run(self._row(Plan, 1))).status, "cancelled")
        self.assertEqual((asyncio.run(self._row(Episode, 1))).status, "ready")      # 已就绪集保留
        self.assertEqual((asyncio.run(self._row(Episode, 2))).status, "cancelled")
        self.assertEqual((asyncio.run(self._row(Job, 1))).status, "cancelled")

        # 重复取消幂等;取消后生成类请求被 409 拒绝
        again = self.client.post("/api/plans/1/cancel")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["cancelled_jobs"], 0)
        rejected = self.client.post("/api/episodes/2/generate")
        self.assertEqual(rejected.status_code, 409)
        rejected_all = self.client.post("/api/plans/1/generate_all")
        self.assertEqual(rejected_all.status_code, 409)

        # 已就绪集仍可播放/上报进度
        stream = self.client.get("/api/episodes/1/audio")
        self.assertEqual(stream.status_code, 200)

    def test_activate_plan_requeues_cancelled_work(self) -> None:
        self.client.post("/api/plans/1/cancel")
        res = self.client.post("/api/plans/1/activate")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "pending")
        self.assertTrue(body["job_ids"])

        self.assertEqual((asyncio.run(self._row(Plan, 1))).status, "active")
        self.assertEqual((asyncio.run(self._row(Episode, 2))).status, "pending")
        requeued = asyncio.run(self._find_job("script", "ep_c2", "pending"))
        self.assertIsNotNone(requeued)

    async def _find_job(self, kind: str, ref_id: str, status: str):
        from sqlalchemy import select
        async with db.session() as s:
            return await s.scalar(
                select(Job).where(Job.kind == kind, Job.ref_id == ref_id, Job.status == status)
            )

    def test_progress_report_clamps_position_into_duration(self) -> None:
        res = self.client.post("/api/episodes/1/progress", json={"position_sec": 700})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["duration_sec"], 600)
        self.assertEqual(body["position_sec"], 599)   # 钳到 duration-1,防下次起播秒跳
        self.assertEqual(body["listen_status"], "done")  # 但听完判定仍按原始上报

    def test_cancelled_episode_is_terminal_for_production_writes(self) -> None:
        """取消后在途 job 完成/失败都不得把集从 cancelled 拉回中间态(幽灵"写脚本中")。"""
        from drivecast.persist import set_episode_status, upsert_script

        self.client.post("/api/plans/1/cancel")

        asyncio.run(upsert_script("ep_c2", {"content": "md", "knowledge": [], "questions": []}))
        self.assertEqual((asyncio.run(self._row(Episode, 2))).status, "cancelled")

        asyncio.run(set_episode_status("ep_c2", "failed"))
        self.assertEqual((asyncio.run(self._row(Episode, 2))).status, "cancelled")

    def test_audio_stream_falls_back_and_heals_stale_db_path(self) -> None:
        """DB 存的是别的机器的绝对路径(迁移后失配)时,按 ext_id 重算并回写自愈。"""
        async def _break_path() -> None:
            from sqlalchemy import update
            async with db.session() as s:
                await s.execute(
                    update(Audio).where(Audio.episode_id == 1)
                    .values(file_path="/nonexistent/machine/ep_c1.mp3")
                )
                await s.commit()

        asyncio.run(_break_path())
        res = self.client.get("/api/episodes/1/audio")
        self.assertEqual(res.status_code, 200)
        healed = asyncio.run(self._row(Audio, 1))
        self.assertEqual(healed.file_path, str(self.audio_path))


if __name__ == "__main__":
    unittest.main()

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
from drivecast.models import Audio, Episode, Plan, Question, Topic
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


if __name__ == "__main__":
    unittest.main()

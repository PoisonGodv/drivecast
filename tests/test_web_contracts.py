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
    """计划永久删除、运行中任务终止、播放位置钳制与路径自愈的 Web 契约。"""

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

    def test_delete_plan_permanently_removes_records_and_files(self) -> None:
        script_file = settings.script_path("ep_c1")
        script_file.parent.mkdir(parents=True, exist_ok=True)
        script_file.write_text('{"episode_id":"ep_c1"}', "utf-8")

        res = self.client.delete("/api/plans/1")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "deleted")
        self.assertEqual(body["cancelled_jobs"], 1)
        self.assertEqual(body["deleted_episodes"], 2)

        # 计划与集彻底从 DB 移除
        self.assertIsNone(asyncio.run(self._row(Plan, 1)))
        self.assertIsNone(asyncio.run(self._row(Episode, 1)))
        self.assertIsNone(asyncio.run(self._row(Episode, 2)))
        self.assertIsNone(asyncio.run(self._row(Job, 1)))

        # 音频、脚本及计划文件全部删除
        self.assertFalse(self.audio_path.exists())
        self.assertFalse(script_file.exists())
        self.assertFalse(settings.plan_path("plan_cancel").exists())

        # 再次请求返回 404
        again = self.client.delete("/api/plans/1")
        self.assertEqual(again.status_code, 404)
        rejected = self.client.post("/api/episodes/2/generate")
        self.assertEqual(rejected.status_code, 404)

    def test_progress_report_clamps_position_into_duration(self) -> None:
        res = self.client.post("/api/episodes/1/progress", json={"position_sec": 700})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["duration_sec"], 600)
        self.assertEqual(body["position_sec"], 599)   # 钳到 duration-1,防下次起播秒跳
        self.assertEqual(body["listen_status"], "done")  # 但听完判定仍按原始上报

    def test_cancelled_episode_is_terminal_for_production_writes(self) -> None:
        """删除栅栏后在途 job 完成不得重新写入内容。"""
        from drivecast.persist import set_episode_status, upsert_script

        self.client.delete("/api/plans/1")

        asyncio.run(upsert_script("ep_c2", {"content": "md", "knowledge": [], "questions": []}))
        self.assertIsNone(asyncio.run(self._row(Episode, 2)))

        asyncio.run(set_episode_status("ep_c2", "failed"))
        self.assertIsNone(asyncio.run(self._row(Episode, 2)))

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

    def test_sse_emits_canonical_status_and_not_found_on_delete(self) -> None:
        from drivecast.web.api_plans import _episode_event
        ev_scripted = _episode_event({"id": 1, "seq": 1, "status": "scripted"})
        self.assertEqual(ev_scripted["status"], "scripted")
        self.assertEqual(ev_scripted["stage"], "script")
        self.assertEqual(ev_scripted["stage_status"], "done")

        ev_tts = _episode_event({"id": 2, "seq": 2, "status": "tts_ready"})
        self.assertEqual(ev_tts["status"], "tts_ready")
        self.assertEqual(ev_tts["stage"], "tts")
        self.assertEqual(ev_tts["stage_status"], "generating")

        del_res = self.client.delete("/api/plans/1")
        self.assertEqual(del_res.status_code, 200)
        events_res = self.client.get("/api/plans/1/events")
        self.assertEqual(events_res.status_code, 200)
        first_chunk = next(events_res.iter_lines())
        self.assertTrue(first_chunk.startswith("retry: 3000"))


    def test_running_child_task_is_cancelled_on_delete(self) -> None:
        """测试正在执行的 child task 在计划删除时能被立即 cancel 并不死常驻循环。"""
        import asyncio
        from drivecast.jobs import worker

        async def _scenario() -> None:
            started = asyncio.Event()
            cancelled = asyncio.Event()

            async def _long_task() -> None:
                started.set()
                try:
                    await asyncio.sleep(10.0)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            task = asyncio.create_task(_long_task())
            worker.register_running_job("plan_cancel", 999, task)
            await started.wait()
            count = await worker.cancel_and_await_plan_tasks("plan_cancel", timeout=3.0)
            worker.unregister_running_job("plan_cancel", 999, task)
            self.assertEqual(count, 1)
            self.assertTrue(cancelled.is_set())
            self.assertFalse(worker.has_running_job_task("plan_cancel", 999))

        asyncio.run(_scenario())


if __name__ == "__main__":
    unittest.main()

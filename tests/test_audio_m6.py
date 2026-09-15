from __future__ import annotations

import asyncio
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from drivecast.jobs import worker
from drivecast.models import Job
from drivecast.schemas import ScriptOut
from drivecast.tts import service


class AudioPipelineUnitTest(unittest.TestCase):
    def test_script_schema_accepts_stable_five_sections(self) -> None:
        data = {
            "sections": [
                {"key": key, "title": title, "content": f"自然口播正文 {key}。"}
                for key, title in zip(service.SECTION_KEYS, (
                    "回顾与开场", "核心知识点", "高频面试问法", "答题框架", "易错点与自测",
                ))
            ]
        }
        out = ScriptOut.model_validate(data)
        self.assertEqual([section.key for section in out.sections], list(service.SECTION_KEYS))
        self.assertNotIn("核心知识点", out.sections[1].content)

    def test_legacy_tts_upgrade_preserves_seq_and_text(self) -> None:
        chunks = [
            {"seq": index, "speaker": "expert", "voice": "v", "rate": "-5%", "text": "正文" * 90}
            for index in range(1, 21)
        ]
        payload = {"episode_id": "ep_test", "chunks": chunks}
        with TemporaryDirectory() as tmp, patch.object(service.settings, "data_dir", Path(tmp)):
            service.settings.tts_path("ep_test").parent.mkdir(parents=True)
            before = [(chunk["seq"], chunk["text"]) for chunk in chunks]
            result = service._upgrade_legacy_tts({"episode_id": "ep_test"}, payload)
        self.assertEqual([(chunk["seq"], chunk["text"]) for chunk in result], before)
        self.assertEqual({chunk["section_key"] for chunk in result}, set(service.SECTION_KEYS))
        self.assertFalse(any(chunk["section_title"] in chunk["text"] for chunk in result))

    def test_chapters_are_monotonic_and_quality_reports_failures(self) -> None:
        chunks = [
            {"seq": index + 1, "speaker": "expert", "section_key": key,
             "section_title": f"章节{index + 1}", "text": "正文" * 100}
            for index, key in enumerate(service.SECTION_KEYS)
        ]
        chapters = service._chapters(chunks, [60.0] * 5)
        self.assertEqual(len(chapters), 5)
        self.assertEqual(chapters[0]["start_sec"], 0.0)
        self.assertTrue(all(a["end_sec"] <= b["start_sec"] for a, b in zip(chapters, chapters[1:])))

        with TemporaryDirectory() as tmp:
            paths = []
            for index in range(5):
                path = Path(tmp) / f"{index}.mp3"
                path.write_bytes(b"x")
                paths.append(path)
            report = service._quality_report(
                duration=300, estimated_duration_sec=600, peak_db=-0.2,
                chunks=chunks, paths=paths, durations=[60.0] * 5,
            )
        self.assertEqual(report["status"], "warning")
        self.assertIn("duration_range", report["failed_checks"])
        self.assertIn("estimated_duration_error", report["failed_checks"])
        self.assertIn("peak", report["failed_checks"])
        self.assertTrue(report["checks"]["chunks"]["passed"])
    def test_quality_uses_calibrated_estimate_not_requested_minutes(self) -> None:
        chunks = [
            {"seq": index + 1, "text": "正文", "section_key": key}
            for index, key in enumerate(service.SECTION_KEYS)
        ]
        with TemporaryDirectory() as tmp:
            paths = []
            for index in range(5):
                path = Path(tmp) / f"{index}.mp3"
                path.write_bytes(b"x")
                paths.append(path)
            report = service._quality_report(
                duration=600,
                estimated_duration_sec=600,
                peak_db=-1.0,
                chunks=chunks,
                paths=paths,
                durations=[120.0] * 5,
            )
        self.assertTrue(report["checks"]["estimated_duration_error"]["passed"])
        self.assertEqual(report["checks"]["estimated_duration_error"]["error_pct"], 0.0)


class AudioUpgradeJobTest(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_reuses_complete_chunks_without_edge_tts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ep_id = "ep_upgrade"
            tts_path = root / "tts" / f"{ep_id}.tts.json"
            chunks_dir = root / "audio" / "chunks" / ep_id
            tts_path.parent.mkdir(parents=True)
            chunks_dir.mkdir(parents=True)
            tts_path.write_text(json.dumps({"chunks": [{"seq": 1}]}), "utf-8")
            (chunks_dir / "001.mp3").write_bytes(b"chunk")
            job = Job(
                id=1,
                kind="audio_upgrade",
                ref_id=ep_id,
                payload=json.dumps({"plan_id": "plan", "seq": 1}),
            )
            synthesize = AsyncMock(return_value={"pipeline_version": service.AUDIO_PIPELINE_VERSION})
            with patch.object(worker.settings, "data_dir", root), \
                    patch.object(worker.planner, "load_plan", return_value=SimpleNamespace(title="专辑")), \
                    patch.object(worker.scriptwriter, "load_script", return_value={"episode_id": ep_id}), \
                    patch.object(worker.tts.edge, "synth_chunk", AsyncMock(side_effect=AssertionError("禁止 TTS"))), \
                    patch.object(worker.tts, "synthesize_episode", synthesize), \
                    patch.object(worker, "_finish", AsyncMock()) as finish:
                await worker._run_audio_upgrade_job(job)
            synthesize.assert_awaited_once()
            finish.assert_awaited_once_with(1, "done")

    async def test_upgrade_missing_chunk_fails_before_synthesis(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ep_id = "ep_missing"
            tts_path = root / "tts" / f"{ep_id}.tts.json"
            tts_path.parent.mkdir(parents=True)
            tts_path.write_text(json.dumps({"chunks": [{"seq": 1}]}), "utf-8")
            job = Job(
                id=2,
                kind="audio_upgrade",
                ref_id=ep_id,
                payload=json.dumps({"plan_id": "plan", "seq": 1}),
            )
            synthesize = AsyncMock()
            edge = AsyncMock(side_effect=AssertionError("禁止 TTS"))
            with patch.object(worker.settings, "data_dir", root), \
                    patch.object(worker.planner, "load_plan", return_value=SimpleNamespace(title="专辑")), \
                    patch.object(worker.scriptwriter, "load_script", return_value={"episode_id": ep_id}), \
                    patch.object(worker.tts.edge, "synth_chunk", edge), \
                    patch.object(worker.tts, "synthesize_episode", synthesize):
                with self.assertRaisesRegex(RuntimeError, "chunk 不完整"):
                    await worker._run_audio_upgrade_job(job)
            synthesize.assert_not_awaited()
            edge.assert_not_awaited()


class AudioPipelineFFmpegTest(unittest.TestCase):
    def test_ffmetadata_id3_cover_and_probe(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunks = []
            paths = []
            for index, key in enumerate(service.SECTION_KEYS):
                path = root / f"{index + 1:03d}.mp3"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={440 + index * 20}:sample_rate=24000", "-t", "0.2",
                    "-q:a", "9", str(path),
                ], check=True)
                paths.append(path)
                chunks.append({
                    "seq": index + 1, "speaker": "expert", "section_key": key,
                    "section_title": f"章节{index + 1}", "text": "正文" * 20,
                })
            durations = asyncio.run(service._chunk_durations(paths))
            chapters = service._chapters(chunks, durations)
            out = root / "episode.mp3"
            with patch.object(service.settings, "data_dir", root), \
                    patch.object(service.settings, "audio_cover_path",
                                 Path(__file__).parents[1] / "static/assets/cover.png"):
                asyncio.run(service._concat(
                    chunks, paths, out, title="测试标题", album="测试专辑", track=7,
                    chapters=chapters,
                ))
            probe = subprocess.run([
                "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format",
                "-show_streams", "-show_chapters", str(out),
            ], check=True, capture_output=True, text=True)
            data = json.loads(probe.stdout)
        self.assertEqual(data["format"]["tags"]["title"], "测试标题")
        self.assertEqual(data["format"]["tags"]["artist"], "DriveCast")
        self.assertEqual(data["format"]["tags"]["album"], "测试专辑")
        self.assertEqual(data["format"]["tags"]["track"], "7")
        self.assertEqual(len(data["chapters"]), 5)
        self.assertTrue(any(stream.get("disposition", {}).get("attached_pic") == 1
                            for stream in data["streams"]))
        self.assertGreater(float(data["format"]["duration"]), 0)


if __name__ == "__main__":
    unittest.main()

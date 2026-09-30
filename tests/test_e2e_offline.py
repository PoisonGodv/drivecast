"""离线端到端:真实 HTTP API + 真实 worker/ffmpeg,只把 LLM 与 TTS 换成本地假实现。

主题 → 计划 → worker 跑空队列 → 计划 active、各集 ready → 音频/脚本接口可读。
不触达任何付费 LLM/TTS/搜索服务;本机无 ffmpeg 时整体跳过。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auth_test_support import authenticated_admin_client

from drivecast import db
from drivecast.config import settings
from drivecast.jobs import worker
from drivecast.llm.provider import LLMResult
from drivecast.refs import hunter
from drivecast.reviewer import service as reviewer_service
from drivecast.schemas import EpisodeOutline, PlanOut, Question, QuizSectionOut, SectionOut

EPISODES = 4  # PlanCreate 的下限


class FakeLLM:
    """按 schema 分派的假 LLM;记录调用以便断言确实走到了各环节。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, *, system: str, user: str, schema: type | None = None,
                       temperature: float = 0.7) -> LLMResult:
        name = schema.__name__ if schema else "text"
        self.calls.append(name)
        if schema is PlanOut:
            parsed = PlanOut(title="离线端到端", episodes=[
                EpisodeOutline(seq=i, title=f"第{i}集", goal="能讲清", objectives=["要点"], outline="大纲")
                for i in range(1, EPISODES + 1)
            ])
        elif schema is not None and issubclass(schema, SectionOut):
            content = "离线测试口播内容。"
            if issubclass(schema, QuizSectionOut):
                parsed = QuizSectionOut(title="易错", content=content, questions=[
                    Question(qtype=q, question=f"{q} 题", framework=["结论"]) for q in
                    ("concept", "interview", "scenario")
                ])
            else:
                parsed = SectionOut(title="段落", content=content)
        elif schema is reviewer_service._ReviewRaw:
            parsed = reviewer_service._ReviewRaw(verdict="pass", flags=[])
        else:
            raise AssertionError(f"未预期的 LLM 调用: {name}")
        return LLMResult(content="", parsed=parsed, usage_tokens=1, model="fake")


async def fake_synth_chunk(text: str, voice: str, rate: str, out_path: Path) -> None:
    # 不能用纯静音:loudnorm 对全静音测得 -inf LUFS 会直接报错,用低音量正弦代替
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000",
        "-af", "volume=0.1", "-t", "0.3", "-q:a", "9", str(out_path),
    ], check=True)


async def _no_sleep() -> None:
    return None


@unittest.skipIf(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                 "需要本机 ffmpeg/ffprobe")
class OfflineEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.llm = FakeLLM()
        for target, attr, value in (
            (settings, "data_dir", Path(self.tmp.name)),
            (settings, "tts_provider", "edge"),
            (settings, "reviewer_enabled", True),
            (settings, "ref_hunter_enabled", False),
            (settings, "worker_poll_interval_sec", 0.05),
        ):
            self.enterContext(patch.object(target, attr, value))
        for module in ("planner.service", "scriptwriter.service", "reviewer.service", "refs.hunter"):
            self.enterContext(patch(f"drivecast.{module}.get_provider", return_value=self.llm))
        self.enterContext(patch("drivecast.tts.edge.synth_chunk", fake_synth_chunk))
        self.enterContext(patch("drivecast.tts.edge.jitter_sleep", _no_sleep))
        # 两处模块级缓存的 provider 必须清空,否则会复用别的测试注入的实例
        reviewer_service._set_provider(None)
        hunter._set_provider(None)
        self.addCleanup(reviewer_service._set_provider, None)
        self.addCleanup(hunter._set_provider, None)
        asyncio.run(db.init_db())
        self.addCleanup(lambda: asyncio.run(db.close_db()))
        self.client = authenticated_admin_client(self)

    def _run_worker(self) -> None:
        asyncio.run(asyncio.wait_for(worker.run_until_empty(), timeout=120))

    def test_topic_to_playable_episodes(self) -> None:
        topic = self.client.post("/api/topics", json={"title": "Redis 分布式锁", "direction": "backend_arch"})
        self.assertEqual(topic.status_code, 201, topic.text)
        created = self.client.post("/api/plans", json={
            "topic_id": topic.json()["id"], "episode_count": EPISODES, "target_minutes": 8,
        })
        self.assertEqual(created.status_code, 201, created.text)
        plan_id = created.json()["plan_id"]

        self._run_worker()
        # 大纲若停在 draft(需人工确认后才入队各集),确认后再跑一轮;否则已链式入队
        if self.client.get(f"/api/plans/{plan_id}").json()["status"] == "draft":
            confirmed = self.client.post(f"/api/plans/{plan_id}/confirm")
            self.assertLess(confirmed.status_code, 300, confirmed.text)
            self._run_worker()

        detail = self.client.get(f"/api/plans/{plan_id}").json()
        self.assertEqual(detail["status"], "active")
        self.assertEqual(detail["title"], "离线端到端")
        self.assertEqual(len(detail["episodes"]), EPISODES)
        self.assertEqual(detail["ready_count"], EPISODES)
        for ep in detail["episodes"]:
            self.assertEqual(ep["status"], "ready", ep)
            self.assertTrue(ep["has_audio"], ep)
        self.assertIn("PlanOut", self.llm.calls)
        self.assertIn("QuizSectionOut", self.llm.calls)
        self.assertIn("_ReviewRaw", self.llm.calls)

        first = detail["episodes"][0]["id"]
        audio = self.client.get(f"/api/episodes/{first}/audio")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.headers["content-type"], "audio/mpeg")
        self.assertGreater(len(audio.content), 1000)
        ranged = self.client.get(f"/api/episodes/{first}/audio", headers={"Range": "bytes=0-99"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(len(ranged.content), 100)

        script = self.client.get(f"/api/episodes/{first}/script")
        self.assertEqual(script.status_code, 200, script.text)
        body = script.json()
        self.assertIn("离线测试口播内容", body["content_md"])
        self.assertEqual(body["review"]["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()

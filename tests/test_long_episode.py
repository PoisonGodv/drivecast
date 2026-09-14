"""长集(40 分钟)支持与片尾参考文献测试:
- 单段超 section_max_words_per_agent 时拆成并行子 agent,拼接后结构与题目数不变
- 参考链接 → refs_outro 口播拼入文稿末尾,TTS 落成 S5「参考资料」章节
- Web 计划创建接受 40 分钟、拒绝 46 分钟
全部 mock LLM/TTS,绝不真实调用。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivecast import db
from drivecast.config import settings
from drivecast.llm.provider import LLMResult
from drivecast.schemas import EpisodeRec, PlanRec, Question, QuizSectionOut, SectionOut
from drivecast.scriptwriter import service as script_service
from drivecast.tts import service as tts_service
from drivecast.web.main import app


def _plan_ep(minutes: int = 40) -> tuple[PlanRec, EpisodeRec]:
    plan = PlanRec(plan_id="plan_long", title="分布式系统深度", direction="backend_arch",
                   user_topic="分布式系统", target_minutes=minutes, episodes=[])
    ep = EpisodeRec(episode_id="ep_long_01", seq=1, title="一致性协议", goal="g",
                    objectives=["Paxos 原理", "Raft 选举", "Zab 与 ZK", "Multi-Paxos 优化"],
                    outline="o", target_minutes=minutes)
    return plan, ep


class SizedFakeProvider:
    """按 user prompt 里的「本段目标字数:约 N 字」返回恰好 N 字正文,稳定过字数门禁。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, *, system: str, user: str, schema: type | None = None,
                       temperature: float = 0.7) -> LLMResult:
        self.calls.append({"system": system, "user": user, "schema": schema})
        import re
        match = re.search(r"本段目标字数:约 (\d+) 字", user)
        size = int(match.group(1)) if match else 200
        content = "甲" * size
        if schema is not None and issubclass(schema, QuizSectionOut):
            parsed = QuizSectionOut(title="易错", content=content, questions=[
                Question(qtype="concept", question="c", framework=["a"], difficulty="medium"),
                Question(qtype="interview", question="i", framework=["b"], difficulty="medium"),
                Question(qtype="scenario", question="s", framework=["c"], difficulty="medium"),
            ])
        else:
            parsed = SectionOut(title="段落", content=content)
        return LLMResult(content="", parsed=parsed, usage_tokens=10, model="fake")


class SectionSplitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        self.old_reviewer = settings.reviewer_enabled
        settings.reviewer_enabled = False  # 本测试不关心审稿,关掉少一层 mock
        self.old_ref_hunter = settings.ref_hunter_enabled
        settings.ref_hunter_enabled = False

    async def asyncTearDown(self) -> None:
        settings.data_dir = self.old_data_dir
        settings.reviewer_enabled = self.old_reviewer
        settings.ref_hunter_enabled = self.old_ref_hunter
        self.tmp.cleanup()

    async def test_long_episode_splits_sections_into_parallel_sub_agents(self) -> None:
        """40 分钟集:S1(约 4200 字)/S2(约 3200 字)超 2400 上限,各拆 2 个子 agent。"""
        plan, ep = _plan_ep(minutes=40)
        provider = SizedFakeProvider()
        with patch("drivecast.scriptwriter.service.get_provider", return_value=provider), \
             patch("drivecast.scriptwriter.service.upsert_script", new=AsyncMock()):
            data = await script_service.write_script(plan, ep)

        # S0(400)+S1(2)+S2(2)+S3(2400 不拆 1)+S4(1600 不拆 1) = 7 次 LLM 调用
        self.assertEqual(len(provider.calls), 7)
        # S1 拆出的子 agent 各自带知识点切片分工,且互不重复
        s1_notes = [c["user"] for c in provider.calls if "你只负责讲透以下知识点" in c["user"]]
        self.assertEqual(len(s1_notes), 2)
        self.assertIn("Paxos 原理", s1_notes[0])
        self.assertIn("Zab 与 ZK", s1_notes[1])
        # 结构仍是稳定五段,S1 拼接后字数 ≈ 两段子 agent 之和(4400 目标拆 2 × 2200)
        self.assertEqual([s["key"] for s in data["sections"]], ["S0", "S1", "S2", "S3", "S4"])
        s1 = next(s for s in data["sections"] if s["key"] == "S1")
        self.assertGreaterEqual(len(s1["content"]), 4300)
        # 自测题仍恰好 3 道(由 S4 产出)
        self.assertEqual(len(data["questions"]), 3)

    async def test_short_episode_stays_single_agent_per_section(self) -> None:
        """15 分钟集:每段一次调用,不拆分(与拆分前行为一致)。"""
        plan, ep = _plan_ep(minutes=15)
        provider = SizedFakeProvider()
        with patch("drivecast.scriptwriter.service.get_provider", return_value=provider), \
             patch("drivecast.scriptwriter.service.upsert_script", new=AsyncMock()):
            await script_service.write_script(plan, ep)
        self.assertEqual(len(provider.calls), 5)
        self.assertFalse(any("接力合写" in c["user"] for c in provider.calls))


class RefsOutroTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        self.old_reviewer = settings.reviewer_enabled
        settings.reviewer_enabled = False

    async def asyncTearDown(self) -> None:
        settings.data_dir = self.old_data_dir
        settings.reviewer_enabled = self.old_reviewer
        self.tmp.cleanup()

    async def _write(self, references: list[dict]) -> dict:
        plan, ep = _plan_ep(minutes=15)
        hunter = AsyncMock(return_value=references)
        with patch("drivecast.scriptwriter.service.get_provider",
                   return_value=SizedFakeProvider()), \
             patch("drivecast.scriptwriter.service.hunter.hunt_references", new=hunter), \
             patch("drivecast.scriptwriter.service.upsert_script", new=AsyncMock()):
            return await script_service.write_script(plan, ep)

    async def test_outro_appended_to_script_end(self) -> None:
        """有参考链接:refs_outro 口播拼到文稿末尾,不念 URL。"""
        data = await self._write([
            {"title": "Raft 论文(Extended)", "url": "https://raft.github.io/raft.pdf", "note": ""},
            {"title": "ZooKeeper 官方文档", "url": "https://zookeeper.apache.org/doc", "note": ""},
        ])
        outro = data["refs_outro"]
        self.assertIn("Raft 论文", outro)
        self.assertIn("ZooKeeper 官方文档", outro)
        self.assertNotIn("http", outro)  # 口播不念 URL
        self.assertTrue(data["content"].endswith(outro))

    async def test_outro_generic_when_refs_have_no_titles(self) -> None:
        data = await self._write([{"title": "", "url": "https://example.com/x", "note": ""}])
        self.assertIn("参考了若干资料", data["refs_outro"])
        self.assertNotIn("http", data["refs_outro"])

    async def test_no_outro_without_references(self) -> None:
        data = await self._write([])
        self.assertNotIn("refs_outro", data)

    async def test_dialogue_outro_uses_host_turn(self) -> None:
        plan, ep = _plan_ep(minutes=15)
        ep.script_format = "dialogue"
        hunter = AsyncMock(return_value=[
            {"title": "Raft 论文", "url": "https://raft.github.io/raft.pdf", "note": ""},
        ])

        class DialogueProvider(SizedFakeProvider):
            async def complete(self, *, system, user, schema=None, temperature=0.7):
                self.calls.append({"system": system, "user": user, "schema": schema})
                from drivecast.schemas import Turn
                turns = [Turn(speaker="host", text="问" * 100), Turn(speaker="expert", text="答" * 100)]
                if schema is not None and issubclass(schema, QuizSectionOut):
                    parsed = QuizSectionOut(title="易错", turns=turns, questions=[
                        Question(qtype="concept", question="c", framework=["a"], difficulty="medium"),
                        Question(qtype="interview", question="i", framework=["b"], difficulty="medium"),
                        Question(qtype="scenario", question="s", framework=["c"], difficulty="medium"),
                    ])
                else:
                    parsed = SectionOut(title="段落", turns=turns)
                return LLMResult(content="", parsed=parsed, usage_tokens=10, model="fake")

        with patch("drivecast.scriptwriter.service.get_provider", return_value=DialogueProvider()), \
             patch("drivecast.scriptwriter.service.hunter.hunt_references", new=hunter), \
             patch("drivecast.scriptwriter.service.upsert_script", new=AsyncMock()):
            data = await script_service.write_script(plan, ep)
        self.assertEqual(data["turns"][-1]["speaker"], "host")
        self.assertIn("Raft 论文", data["turns"][-1]["text"])


class OutroChapterTest(unittest.TestCase):
    """TTS 侧:refs_outro → S5 chunk 与「参考资料」章节。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        settings.data_dir = self.old_data_dir
        self.tmp.cleanup()

    def _script(self, outro: str = "", script_format: str = "monologue") -> dict:
        script = {
            "episode_id": "ep_outro",
            "script_format": script_format,
            "sections": [
                {"key": key, "title": f"标题{key}", "content": f"{key} 正文内容。", "turns": []}
                for key in tts_service.SECTION_KEYS
            ],
        }
        if outro:
            script["refs_outro"] = outro
        return script

    def test_outro_becomes_s5_chunks_and_chapter(self) -> None:
        chunks = tts_service.build_tts_json(self._script(outro="本期内容参考了以下资料:第一条,Raft 论文。"))
        s5 = [c for c in chunks if c["section_key"] == tts_service.OUTRO_KEY]
        self.assertTrue(s5)
        self.assertEqual(s5[0]["section_title"], settings.chapter_title_refs)
        self.assertEqual(chunks[-1]["section_key"], tts_service.OUTRO_KEY)  # 在音频最后

        chapters = tts_service._chapters(chunks, [60.0] * len(chunks))
        self.assertEqual(chapters[-1]["key"], tts_service.OUTRO_KEY)
        self.assertEqual(chapters[-1]["title"], settings.chapter_title_refs)
        self.assertEqual(len(chapters), 6)
        self.assertTrue(all(a["end_sec"] <= b["start_sec"] for a, b in zip(chapters, chapters[1:])))

    def test_dialogue_outro_uses_host_voice(self) -> None:
        chunks = tts_service.build_tts_json(
            self._script(outro="本期内容参考了以下资料。", script_format="dialogue"))
        s5 = [c for c in chunks if c["section_key"] == tts_service.OUTRO_KEY]
        self.assertTrue(all(c["speaker"] == "host" for c in s5))

    def test_no_outro_keeps_five_chapters(self) -> None:
        chunks = tts_service.build_tts_json(self._script())
        self.assertEqual({c["section_key"] for c in chunks}, set(tts_service.SECTION_KEYS))
        chapters = tts_service._chapters(chunks, [60.0] * len(chunks))
        self.assertEqual(len(chapters), 5)

    def test_s5_chunks_not_misread_as_legacy(self) -> None:
        """含 S5 的新格式 tts.json 不应被 legacy 升级路径重新按五段权重分组。"""
        chunks = tts_service.build_tts_json(self._script(outro="本期内容参考了以下资料。"))
        payload = json.loads(settings.tts_path("ep_outro").read_text("utf-8"))
        upgraded = tts_service._upgrade_legacy_tts(self._script(), payload)
        # 原样返回(内容逐 chunk 一致),不被 _weighted_groups 重切
        self.assertEqual(
            [(c["seq"], c["section_key"], c["text"]) for c in upgraded],
            [(c["seq"], c["section_key"], c["text"]) for c in chunks],
        )


class PlanMinutesContractTest(unittest.TestCase):
    def setUp(self) -> None:
        asyncio.run(db.close_db())
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        asyncio.run(db.init_db())
        self.client = TestClient(app)

    def tearDown(self) -> None:
        asyncio.run(db.close_db())
        settings.data_dir = self.old_data_dir
        self.tmp.cleanup()

    def _seed_topic(self) -> int:
        resp = self.client.post("/api/topics", json={"title": "分布式系统", "direction": "backend_arch"})
        self.assertEqual(resp.status_code, 201)
        return resp.json()["id"]

    def test_create_plan_accepts_40_minutes(self) -> None:
        topic_id = self._seed_topic()
        resp = self.client.post("/api/plans",
                                json={"topic_id": topic_id, "episode_count": 4, "target_minutes": 40})
        self.assertEqual(resp.status_code, 201)

    def test_create_plan_rejects_over_45_minutes(self) -> None:
        topic_id = self._seed_topic()
        resp = self.client.post("/api/plans",
                                json={"topic_id": topic_id, "episode_count": 4, "target_minutes": 46})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()

"""题库加载与 grounding 注入测试。

契约: docs/07 §1.1/§1.3/§2
- 题库 JSON 合法且数量达标
- loader 对非法 JSON/空目录容错
- planner/scriptwriter prompt 中注入题库内容(fake provider 截获断言)
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivecast.config import settings
from drivecast.content.loader import get_fact_cards, get_question_bank
from drivecast.llm.provider import LLMResult
from drivecast.planner import service as planner_service
from drivecast.schemas import (
    Direction, EpisodeOutline, EpisodeRec, PlanOut, PlanRec, Question,
    QuizSectionOut, ScriptOut, ScriptSection, SectionOut, Turn,
)
from drivecast.scriptwriter import service as script_service


_MIN_COUNTS: dict[str, int] = {
    "backend_arch": 40,
    "ai_llm": 40,
    "tech_news": 15,
    "source_dive": 15,
}


def _fake_questions() -> list[Question]:
    return [
        Question(qtype="concept", question="c", framework=["a"], difficulty="medium"),
        Question(qtype="interview", question="i", framework=["b"], difficulty="medium"),
        Question(qtype="scenario", question="s", framework=["c"], difficulty="medium"),
    ]


class FakeProvider:
    """截获 LLM 调用,记录 system/user/schema,按请求 schema 返回预设结构化输出。

    脚本生成已拆成分段 agent(每段一次调用,schema 为 SectionOut/QuizSectionOut),
    其他调用(planner 等)仍返回构造时传入的 parsed。
    """

    def __init__(self, parsed: object) -> None:
        self.calls: list[dict] = []
        self.parsed = parsed

    async def complete(self, *, system: str, user: str, schema: type | None = None, temperature: float = 0.7) -> LLMResult:
        self.calls.append({"system": system, "user": user, "schema": schema, "temperature": temperature})
        if schema is not None and issubclass(schema, SectionOut):
            content = "本段围绕分布式锁与 Redlock 的核心考点展开,讲原理与面试答法。" * 2
            if issubclass(schema, QuizSectionOut):
                parsed: object = QuizSectionOut(title="易错", content=content, questions=_fake_questions())
            else:
                parsed = SectionOut(title="段落", content=content)
            return LLMResult(content="", parsed=parsed, usage_tokens=10, model="fake")
        return LLMResult(content="", parsed=self.parsed, usage_tokens=10, model="fake")


class QuestionBankLoaderTest(unittest.TestCase):
    def test_all_banks_load_and_meet_minimum_counts(self) -> None:
        for direction, minimum in _MIN_COUNTS.items():
            questions = get_question_bank(direction)
            self.assertGreaterEqual(
                len(questions), minimum,
                f"{direction} 题库数量不足: {len(questions)} < {minimum}",
            )
            for q in questions:
                self.assertIn("id", q, f"{direction} 存在缺 id 的题目")
                self.assertIn("question", q)
                self.assertIn("topic", q)
                self.assertIsInstance(q["topic"], list)
                self.assertIn("frequency", q)
                self.assertIn(q["frequency"], {"high", "mid", "low"})
                self.assertIn("source", q)

    def test_questions_sorted_by_frequency(self) -> None:
        order = {"high": 0, "mid": 1, "low": 2}
        for direction in _MIN_COUNTS:
            questions = get_question_bank(direction)
            if len(questions) < 2:
                continue
            for i in range(len(questions) - 1):
                self.assertLessEqual(
                    order[questions[i]["frequency"]],
                    order[questions[i + 1]["frequency"]],
                    f"{direction} 频率排序错误",
                )

    def test_corrupt_question_bank_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            bank_dir = Path(tmp_name) / "question_bank"
            bank_dir.mkdir(parents=True)
            (bank_dir / "broken.json").write_text("{broken json", "utf-8")
            with patch("drivecast.content.loader._question_bank_dir", return_value=bank_dir):
                self.assertEqual(get_question_bank("broken"), [])

    def test_missing_question_bank_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            bank_dir = Path(tmp_name) / "question_bank"
            bank_dir.mkdir(parents=True)
            with patch("drivecast.content.loader._question_bank_dir", return_value=bank_dir):
                self.assertEqual(get_question_bank("ghost"), [])

    def test_fact_cards_empty_dir_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            cards_dir = Path(tmp_name) / "fact_cards"
            cards_dir.mkdir(parents=True)
            with patch("drivecast.content.loader._fact_cards_dir", return_value=cards_dir):
                self.assertEqual(get_fact_cards(["分布式锁"]), [])

    def test_fact_cards_match_and_sort_by_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            cards_dir = Path(tmp_name) / "fact_cards"
            cards_dir.mkdir(parents=True)
            (cards_dir / "a.json").write_text(
                json.dumps({"slug": "a", "title": "分布式锁", "direction": "backend_arch", "tags": ["redlock"]}, ensure_ascii=False),
                "utf-8",
            )
            (cards_dir / "b.json").write_text(
                json.dumps({"slug": "b", "title": "MySQL 索引", "direction": "backend_arch", "tags": []}, ensure_ascii=False),
                "utf-8",
            )
            (cards_dir / "bad.json").write_text("{broken", "utf-8")
            with patch("drivecast.content.loader._fact_cards_dir", return_value=cards_dir):
                cards = get_fact_cards(["分布式锁", "redlock"])
                self.assertEqual([c["slug"] for c in cards], ["a"])
                cards2 = get_fact_cards(["MySQL"])
                self.assertEqual([c["slug"] for c in cards2], ["b"])


class PlannerGroundingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)

    async def asyncTearDown(self) -> None:
        settings.data_dir = self.old_data_dir
        self.tmp.cleanup()

    async def test_planner_prompt_includes_top15_high_frequency_questions(self) -> None:
        fake_out = PlanOut(
            title="测试计划",
            episodes=[
                EpisodeOutline(seq=1, title="集1", goal="g", objectives=["o"], outline="大纲"),
            ],
        )
        fake = FakeProvider(fake_out)

        with patch("drivecast.planner.service.get_provider", return_value=fake):
            with patch("drivecast.planner.service.upsert_plan", new=AsyncMock()):
                plan = await planner_service.create_plan("测试主题", direction="backend_arch", episode_count=1)

        self.assertEqual(plan.title, "测试计划")
        self.assertEqual(len(fake.calls), 1)
        user = fake.calls[0]["user"]
        self.assertIn("【方向高频面试题参考】", user)
        # 高频题中第一题应被注入
        first_high = next(q for q in get_question_bank("backend_arch") if q["frequency"] == "high")
        self.assertIn(first_high["question"], user)
        # 计数: 按行号解析应至少 15 题
        question_lines = [ln for ln in user.splitlines() if ln.strip().startswith(("1.", "2.", "15."))]
        self.assertTrue(any("15." in ln for ln in question_lines), "应注入至少 15 道高频题")


class ScriptwriterGroundingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        # 本组用例只测 grounding 注入,关掉审稿避免误触真实 LLM(docs/07 §3.1)
        self.old_reviewer_enabled = settings.reviewer_enabled
        settings.reviewer_enabled = False

    async def asyncTearDown(self) -> None:
        settings.data_dir = self.old_data_dir
        settings.reviewer_enabled = self.old_reviewer_enabled
        self.tmp.cleanup()

    async def test_scriptwriter_prompt_includes_at_least_two_matched_questions(self) -> None:
        fake = FakeProvider(None)  # 分段 agent 模式下按 schema 自动返回各段输出

        plan = PlanRec(
            plan_id="plan_test",
            title="分布式锁",
            direction="backend_arch",
            user_topic="分布式锁",
            episodes=[],
        )
        ep = EpisodeRec(
            episode_id="ep_test_01",
            seq=1,
            title="Redis 分布式锁与 Redlock 争议",
            goal="能讲清 Redis 锁与 Redlock 争议",
            objectives=["SET NX EX", "Redlock"],
            outline="分布式锁实现与争议",
        )

        with patch("drivecast.scriptwriter.service.get_provider", return_value=fake):
            with patch("drivecast.scriptwriter.service.upsert_script", new=AsyncMock()):
                data = await script_service.write_script(plan, ep)

        self.assertEqual(data["episode_id"], "ep_test_01")
        # 分段 agent:每段一次调用(S4 用 QuizSectionOut,其余 SectionOut),字数门禁可能触发本段重出
        section_calls = [c for c in fake.calls
                         if c["schema"] is not None and issubclass(c["schema"], SectionOut)]
        self.assertGreaterEqual(len(section_calls), 5, "五段应各至少调用一次")
        self.assertTrue(any(issubclass(c["schema"], QuizSectionOut) for c in section_calls),
                        "S4 应使用 QuizSectionOut 顺带产出自测题")
        self.assertEqual(len(data["questions"]), 3)

        # grounding 只注入 S2 段的 prompt
        s2_users = [c["user"] for c in section_calls
                    if "【本集必须覆盖的题库高频问法】" in c["user"]]
        self.assertTrue(s2_users, "S2 段 prompt 应注入题库高频问法")
        # 主题匹配应命中分布式锁相关题目,至少 2 题
        matched_count = sum(1 for q in get_question_bank("backend_arch") if q["question"] in s2_users[0])
        self.assertGreaterEqual(matched_count, 2, f"S2 prompt 应注入至少 2 道匹配题,实际 {matched_count}")

    async def test_scriptwriter_falls_back_to_high_frequency_when_no_topic_match(self) -> None:
        fake = FakeProvider(None)

        plan = PlanRec(
            plan_id="plan_test",
            title="不相关主题",
            direction="backend_arch",
            user_topic="不相关主题",
            episodes=[],
        )
        ep = EpisodeRec(
            episode_id="ep_test_02",
            seq=1,
            title="完全不相关的标题",
            goal="没有匹配",
            objectives=["xyz"],
            outline="nothing",
        )

        with patch("drivecast.scriptwriter.service.get_provider", return_value=fake):
            with patch("drivecast.scriptwriter.service.upsert_script", new=AsyncMock()):
                await script_service.write_script(plan, ep)

        s2_users = [c["user"] for c in fake.calls
                    if "【本集必须覆盖的题库高频问法】" in c["user"]]
        self.assertTrue(s2_users, "S2 段 prompt 应注入题库高频问法")
        high_questions = [q for q in get_question_bank("backend_arch") if q["frequency"] == "high"]
        self.assertIn(high_questions[0]["question"], s2_users[0])


if __name__ == "__main__":
    unittest.main()

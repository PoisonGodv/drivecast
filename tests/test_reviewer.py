from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivecast.llm.provider import LLMOutputError, LLMResult
from drivecast.reviewer.service import (
    Flag,
    ReviewResult,
    ReviewerError,
    _set_provider,
    review_script,
)


class _FakeParsed:
    """模拟 provider.complete 返回的 parsed 对象。"""

    def __init__(
        self,
        verdict: str,
        flags: list[Flag],
        claims: list[str] | None = None,
    ) -> None:
        self.verdict = verdict
        self.flags = flags
        self.claims = claims or []


class FakeProvider:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    async def complete(self, **kwargs) -> LLMResult:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ReviewerServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.script = {
            "episode_id": "ep_test",
            "title": "测试集",
            "content": "Redis 的 SET 命令支持 NX 与 PX 选项,可以原子地完成加锁与设置过期时间。",
            "sections": [],
            "knowledge": ["Redis", "分布式锁"],
            "questions": [],
        }
        self.fact_cards = [
            {
                "slug": "distributed-lock-redlock",
                "title": "分布式锁与 Redlock",
                "facts": [
                    {
                        "id": "f01",
                        "claim": "SETNX+过期需一条 SET k v NX PX 原子完成",
                        "detail": "分两步在 SETNX 与 EXPIRE 间宕机会留下永不过期的死键",
                    }
                ],
            }
        ]

    def tearDown(self) -> None:
        _set_provider(None)

    async def test_pass_no_flags(self) -> None:
        fake = FakeProvider(
            LLMResult(
                content="",
                parsed=_FakeParsed("pass", []),
                model="fake-model",
                usage_tokens=0,
            )
        )
        _set_provider(fake)

        result = await review_script(self.script, self.fact_cards)

        self.assertIsInstance(result, ReviewResult)
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.flags, [])
        self.assertEqual(result.reviewer_model, "fake-model")
        self.assertTrue(result.reviewed_at.endswith("+00:00"))
        self.assertEqual(fake.calls[0]["schema"].__name__, "_ReviewRaw")

    async def test_block_with_hard_flag(self) -> None:
        flag = Flag(
            quote="Redis 6.0 开始支持多线程执行命令",
            type="hard",
            reason="与事实卡矛盾:Redis 命令执行仍是单线程,多线程仅用于网络 IO。",
            suggestion="改为说明 Redis 6.0 IO 多线程,不混淆命令执行线程模型。",
        )
        fake = FakeProvider(
            LLMResult(
                content="",
                parsed=_FakeParsed("block", [flag]),
                model="fake-model",
            )
        )
        _set_provider(fake)

        result = await review_script(self.script, self.fact_cards)

        self.assertEqual(result.verdict, "block")
        self.assertEqual(len(result.flags), 1)
        self.assertEqual(result.flags[0].type, "hard")
        self.assertEqual(result.flags[0].quote, flag.quote)

    async def test_pass_with_soft_flags(self) -> None:
        flag = Flag(
            quote="大多数公司直接使用 Redlock",
            type="soft",
            reason="事实卡未给出使用率数据,表述可能过于绝对。",
            suggestion="补充「在部分场景」或给出来源。",
        )
        fake = FakeProvider(
            LLMResult(
                content="",
                parsed=_FakeParsed("pass", [flag]),
                model="fake-model",
            )
        )
        _set_provider(fake)

        result = await review_script(self.script, self.fact_cards)

        self.assertEqual(result.verdict, "pass")
        self.assertEqual(len(result.flags), 1)
        self.assertEqual(result.flags[0].type, "soft")

    async def test_verdict_overridden_when_llm_says_pass_but_hard_flag(self) -> None:
        flag = Flag(
            quote="SETNX 和 EXPIRE 分两步也能保证原子性",
            type="hard",
            reason="与事实卡 f01 直接矛盾。",
            suggestion="改为使用 SET key value NX PX 一次性完成。",
        )
        fake = FakeProvider(
            LLMResult(
                content="",
                parsed=_FakeParsed("pass", [flag]),
                model="fake-model",
            )
        )
        _set_provider(fake)

        result = await review_script(self.script, self.fact_cards)

        self.assertEqual(result.verdict, "block")

    async def test_flags_sorted_hard_first(self) -> None:
        soft1 = Flag(quote="s1", type="soft", reason="r1", suggestion="a1")
        hard1 = Flag(quote="h1", type="hard", reason="r2", suggestion="a2")
        soft2 = Flag(quote="s2", type="soft", reason="r3", suggestion="a3")
        fake = FakeProvider(
            LLMResult(
                content="",
                parsed=_FakeParsed("block", [soft1, hard1, soft2]),
                model="fake-model",
            )
        )
        _set_provider(fake)

        result = await review_script(self.script, self.fact_cards)

        self.assertEqual([f.type for f in result.flags], ["hard", "soft", "soft"])

    async def test_reviewer_error_on_timeout(self) -> None:
        fake = FakeProvider(TimeoutError("LLM 超时"))
        _set_provider(fake)

        with self.assertRaises(ReviewerError) as ctx:
            await review_script(self.script, self.fact_cards)

        self.assertIsInstance(ctx.exception.__cause__, TimeoutError)

    async def test_reviewer_error_on_llm_output_error(self) -> None:
        fake = FakeProvider(LLMOutputError("self-heal 后仍无法解析"))
        _set_provider(fake)

        with self.assertRaises(ReviewerError) as ctx:
            await review_script(self.script, self.fact_cards)

        self.assertIsInstance(ctx.exception.__cause__, LLMOutputError)

    async def test_reviewer_error_when_parsed_missing(self) -> None:
        fake = FakeProvider(LLMResult(content="not json", parsed=None, model="fake-model"))
        _set_provider(fake)

        with self.assertRaises(ReviewerError):
            await review_script(self.script, self.fact_cards)


if __name__ == "__main__":
    unittest.main()

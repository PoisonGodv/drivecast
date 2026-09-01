"""审稿模块(docs/07 §3):对脚本做事实审查,返回结构化审查结果。

本模块只负责审查与解析;是否 fail-open/close 由上游整合阶段决定。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from ..llm.provider import LLMResult, OpenAICompatProvider, get_provider
from . import prompts


class ReviewerError(Exception):
    """审稿 LLM 调用失败或返回无法解析,需由上游决定 fail-open/close。"""


class Flag(BaseModel):
    """单条审稿标记。"""

    quote: str
    type: Literal["hard", "soft"]
    reason: str
    suggestion: str


class ReviewResult(BaseModel):
    """审稿结果,与 docs/07 §3 契约一致。"""

    verdict: Literal["pass", "block"]
    flags: list[Flag] = Field(default_factory=list)
    reviewer_model: str
    reviewed_at: str


class _ReviewRaw(BaseModel):
    """LLM 实际输出的 schema,比最终 ReviewResult 多一个 claims 字段,
    用于要求模型先显式列出 checkable claims 再做判定。
    """

    claims: list[str] = Field(default_factory=list)
    verdict: Literal["pass", "block"]
    flags: list[Flag] = Field(default_factory=list)


_provider: OpenAICompatProvider | None = None


def _get_provider() -> OpenAICompatProvider:
    """懒加载默认 LLM provider;测试可通过 _set_provider 注入 fake。"""
    global _provider
    if _provider is None:
        _provider = get_provider()
    return _provider


def _set_provider(provider: OpenAICompatProvider | None) -> None:
    """仅用于测试注入,不暴露给业务代码。"""
    global _provider
    _provider = provider


async def review_script(script: dict, fact_cards: list[dict]) -> ReviewResult:
    """对单集脚本进行事实审查。

    Args:
        script: 单集脚本 dict,至少包含 content/sections/turns/knowledge/questions。
        fact_cards: 与该集主题相关的事实卡列表。

    Returns:
        ReviewResult,verdict 由 flags 中是否存在 hard 标记决定。

    Raises:
        ReviewerError: LLM 调用失败、超时或返回无法解析的结构化输出。
    """
    provider = _get_provider()
    try:
        result: LLMResult = await provider.complete(
            system=prompts.REVIEWER_SYSTEM,
            user=prompts.reviewer_user(script, fact_cards),
            schema=_ReviewRaw,
            temperature=0.2,
        )
    except Exception as exc:
        raise ReviewerError(f"审稿 LLM 调用失败: {exc}") from exc

    raw = result.parsed
    if raw is None:
        raise ReviewerError("审稿 LLM 未返回结构化结果")

    # 判定规则:只要存在 hard 标记,无论 LLM 给的 verdict 是什么,都 block。
    verdict: Literal["pass", "block"] = (
        "block" if any(flag.type == "hard" for flag in raw.flags) else "pass"
    )
    # hard 优先,方便人工复核。
    flags = sorted(raw.flags, key=lambda f: 0 if f.type == "hard" else 1)

    return ReviewResult(
        verdict=verdict,
        flags=flags,
        reviewer_model=result.model or "",
        reviewed_at=datetime.now(timezone.utc).isoformat(),
    )

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


_SPEAKER_CN = {"host": "主持人", "expert": "专家"}


def _section_text(section: dict) -> str:
    if section.get("turns"):
        return "\n".join(f"{_SPEAKER_CN.get(t.get('speaker'), t.get('speaker', ''))}:{t.get('text', '')}"
                         for t in section["turns"])
    return str(section.get("content") or "")


def _review_chunks(script: dict, max_chars: int) -> list[tuple[str, str]]:
    """正文按段切成若干审稿块,返回 [(块说明, 纯文本)];无 sections 的旧脚本退回整篇 content。"""
    sections = [s for s in script.get("sections") or [] if _section_text(s).strip()]
    if not sections:
        return [("", str(script.get("content") or ""))]
    blocks = [(f"{s.get('key', '')} {s.get('title', '')}".strip(), _section_text(s)) for s in sections]
    chunks: list[tuple[list[str], list[str]]] = []
    size = 0
    for label, text in blocks:
        if not chunks or size + len(text) > max_chars:
            chunks.append(([], []))
            size = 0
        chunks[-1][0].append(label)
        chunks[-1][1].append(f"【{label}】\n{text}")
        size += len(text)
    if len(chunks) == 1:
        return [("", "\n\n".join(chunks[0][1]))]
    return [(f"段落 {'、'.join(labels)}", "\n\n".join(texts)) for labels, texts in chunks]


def _review_meta(script: dict) -> dict:
    questions = [q.get("question", "") for q in script.get("questions") or [] if isinstance(q, dict)]
    return {
        "标题": script.get("title", ""),
        "学习计划主题": script.get("user_topic", ""),
        "本集知识点": "、".join(str(k) for k in script.get("knowledge") or []),
        "自测题": ";".join(q for q in questions if q),
    }


async def _review_chunk(provider, meta: dict, body: str, fact_cards: list[dict], part: str) -> LLMResult:
    try:
        result: LLMResult = await provider.complete(
            system=prompts.REVIEWER_SYSTEM,
            user=prompts.reviewer_user(meta, body, fact_cards, part=part),
            schema=_ReviewRaw,
            temperature=0.2,
        )
    except Exception as exc:
        raise ReviewerError(f"审稿 LLM 调用失败{f'({part})' if part else ''}: {exc}") from exc
    if result.parsed is None:
        raise ReviewerError(f"审稿 LLM 未返回结构化结果{f'({part})' if part else ''}")
    return result


async def review_script(script: dict, fact_cards: list[dict]) -> ReviewResult:
    """对单集脚本进行事实审查。

    只送纯文本正文(不再整 dict 序列化,避免 content/turns/sections 三份重复);
    正文超过 reviewer_chunk_chars 时按段切块并行审,合并 flags。任一块失败即整体 ReviewerError。

    Raises:
        ReviewerError: LLM 调用失败、超时或返回无法解析的结构化输出。
    """
    from ..concurrency import gather_cancel
    from ..config import settings

    provider = _get_provider()
    meta = _review_meta(script)
    chunks = _review_chunks(script, max(1000, settings.reviewer_chunk_chars))
    results = await gather_cancel(*(
        _review_chunk(provider, meta, body, fact_cards, part) for part, body in chunks))

    raw_flags = [flag for r in results for flag in r.parsed.flags]
    # 判定规则:只要存在 hard 标记,无论 LLM 给的 verdict 是什么,都 block。
    verdict: Literal["pass", "block"] = (
        "block" if any(flag.type == "hard" for flag in raw_flags) else "pass"
    )
    # hard 优先,方便人工复核。
    flags = sorted(raw_flags, key=lambda f: 0 if f.type == "hard" else 1)

    return ReviewResult(
        verdict=verdict,
        flags=flags,
        reviewer_model=results[0].model or "",
        reviewed_at=datetime.now(timezone.utc).isoformat(),
    )

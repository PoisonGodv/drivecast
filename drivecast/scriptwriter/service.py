"""scriptwriter:单集大纲 → 五段式面试导向脚本,落 data/scripts/{ep}.script.json。

产物契约: docs/01 §3.2(五段)/ §4(题库);M1 文件态,question 随脚本一次产出。
"""
from __future__ import annotations

import json
import re

from ..config import ensure_data_dirs, settings
from ..llm import prompts
from ..llm.provider import get_provider
from ..persist import upsert_script
from ..schemas import EpisodeRec, PlanRec, Question, ScriptOut

_SPEAKER_CN = {"host": "主持人", "expert": "专家"}
_SECTION_KEYS = ("S0", "S1", "S2", "S3", "S4")
_SECTION_TITLES = ("回顾与开场", "核心知识点", "高频面试问法", "答题框架", "易错点与自测")


def load_script(episode_id: str) -> dict | None:
    p = settings.script_path(episode_id)
    if not p.exists():
        return None
    return json.loads(p.read_text("utf-8"))


def save_script(episode_id: str, data: dict) -> None:
    ensure_data_dirs()
    settings.script_path(episode_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), "utf-8"
    )


def _recap_questions(plan: PlanRec, seq: int) -> list[str]:
    """取上一集的 3 个自测题文本(第 1 集为空)→ 本集 S0 输入。"""
    if seq <= 1:
        return []
    prev = next((e for e in plan.episodes if e.seq == seq - 1), None)
    if prev is None:
        return []
    prev_script = load_script(prev.episode_id)
    if not prev_script:
        return []
    return [q["question"] for q in prev_script.get("questions", [])[:3]]


# 段标残留行:「开场。」「S1,核心知识点。」「S2、高频面试问法」等,剥掉不念出来
_LABEL_LINE = re.compile(
    r"^\s*(开场白?|结尾|S[0-4])\s*[,、.．:：]?\s*[^。\n]{0,20}。?\s*$"
)


def _strip_labels(content: str) -> str:
    """剥掉 LLM 写进稿子的段标/小标题行(口播噪音)。prompt 层已禁,这里兜底。"""
    lines = [ln for ln in content.splitlines() if not _LABEL_LINE.match(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _normalise_sections(out: ScriptOut, script_format: str) -> list[dict]:
    """校验五段结构并拼兼容全文；章节标题只进元数据，不进任何口播文本。"""
    if not out.sections:
        return []
    if tuple(section.key for section in out.sections) != _SECTION_KEYS:
        raise ValueError("脚本 sections 必须恰好按 S0/S1/S2/S3/S4 排列")

    sections: list[dict] = []
    flat_turns = []
    flat_content = []
    for index, section in enumerate(out.sections):
        title = section.title.strip() or _SECTION_TITLES[index]
        if script_format == "dialogue":
            if not section.turns:
                raise ValueError(f"对话式脚本 {section.key} turns 为空")
            clean_turns = [t.model_copy(update={"text": _strip_labels(t.text)}) for t in section.turns]
            turns = [{"speaker": t.speaker, "text": t.text} for t in clean_turns]
            flat_turns.extend(clean_turns)
            flat_content.extend(f"{_SPEAKER_CN[t.speaker]}:{t.text}" for t in clean_turns)
            sections.append({"key": section.key, "title": title, "turns": turns})
        else:
            content = _strip_labels(section.content)
            if not content:
                raise ValueError(f"独白式脚本 {section.key} content 为空")
            flat_content.append(content)
            sections.append({"key": section.key, "title": title, "content": content})
    out.turns = flat_turns
    out.content_md = "\n\n".join(flat_content)
    return sections


async def _gen(plan: PlanRec, ep: EpisodeRec, target_words: int, extra_note: str = "") -> ScriptOut:
    user = prompts.scriptwriter_user(plan, ep, _recap_questions(plan, ep.seq), target_words)
    if extra_note:
        user += f"\n\n【修正】{extra_note}"
    result = await get_provider().complete(
        system=prompts.scriptwriter_system(ep.script_format), user=user,
        schema=ScriptOut, temperature=0.75,
    )
    out: ScriptOut = result.parsed  # type: ignore[assignment]
    sections = _normalise_sections(out, ep.script_format)
    if not sections:
        # 兼容旧 provider/测试桩输出；新生成 prompt 会稳定给 sections。
        if ep.script_format == "dialogue":
            if not out.turns:
                raise ValueError("对话式脚本 turns 为空")
            out.content_md = "\n\n".join(f"{_SPEAKER_CN[t.speaker]}:{t.text}" for t in out.turns)
        elif not out.content_md:
            raise ValueError("独白式脚本 content_md 为空")
    out.content_md = _strip_labels(out.content_md)
    out._sections = sections  # type: ignore[attr-defined]
    out._usage = result.usage_tokens  # type: ignore[attr-defined]
    return out


async def write_script(plan: PlanRec, ep: EpisodeRec, *, force: bool = False) -> dict:
    """生成单集脚本。幂等:已有脚本且非 force 则直接返回(docs/02 §6 episode 级幂等)。"""
    existing = load_script(ep.episode_id)
    if existing and not force:
        return existing

    target_words = ep.target_minutes * settings.words_per_minute
    out = await _gen(plan, ep, target_words)

    # 长度门禁:落在 0.75-1.25 倍区间外,带修正说明重出一次
    n = len(out.content_md)
    if not (target_words * 0.75 <= n <= target_words * 1.25):
        print(f"[script] 字数 {n} 超出目标区间 [{int(target_words * 0.75)}, {int(target_words * 1.25)}],修正重出")
        out = await _gen(plan, ep, target_words,
                         f"上一次正文 {n} 字,不符合要求。请重写,严格控制在 {int(target_words * 0.9)} 到 {int(target_words * 1.1)} 字之间,宁精勿滥。")

    # 质量校验:五段内容长度 + 恰好 3 道自测题(docs/02 §6 输出校验)
    n_chars = len(out.content_md)
    if n_chars < target_words * 0.6:
        print(f"[warn] {ep.episode_id} 脚本报备字数 {target_words},实际只有 {n_chars} 字,内容可能缩水")

    questions = out.questions[:3]
    while len(questions) < 3:  # LLM 少给了就占位,保证结构完整(M5 复习闭环依赖 3 题/集)
        questions.append(Question(qtype="concept", question="(本集自测题缺失,请重生成)", framework=[], difficulty="medium"))

    data = {
        "episode_id": ep.episode_id,
        "plan_id": plan.plan_id,
        "seq": ep.seq,
        "title": ep.title,
        "script_format": ep.script_format,
        "content": out.content_md,
        "turns": [t.model_dump() for t in out.turns],  # 兼容展示/TTS；章节源见 sections
        "sections": getattr(out, "_sections", []),  # 稳定五段结构，标题永不朗读
        "knowledge": out.knowledge,
        "questions": [q.model_dump() for q in questions],
        "char_count": n_chars,
        "target_words": target_words,
        "usage_tokens": getattr(out, "_usage", 0),
        "status": "scripted",
    }
    save_script(ep.episode_id, data)
    _wipe_derived(ep.episode_id)  # 脚本变了,下游产物全部作废
    await upsert_script(ep.episode_id, data)  # 双写 DB:script + question(docs/05 M2-5)
    return data


def _wipe_derived(episode_id: str) -> None:
    """脚本重生成后,tts.json/chunks/最终音频全部作废(防旧 chunk 被当成断点续跑)。"""
    import shutil
    settings.tts_path(episode_id).unlink(missing_ok=True)
    settings.episode_audio_path(episode_id).unlink(missing_ok=True)
    settings.meta_path(episode_id).unlink(missing_ok=True)
    shutil.rmtree(settings.chunks_dir(episode_id), ignore_errors=True)

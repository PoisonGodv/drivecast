"""scriptwriter:单集大纲 → 五段式面试导向脚本,落 data/scripts/{ep}.script.json。

产物契约: docs/01 §3.2(五段)/ §4(题库);M1 文件态,question 随脚本一次产出。

生成方式:不用一个大 prompt 写完整集,而是拆成 5 个分段 agent(每段一次独立 LLM 调用):
- S0/S1/S2 无相互依赖,并行生成(S2 注入题库高频问法做 grounding);
- S3 依赖 S2 的问法,串行跟进;
- S4 依赖 S1/S2 的内容,串行收尾,并顺带产出本集 3 道自测题;
- 每段独立做字数门禁,不合格只重出该段,不牵连其他段。

长集(如 40 分钟)二次拆分:单段目标字数超过 settings.section_max_words_per_agent 时,
该段再拆成多个并行子 agent(S1 按知识点切片、S2 按题库问法切片、S3 按问法顺序均分),
各带分工说明独立生成后拼接回原段,单次调用字数始终受控。
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone

from .. import reviewer
from ..config import ensure_data_dirs, settings
from ..content.loader import get_fact_cards, get_question_bank
from ..llm import prompts
from ..llm.provider import get_provider
from ..persist import upsert_script
from ..refs import hunter
from ..schemas import (
    EpisodeRec,
    PlanRec,
    Question,
    QuizSectionOut,
    ScriptOut,
    ScriptSection,
    SectionOut,
)

_SPEAKER_CN = {"host": "主持人", "expert": "专家"}
_SECTION_KEYS = ("S0", "S1", "S2", "S3", "S4")
_SECTION_TITLES = ("回顾与开场", "核心知识点", "高频面试问法", "答题框架", "易错点与自测")
# 五段时间占比(docs/01 §3.2:1/5/4/3/2 分钟,共 15 分钟),用于把整集字数预算切给各段 agent
_SECTION_SHARE = {"S0": 1 / 15, "S1": 5 / 15, "S2": 4 / 15, "S3": 3 / 15, "S4": 2 / 15}


class ScriptBlockedError(RuntimeError):
    """脚本被事实审稿拦截(hard 硬伤),job 层按普通失败走重试/失败路径(docs/07 §3.1)。"""


def _episode_text(plan: PlanRec, ep: EpisodeRec) -> str:
    """提取 episode 主题文本,用于题库关键词匹配。"""
    parts = [
        plan.title,
        ep.title,
        ep.goal,
        ep.outline,
        plan.direction,
    ] + list(ep.objectives)
    return " ".join(str(p) for p in parts if p).lower()


def _match_questions(plan: PlanRec, ep: EpisodeRec, min_count: int = 2) -> list[dict]:
    """按 episode 主题关键词匹配题库,不足时以方向高频题补齐(docs/07 §2)。"""
    text = _episode_text(plan, ep)
    all_questions = get_question_bank(plan.direction)
    matched: list[dict] = []
    seen: set[str] = set()
    for q in all_questions:
        qid = q.get("id")
        topics = [str(t).lower() for t in q.get("topic", []) if t]
        if any(topic in text for topic in topics):
            if qid not in seen:
                matched.append(q)
                seen.add(qid)
    if len(matched) < min_count:
        high = [q for q in all_questions if q.get("frequency") == "high" and q.get("id") not in seen]
        matched.extend(high[: min_count - len(matched)])
    return matched[:6]  # 注入上限,避免 prompt 过长


def _s2_grounding_prompt(questions: list[dict]) -> str:
    """生成 S2 高频问法注入文本。"""
    if not questions:
        return ""
    lines = ["【本集必须覆盖的题库高频问法】(S2 段至少包含其中 2 题,可改写题面但保留考点)"]
    for q in questions:
        lines.append(f"- {q.get('question', '')}")
    return "\n".join(lines)


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


async def _review(data: dict, ep: EpisodeRec, matched: list[dict]) -> dict:
    """TTS 前事实审稿(docs/07 §3.1):hard 拦截,ReviewerError fail-open 放行留痕。

    事实卡按已匹配题库题的 topic 关键词选取,最多 3 张防 prompt 膨胀。
    """
    keywords = sorted({str(t) for q in matched for t in q.get("topic", []) if t})
    cards = get_fact_cards(keywords)[:3]
    try:
        result = await reviewer.review_script(data, cards)
    except reviewer.ReviewerError as exc:
        print(f"[reviewer] {ep.episode_id} 审稿失败,fail-open 放行: {exc}")
        return {
            "verdict": "pass", "flags": [], "reviewer_model": "",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "pending_error": str(exc),
        }
    if result.verdict == "block":
        hard = [f for f in result.flags if f.type == "hard"]
        summary = ";".join(f"{f.quote[:40]}→{f.reason[:60]}" for f in hard[:3])
        raise ScriptBlockedError(f"{ep.episode_id} 事实审稿拦截({len(hard)} 处硬伤): {summary}")
    return result.model_dump()


async def _gen_section(plan: PlanRec, ep: EpisodeRec, key: str, target_words: int,
                       *, recap_questions: list[str] | None = None,
                       s2_grounding: str = "", context: str = "",
                       extra_note: str = "", part_note: str = "",
                       plain: bool = False) -> tuple[ScriptSection, list[Question], int]:
    """单段 agent:一次独立 LLM 调用,只写 key 这一段。返回 (section, questions, usage)。

    part_note:长段拆分时本部分的分工说明(跨重试保留,与 extra_note 修正说明并存)。
    plain:S4 拆分时非末段不出自测题,用 SectionOut schema。
    """
    quiz = key == "S4" and not plain
    schema = QuizSectionOut if quiz else SectionOut
    user = prompts.section_user(plan, ep, key, target_words,
                                recap_questions=recap_questions,
                                s2_grounding=s2_grounding, context=context,
                                part_note=part_note)
    if extra_note:
        user += f"\n\n【修正】{extra_note}"
    result = await get_provider().complete(
        system=prompts.section_system(ep.script_format, key, quiz=quiz), user=user,
        schema=schema, temperature=0.75,
    )
    out = result.parsed
    if not isinstance(out, SectionOut):  # 兼容返回整集 ScriptOut 的旧测试桩
        raise ValueError(f"分段 agent 输出类型不符:期望 {schema.__name__},实际 {type(out).__name__}")
    section = ScriptSection(key=key, title=out.title, content=out.content, turns=out.turns)
    questions = out.questions if isinstance(out, QuizSectionOut) else []
    return section, questions, result.usage_tokens


def _section_len(section: ScriptSection, script_format: str) -> int:
    if script_format == "dialogue":
        return sum(len(t.text) for t in section.turns)
    return len(section.content)


def _section_plain(section: ScriptSection, script_format: str) -> str:
    """段的纯文本形态,作为后续段 agent 的衔接上下文。"""
    if script_format == "dialogue":
        return "\n".join(f"{_SPEAKER_CN[t.speaker]}:{t.text}" for t in section.turns)
    return section.content


async def _gen_section_checked(plan: PlanRec, ep: EpisodeRec, key: str, target_words: int,
                               **kwargs) -> tuple[ScriptSection, list[Question], int]:
    """单段生成 + 门禁:结构为空或字数落在 0.75-1.25 倍区间外,带修正说明只重出本段一次。"""
    import time
    started = time.monotonic()
    section, questions, usage = await _gen_section(plan, ep, key, target_words, **kwargs)
    n = _section_len(section, ep.script_format)
    if n == 0:
        raise ValueError(f"分段脚本 {key} 内容为空(script_format={ep.script_format})")
    if not (target_words * 0.75 <= n <= target_words * 1.25):
        print(f"[script] {ep.episode_id} {key} 字数 {n} 超出目标区间 "
              f"[{int(target_words * 0.75)}, {int(target_words * 1.25)}],本段修正重出")
        note = (f"上一次本段正文 {n} 字,不符合要求。请重写本段,严格控制在 "
                f"{int(target_words * 0.9)} 到 {int(target_words * 1.1)} 字之间,宁精勿滥。")
        retry, retry_questions, retry_usage = await _gen_section(
            plan, ep, key, target_words, extra_note=note, **kwargs)
        usage += retry_usage
        if _section_len(retry, ep.script_format) > 0:  # 重出为空则保留首版,不至于开天窗
            section, questions = retry, retry_questions
    print(f"[script] {ep.episode_id} {key} 完成，耗时 {time.monotonic() - started:.1f}s")
    return section, questions, usage


def _split_count(target_words: int) -> int:
    """该段需要拆成几个并行子 agent;不超上限时恒为 1(行为与拆分前完全一致)。"""
    max_words = max(1, settings.section_max_words_per_agent)
    return max(1, -(-target_words // max_words))  # ceil 除法


def _contiguous_slices(items: list[str], parts: int) -> list[list[str]]:
    """把有序要点连续均分成 parts 片(保序,拼接后顺序不变);要点不足时允许空片。"""
    if not items:
        return [[] for _ in range(parts)]
    base, rem = divmod(len(items), parts)
    slices, start = [], 0
    for i in range(parts):
        size = base + (1 if i < rem else 0)
        slices.append(items[start:start + size])
        start += size
    return slices


async def _gen_section_split(plan: PlanRec, ep: EpisodeRec, key: str, target_words: int,
                             *, scope_items: list[str] | None = None,
                             scope_style: str = "", **kwargs):
    """长段二次拆分:目标字数超单 agent 上限时,拆成多个并行子 agent 写同一 key 段再拼接。

    每个子 agent 拿到自己的字数预算与分工说明(part_note),S4 拆分时自测题只由末段产出。
    不拆时直接退回 _gen_section_checked,行为与拆分前逐字节一致。
    """
    parts = _split_count(target_words)
    if parts == 1:
        return await _gen_section_checked(plan, ep, key, target_words, **kwargs)

    per = max(120, target_words // parts)
    scopes = _contiguous_slices(list(scope_items or []), parts)
    print(f"[script] {ep.episode_id} {key} 目标 {target_words} 字超单 agent 上限,拆 {parts} 个子 agent 并行")

    async def _part(index: int):
        part_kwargs = dict(kwargs)
        if key == "S2":
            # 拆分后整段 grounding 改由各部分的 part_note 按切片携带,避免「至少 2 题」与切片冲突
            part_kwargs.pop("s2_grounding", None)
        note = prompts.split_part_note(key, index, parts, per, scopes[index], scope_style)
        return await _gen_section_checked(
            plan, ep, key, per, part_note=note,
            plain=key == "S4" and index < parts - 1,  # 自测题只由末段产出
            **part_kwargs)

    from ..concurrency import gather_cancel
    results = await gather_cancel(*(_part(i) for i in range(parts)))
    sections = [r[0] for r in results]
    if ep.script_format == "dialogue":
        merged = ScriptSection(
            key=key, title=sections[0].title,
            turns=[turn for section in sections for turn in section.turns],
        )
    else:
        merged = ScriptSection(
            key=key, title=sections[0].title,
            content="\n\n".join(s.content for s in sections if s.content.strip()),
        )
    return merged, results[-1][1], sum(r[2] for r in results)


async def _gen_sections(plan: PlanRec, ep: EpisodeRec, target_words: int,
                        s2_questions: list[dict]) -> ScriptOut:
    """分段 agent 编排:S0/S1/S2 并行 → S3 跟 S2 → S4 跟 S1+S2,拼回整集 ScriptOut。

    长集(S1/S2/S3 超单 agent 字数上限)由 _gen_section_split 内部再拆成并行子 agent。
    """
    targets = {k: max(120, int(target_words * share)) for k, share in _SECTION_SHARE.items()}
    if ep.seq <= 1:
        # 首集 S0 是 30 秒开场而非回顾,预算减半,省下的匀给核心段 S1
        saved = targets["S0"] // 2
        targets["S0"] -= saved
        targets["S1"] += saved

    # 各段的拆分切片源:S1 按目标知识点,S2 按命中题库问法,S3 按 S2 问法顺序
    s2_scope = [str(q.get("question", "")).strip() for q in s2_questions if q.get("question")]
    from ..concurrency import gather_cancel
    tasks = []

    def start(aw):
        task = asyncio.create_task(aw)
        tasks.append(task)
        return task

    start(_gen_section_checked(
        plan, ep, "S0", targets["S0"], recap_questions=_recap_questions(plan, ep.seq)))
    s1_task = start(_gen_section_split(
        plan, ep, "S1", targets["S1"],
        scope_items=[str(o) for o in ep.objectives], scope_style="objectives"))
    s2_task = start(_gen_section_split(
        plan, ep, "S2", targets["S2"], scope_items=s2_scope, scope_style="questions",
        s2_grounding=_s2_grounding_prompt(s2_questions)))

    async def gen_s3():
        s2 = await s2_task
        return await _gen_section_split(
            plan, ep, "S3", targets["S3"], scope_style="order",
            context=_section_plain(s2[0], ep.script_format))

    async def gen_s4():
        s1, s2 = await gather_cancel(s1_task, s2_task)
        return await _gen_section_split(
            plan, ep, "S4", targets["S4"],
            context=_section_plain(s1[0], ep.script_format)
                    + "\n\n" + _section_plain(s2[0], ep.script_format))

    start(gen_s3())
    start(gen_s4())
    runs = await gather_cancel(*tasks)
    s4 = runs[4]
    out = ScriptOut(
        sections=[run[0] for run in runs],
        questions=s4[1],  # 自测题由 S4 agent 随段产出
        knowledge=list(ep.objectives),
    )
    out._sections = _normalise_sections(out, ep.script_format)  # type: ignore[attr-defined]
    out._usage = sum(run[2] for run in runs)  # type: ignore[attr-defined]
    return out


_REF_CN_NUMERALS = "一二三四五六"


def _references_outro(references: list[dict]) -> str:
    """片尾参考资料口播(确定性拼接,不过 LLM):只念资料标题,URL 留在文稿末尾点击。

    口播最多念 6 条有标题的;一条可念标题都没有但确有参考链接时,退化为一句总起口播,
    保证「音频最后一定交代参考文献」。整条列表为空则不出口播。
    """
    if not references:
        return ""
    closer = "这些资料的链接已经附在本集文稿末尾,到站之后欢迎点开读原文。"
    titles: list[str] = []
    for ref in references:
        title = str(ref.get("title") or "").strip()
        url = str(ref.get("url") or "").strip()
        if title and title != url:
            titles.append(title[:40])  # 超长标题截断,口播不念又臭又长的页面名
    if not titles:
        return f"本期内容参考了若干资料,{closer}"
    spoken = ";".join(
        f"第{_REF_CN_NUMERALS[i]}条,{title}" for i, title in enumerate(titles[:6])
    )
    return f"本期内容参考了以下资料:{spoken}。{closer}"


async def _write_script_with_refs(plan: PlanRec, ep: EpisodeRec, matched: list[dict], refs_task) -> dict:
    target_words = ep.target_minutes * settings.words_per_minute
    out = await _gen_sections(plan, ep, target_words, matched)

    # 质量校验:整集字数明显缩水时告警(分段门禁已在各段内做过一轮修正重出)
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
        "target_minutes": ep.target_minutes,
        "words_per_minute_at_generation": settings.words_per_minute,
        "usage_tokens": getattr(out, "_usage", 0),
        "status": "scripted",
    }

    if settings.reviewer_enabled:
        data["review"] = await _review(data, ep, matched)
    references = await refs_task
    data["references"] = references  # 供复习卡片/脚本页读原文;旧脚本无此字段,读取处用 .get

    # 片尾参考文献口播(docs/03 §2):拼进文稿末尾(展示与音频一致),TTS 落成 S5 章节;
    # 在审稿之后追加——口播内容确定性地来自已校验链接,无事实性风险,不送审
    outro = _references_outro(references)
    if outro:
        data["refs_outro"] = outro
        if ep.script_format == "dialogue":
            data["content"] += f"\n\n主持人:{outro}"
            data["turns"].append({"speaker": "host", "text": outro})
        else:
            data["content"] += f"\n\n{outro}"

    save_script(ep.episode_id, data)
    _wipe_derived(ep.episode_id)  # 脚本变了,下游产物全部作废
    await upsert_script(ep.episode_id, data)  # 双写 DB:script + question(docs/05 M2-5)
    return data


async def write_script(plan: PlanRec, ep: EpisodeRec, *, force: bool = False) -> dict:
    """先并行猎取引用，正文通过审稿后才保存；失败回收所有请求。"""
    existing = load_script(ep.episode_id)
    if existing and not force:
        return existing
    from ..concurrency import gather_cancel
    matched = _match_questions(plan, ep)
    keywords = sorted({str(t) for q in matched for t in q.get("topic", []) if t})
    cards = get_fact_cards(keywords)[:3]
    refs_task = asyncio.create_task(hunter.hunt_references(plan, ep, matched, cards))
    result, _ = await gather_cancel(
        _write_script_with_refs(plan, ep, matched, refs_task), refs_task)
    return result


def _wipe_derived(episode_id: str) -> None:
    """脚本重生成后,tts.json/chunks/最终音频全部作废(防旧 chunk 被当成断点续跑)。"""
    import shutil
    settings.tts_path(episode_id).unlink(missing_ok=True)
    settings.episode_audio_path(episode_id).unlink(missing_ok=True)
    settings.meta_path(episode_id).unlink(missing_ok=True)
    shutil.rmtree(settings.chunks_dir(episode_id), ignore_errors=True)

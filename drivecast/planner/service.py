"""planner:主题+方向 → N 集递进学习计划,落 data/plans/{plan_id}.json。

M1 文件态(M2 双写 DB,docs/05 M2-1)。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from ..config import ensure_data_dirs, settings
from ..content.loader import get_question_bank
from ..llm import prompts
from ..llm.provider import get_provider
from ..persist import upsert_plan
from ..schemas import Direction, EpisodeRec, PlanOut, PlanRec

# 对话式默认方向(docs/01 §3.1:②③以对话为主,①④以独白为主)
DIALOGUE_DIRECTIONS = {"ai_llm", "tech_news"}


async def _covered_question_ids(user_id: int = 1) -> set[str]:
    """汇总「该用户」已覆盖的题库题 id。

    plan 文件本身无归属字段,按 DB plan.user_id 取 ext_id 再读对应文件;
    DB 不可用(纯文件态单测)退化为全量扫描——单用户语义下等价。
    """
    covered: set[str] = set()
    plans_dir = settings.data_dir / "plans"
    if not plans_dir.exists():
        return covered
    paths: list[Path] | None = None
    try:
        from sqlalchemy import select

        from ..db import session
        from ..models import Plan

        async with session() as s:
            ext_ids = (
                await s.execute(select(Plan.ext_id).where(Plan.user_id == user_id))
            ).scalars().all()
        paths = [plans_dir / f"{ext}.json" for ext in ext_ids]
    except Exception:  # 纯文件态运行(单测/未建表)退化为全量
        paths = None
    if paths is None:
        paths = sorted(plans_dir.glob("plan_*.json"))
    for path in paths:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue  # 坏文件/占位行未落文件,跳过不阻断新计划生成
        for ep in data.get("episodes", []):
            for qid in ep.get("covers") or []:
                covered.add(str(qid))
    return covered


async def _grounding_prompt(direction: str, user_id: int = 1) -> str:
    """注入全量题簇地图 + 该用户已覆盖题集合,大纲按「未覆盖高频题簇优先」组织(docs/07 §2)。

    按 topic 首元素聚簇:每簇给高/中/低频题数统计与代表性题面(高频全列、
    中低频每簇最多 3 条),并附题 id 供 LLM 填 covers;控制总量防 prompt 爆炸。
    覆盖集合必须按用户取,否则 A 的学习进度会把 B 的大纲高频题标成 [已覆盖]。
    """
    questions = get_question_bank(direction)
    if not questions:
        return ""
    covered = await _covered_question_ids(user_id)

    # loader 已按 frequency(high→mid→low)稳定排序,聚簇保序即可
    clusters: dict[str, list[dict]] = {}
    for q in questions:
        topics = q.get("topic") or []
        key = str(topics[0]) if topics else "未分类"
        clusters.setdefault(key, []).append(q)

    lines = [
        "【题库题簇地图】(按主题聚簇;每集必须从中选取 covers,已覆盖的题标注 [已覆盖],"
        "大纲优先覆盖未覆盖的高频簇)"
    ]
    for key, qs in clusters.items():
        highs = [q for q in qs if q.get("frequency") == "high"]
        mids = [q for q in qs if q.get("frequency") == "mid"]
        lows = [q for q in qs if q.get("frequency") == "low"]
        lines.append(f"■ 簇「{key}」(高频 {len(highs)} / 中频 {len(mids)} / 低频 {len(lows)})")
        for q in highs + (mids + lows)[:3]:  # 高频全列,中低频代表题最多 3 条
            mark = " [已覆盖]" if str(q.get("id")) in covered else ""
            lines.append(f"  - {q.get('id')}: {q.get('question', '')}{mark}")
    lines.append(
        f"(已覆盖题 id 共 {len(covered)} 个;covers 只能从上面列出的题 id 中选取,不要杜撰)"
    )
    return "\n".join(lines)


async def new_plan_id() -> str:
    """plan_YYYYMMDD_NN,NN 为当日序号(DB 占位行与文件取较大值,防同日并发重号)。

    Web 创建改为异步后,接口先落 generating 占位 Plan 行、文件要等 worker 调完
    大模型才写;只数文件会在同日二次创建时撞号,所以以 DB 计数为准、文件兜底。
    """
    today = datetime.now().strftime("%Y%m%d")
    file_count = 0
    plans_dir = settings.data_dir / "plans"
    if plans_dir.exists():
        file_count = sum(1 for p in plans_dir.glob(f"plan_{today}_*.json"))
    db_count = 0
    try:
        from sqlalchemy import func, select

        from ..db import session
        from ..models import Plan

        async with session() as s:
            db_count = await s.scalar(
                select(func.count(Plan.id)).where(Plan.ext_id.like(f"plan_{today}_%"))
            ) or 0
    except Exception:  # 纯文件态运行(单测/未建表)退化为文件计数
        db_count = 0
    return f"plan_{today}_{max(file_count, db_count) + 1:02d}"


def load_plan(plan_id: str) -> PlanRec:
    return PlanRec.model_validate(json.loads(settings.plan_path(plan_id).read_text("utf-8")))


def save_plan(plan: PlanRec) -> None:
    ensure_data_dirs()
    settings.plan_path(plan.plan_id).write_text(
        plan.model_dump_json(indent=2), "utf-8"
    )


async def create_plan(
    topic: str,
    direction: Direction = "backend_arch",
    episode_count: int | None = None,
    target_minutes: int | None = None,
    plan_id: str | None = None,
    user_id: int | None = None,
) -> PlanRec:
    """user_id 仅在 Web 异步链路由 worker 传入(job.user_id);CLI/测试缺省 admin(=1)。"""
    count = episode_count or settings.default_episode_count
    minutes = target_minutes or settings.target_minutes

    user = prompts.planner_user(topic, direction, count, target_minutes=minutes)
    grounding = await _grounding_prompt(direction, user_id or 1)
    if grounding:
        user += f"\n\n{grounding}"

    result = await get_provider().complete(
        system=prompts.PLANNER_SYSTEM,
        user=user,
        schema=PlanOut,
        temperature=0.7,
    )
    out: PlanOut = result.parsed  # type: ignore[assignment]

    # Web 异步创建由接口预分配 plan_id(占位行 ext_id);CLI 缺省现场分配
    plan_id = plan_id or await new_plan_id()
    short = plan_id.removeprefix("plan_")  # 20260820_01
    default_format = "dialogue" if direction in DIALOGUE_DIRECTIONS else "monologue"
    # 排期:每日新学 N 集,第 seq 集落在创建日后第 (seq-1)//N 天
    created = datetime.now()
    per_day = max(settings.daily_new_episodes, 1)
    # covers 只允许取注入过的真实题 id,LLM 杜撰的一律丢弃
    valid_qids = {str(q.get("id")) for q in get_question_bank(direction)}
    episodes: list[EpisodeRec] = []
    prev_id: str | None = None
    for ep in out.episodes[:count]:
        ep_id = f"ep_{short}_{ep.seq:02d}"
        scheduled = created.date() + timedelta(days=(ep.seq - 1) // per_day)
        episodes.append(EpisodeRec(
            episode_id=ep_id,
            seq=ep.seq,
            title=ep.title,
            goal=ep.goal,
            objectives=ep.objectives,
            outline=ep.outline,
            depends_on=[prev_id] if prev_id else [],
            covers=[str(qid) for qid in ep.covers if str(qid) in valid_qids][:6],
            scheduled_date=scheduled.isoformat(),
            target_minutes=minutes,
            script_format=default_format,
        ))
        prev_id = ep_id

    plan = PlanRec(
        plan_id=plan_id,
        title=out.title,
        direction=direction,
        user_topic=topic,
        target_minutes=minutes,
        created_at=created.isoformat(timespec="seconds"),
        episodes=episodes,
    )
    save_plan(plan)
    await upsert_plan(plan, user_id=user_id)  # 双写 DB(docs/05 M2-1)
    return plan

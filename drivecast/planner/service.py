"""planner:主题+方向 → N 集递进学习计划,落 data/plans/{plan_id}.json。

M1 文件态(M2 双写 DB,docs/05 M2-1)。
"""
from __future__ import annotations

import json
from datetime import datetime

from ..config import ensure_data_dirs, settings
from ..llm import prompts
from ..llm.provider import get_provider
from ..persist import upsert_plan
from ..schemas import Direction, EpisodeRec, PlanOut, PlanRec

# 对话式默认方向(docs/01 §3.1:②③以对话为主,①④以独白为主)
DIALOGUE_DIRECTIONS = {"ai_llm", "tech_news"}


def _new_plan_id() -> str:
    """plan_YYYYMMDD_NN,NN 为当日序号。"""
    today = datetime.now().strftime("%Y%m%d")
    seq = 1
    plans_dir = settings.data_dir / "plans"
    if plans_dir.exists():
        seq += sum(1 for p in plans_dir.glob(f"plan_{today}_*.json"))
    return f"plan_{today}_{seq:02d}"


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
) -> PlanRec:
    count = episode_count or settings.default_episode_count
    minutes = target_minutes or settings.target_minutes

    result = await get_provider().complete(
        system=prompts.PLANNER_SYSTEM,
        user=prompts.planner_user(topic, direction, count),
        schema=PlanOut,
        temperature=0.7,
    )
    out: PlanOut = result.parsed  # type: ignore[assignment]

    plan_id = _new_plan_id()
    short = plan_id.removeprefix("plan_")  # 20260820_01
    default_format = "dialogue" if direction in DIALOGUE_DIRECTIONS else "monologue"
    episodes: list[EpisodeRec] = []
    prev_id: str | None = None
    for ep in out.episodes[:count]:
        ep_id = f"ep_{short}_{ep.seq:02d}"
        episodes.append(EpisodeRec(
            episode_id=ep_id,
            seq=ep.seq,
            title=ep.title,
            goal=ep.goal,
            objectives=ep.objectives,
            outline=ep.outline,
            depends_on=[prev_id] if prev_id else [],
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
        created_at=datetime.now().isoformat(timespec="seconds"),
        episodes=episodes,
    )
    save_plan(plan)
    await upsert_plan(plan)  # 双写 DB(docs/05 M2-1)
    return plan

"""大纲确认流(docs/04 §1.2):大纲生成后计划停在 draft,用户删改确认后才转 active 开写。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

from .. import quota
from ..auth import get_current_user
from ..config import settings
from ..db import session
from ..jobs import pipeline
from ..models import Episode, Job, Plan, User
from ..planner import service as planner
from ..schemas import PlanRec

router = APIRouter(prefix="/api", tags=["outline"])


class EpisodePatch(BaseModel):
    id: int = Field(gt=0)  # 集的数字 id(与 GET /api/plans/{id} 一致)
    title: str | None = Field(default=None, max_length=200)
    goal: str | None = Field(default=None, max_length=500)
    outline: str | None = Field(default=None, max_length=2000)
    objectives: list[str] | None = Field(default=None, max_length=10)


class OutlinePatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    episodes: list[EpisodePatch] = Field(default_factory=list)
    delete: list[int] = Field(default_factory=list)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": 404, "message": "plan not found"})


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": 422, "message": message})


async def _owned_plan(plan_id: int, user_id: int) -> Plan:
    async with session() as s:
        row = await s.scalar(select(Plan).where(Plan.id == plan_id, Plan.user_id == user_id))
    if row is None:
        raise _not_found()
    return row


def _require_draft(row: Plan) -> None:
    if row.status != "draft":
        raise HTTPException(status_code=409, detail={
            "code": 409, "message": f"plan is {row.status}, outline is locked"})


async def _outline_view(row: Plan, plan: PlanRec) -> dict:
    async with session() as s:
        ids = dict((await s.execute(
            select(Episode.ext_id, Episode.id).where(Episode.plan_id == row.id))).all())
    return {
        "plan_id": row.id,
        "status": row.status,
        "title": plan.title,
        "episodes": [
            {
                "id": ids.get(ep.episode_id), "ext_id": ep.episode_id, "seq": ep.seq,
                "title": ep.title, "goal": ep.goal, "outline": ep.outline,
                "objectives": ep.objectives, "covers": ep.covers,
                "scheduled_date": ep.scheduled_date,
            }
            for ep in sorted(plan.episodes, key=lambda e: e.seq)
        ],
    }


def _created_day(plan: PlanRec) -> date:
    try:
        return datetime.fromisoformat(plan.created_at).date()
    except ValueError:
        return date.today()


@router.get("/plans/{plan_id}/outline")
async def get_outline(plan_id: int, user: User = Depends(get_current_user)) -> dict:
    """完整大纲(含目标/大纲/知识点);跨用户与不存在同 404。"""
    row = await _owned_plan(plan_id, user.id)
    if not settings.plan_path(row.ext_id).exists():
        raise HTTPException(status_code=409, detail={"code": 409, "message": "大纲尚未生成"})
    return await _outline_view(row, planner.load_plan(row.ext_id))


@router.patch("/plans/{plan_id}/outline")
async def patch_outline(plan_id: int, body: OutlinePatch, user: User = Depends(get_current_user)) -> dict:
    """draft 期编辑大纲:改标题/目标/大纲/知识点、删集(至少留 1 集,seq 连续重排,ext_id 不变)。"""
    row = await _owned_plan(plan_id, user.id)
    async with pipeline.get_plan_lifecycle_lock(row.ext_id):
        row = await _owned_plan(plan_id, user.id)
        _require_draft(row)
        plan = planner.load_plan(row.ext_id)
        async with session() as s:
            ext_by_id = dict((await s.execute(
                select(Episode.id, Episode.ext_id).where(Episode.plan_id == row.id))).all())
        referenced = {p.id for p in body.episodes} | set(body.delete)
        unknown = referenced - set(ext_by_id)
        if unknown:
            raise _unprocessable(f"episode not in plan: {sorted(unknown)}")
        deleted_ext = {ext_by_id[i] for i in body.delete}
        remaining = sorted((e for e in plan.episodes if e.episode_id not in deleted_ext),
                           key=lambda e: e.seq)
        if not remaining:
            raise _unprocessable("至少保留 1 集")

        by_ext = {e.episode_id: e for e in remaining}
        for patch in body.episodes:
            ep = by_ext.get(ext_by_id[patch.id])
            if ep is None:
                continue  # 同一请求里既改又删:以删除为准
            if patch.title is not None:
                if not patch.title.strip():
                    raise _unprocessable("集标题不能为空")
                ep.title = patch.title.strip()
            if patch.goal is not None:
                ep.goal = patch.goal.strip()
            if patch.outline is not None:
                ep.outline = patch.outline.strip()
            if patch.objectives is not None:
                ep.objectives = [o.strip() for o in patch.objectives if o.strip()]
        if body.title is not None:
            if not body.title.strip():
                raise _unprocessable("计划标题不能为空")
            plan.title = body.title.strip()

        # 删集后 seq 连续重排,回顾依赖与排期随新 seq 重算(与 planner.create_plan 同口径)
        created = _created_day(plan)
        per_day = max(settings.daily_new_episodes, 1)
        prev_id: str | None = None
        for index, ep in enumerate(remaining, start=1):
            ep.seq = index
            ep.depends_on = [prev_id] if prev_id else []
            ep.scheduled_date = (created + timedelta(days=(index - 1) // per_day)).isoformat()
            prev_id = ep.episode_id
        plan.episodes = remaining

        async with session() as s:
            if deleted_ext:
                await s.execute(delete(Job).where(Job.ref_id.in_(deleted_ext)))
                await s.execute(delete(Episode).where(
                    Episode.plan_id == row.id, Episode.ext_id.in_(deleted_ext)))
            # UNIQUE(plan_id, seq):先挪到负数临时位,再写最终 seq,避免重排中途撞约束
            await s.execute(update(Episode).where(Episode.plan_id == row.id)
                            .values(seq=-Episode.id))
            for ep in remaining:
                await s.execute(
                    update(Episode).where(Episode.ext_id == ep.episode_id).values(
                        seq=ep.seq, title=ep.title, outline=ep.outline,
                        objectives=json.dumps(ep.objectives, ensure_ascii=False),
                        scheduled_date=ep.scheduled_date or None,
                    ))
            await s.execute(update(Plan).where(Plan.id == row.id).values(
                title=plan.title, episode_count=len(remaining)))
            await s.commit()
        planner.save_plan(plan)
        row.title = plan.title
        return await _outline_view(row, plan)


@router.post("/plans/{plan_id}/confirm")
async def confirm_outline(plan_id: int, user: User = Depends(get_current_user)) -> dict:
    """确认大纲:draft → active 并入队全部集;非 draft(含已确认)一律 409,重复点击不会重复入队。"""
    row = await _owned_plan(plan_id, user.id)
    async with pipeline.get_plan_lifecycle_lock(row.ext_id):
        row = await _owned_plan(plan_id, user.id)
        _require_draft(row)
        plan = planner.load_plan(row.ext_id)
        # 大纲出完后 plan job 已结束、不再预占额度,确认时按将新建的 script job 重新计额
        async with quota.reserve(user, await quota.new_script_minutes(plan)):
            async with session() as s:
                result = await s.execute(update(Plan).where(Plan.id == row.id, Plan.status == "draft")
                                         .values(status="active"))
                await s.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=409, detail={"code": 409, "message": "plan already confirmed"})
            try:
                job_ids = await pipeline.enqueue_plan_all(plan, user_id=user.id)
            except Exception:
                # 入队失败退回 draft 并撤掉已入队的集任务,用户可再次确认,不留下半开写的计划
                ep_ext_ids = [e.episode_id for e in plan.episodes]
                async with session() as s:
                    await s.execute(update(Job).where(Job.ref_id.in_(ep_ext_ids), Job.status == "pending")
                                    .values(status="cancelled", error="confirm rolled back"))
                    await s.execute(update(Plan).where(Plan.id == row.id).values(status="draft"))
                    await s.commit()
                raise
    return {
        "plan_id": plan_id,
        "job_id": job_ids[0] if job_ids else None,
        "status": "active",
        "job_ids": job_ids,
    }

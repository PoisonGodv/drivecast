"""计划/集/生成任务 API(docs/02 §4 + docs/04 §3 SSE)。"""
from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from ..auth import get_current_user
from ..db import session
from ..jobs import pipeline
from ..library import artifacts
from ..library import service as library_service
from ..config import settings
from ..models import Episode, Job, Plan, Script, Topic, User
from ..persist import (
    finalize_plan_deletion,
    set_episode_status,
)
from ..planner import service as planner
from ..scriptwriter import service as scriptwriter
from .api_directions import ensure_direction_available

router = APIRouter(prefix="/api", tags=["plans"])


# ---------- 请求/响应契约 ----------

class TopicCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    direction: Literal["backend_arch", "ai_llm", "tech_news", "source_dive"]


class TopicOut(BaseModel):
    id: int
    title: str
    direction: str
    depth: str
    created_at: str


class PlanCreate(BaseModel):
    topic_id: int = Field(gt=0)
    episode_count: int | None = Field(default=None, ge=4, le=12)
    target_minutes: int | None = Field(default=None, ge=8, le=45)  # 支持到 40 分钟长集(留 5 分钟余量)


class PlanGenerateOut(BaseModel):
    plan_id: int
    job_id: int | None
    status: str
    job_ids: list[int]


class PlanDeleteOut(BaseModel):
    plan_id: int
    status: str
    cancelled_jobs: int
    deleted_episodes: int


class EpisodeGenerateOut(BaseModel):
    episode_id: int
    job_id: int
    status: str


class RegenerateIn(BaseModel):
    """勘误重生成入参(docs/07 §5);reason 可空,body 整体可省。"""

    reason: str = Field(default="", max_length=500)


class QcIn(BaseModel):
    """人审标记入参(docs/07 §4)。"""

    passed: bool
    notes: str = Field(default="", max_length=500)


class JobOut(BaseModel):
    id: int
    kind: str
    ref_id: str
    status: str
    attempt: int
    max_attempt: int
    error: str
    payload: dict
    created_at: str
    updated_at: str


class ScriptOut(BaseModel):
    episode_id: int
    content_md: str
    knowledge: list[str]
    references: list[dict] = []      # 集级参考链接(题库题 refs 汇总);旧脚本为 []
    review: dict | None = None   # 事实审稿结果,文件态透传(docs/07 §3.1);旧脚本无此字段
    errata: list[dict] = []      # 勘误历史(docs/07 §5)


# ---------- topics ----------

@router.post("/topics", response_model=TopicOut, status_code=201)
async def create_topic(body: TopicCreate, user: User = Depends(get_current_user)) -> dict:
    """创建主题(docs/02 §4);归属当前用户。"""
    ensure_direction_available(body.direction)
    async with session() as s:
        t = Topic(title=body.title, direction=body.direction, user_id=user.id)
        s.add(t)
        await s.commit()
        await s.refresh(t)
        return {
            "id": t.id, "title": t.title, "direction": t.direction,
            "depth": t.depth, "created_at": t.created_at,
        }


# ---------- plans ----------

@router.post("/plans", response_model=PlanGenerateOut, status_code=201)
async def create_plan(body: PlanCreate, user: User = Depends(get_current_user)) -> dict:
    """由主题生成学习计划(docs/02 §4):落 generating 占位 + 入 plan job,立即返回。

    大模型出大纲真实耗时可达数分钟,同步等待会被浏览器/网关超时击杀;
    大纲由 worker 异步生成,完成后占位行转正 active 并链式入队各集 script job,
    进度由 SSE 推送(docs/04 §3)。topic/占位 plan/job 全部写入当前用户归属。
    """
    async with session() as s:
        topic = await s.scalar(
            select(Topic).where(Topic.id == body.topic_id, Topic.user_id == user.id)
        )
        if topic is None:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "topic not found"})
        ensure_direction_available(topic.direction)

        # 服务端幂等兜底:同主题已有生成中的计划直接复用(配合前端防抖)
        existing = await s.scalar(
            select(Plan).where(
                Plan.topic_id == topic.id,
                Plan.status == "generating",
                Plan.user_id == user.id,
            )
        )

    if existing is not None:
        job_id = await pipeline.enqueue("plan", existing.ext_id, {
            "plan_id": existing.ext_id,
            "topic": topic.title,
            "direction": topic.direction,
            "episode_count": body.episode_count,
            "target_minutes": body.target_minutes,
        }, user_id=user.id)
        return {"plan_id": existing.id, "job_id": job_id, "status": "pending", "job_ids": [job_id]}

    plan_ext_id = await planner.new_plan_id()
    async with session() as s:
        placeholder = Plan(
            ext_id=plan_ext_id, topic_id=topic.id, title=topic.title,
            status="generating", episode_count=0, user_id=user.id,
        )
        s.add(placeholder)
        try:
            await s.commit()
        except IntegrityError:
            # 并发创建同日序号撞号(ext_id 唯一约束),前端重试即可
            raise HTTPException(
                status_code=409, detail={"code": 409, "message": "计划编号冲突,请重试"}
            )
        plan_row_id = placeholder.id

    try:
        job_id = await pipeline.enqueue("plan", plan_ext_id, {
            "plan_id": plan_ext_id,
            "topic": topic.title,
            "direction": topic.direction,
            "episode_count": body.episode_count,
            "target_minutes": body.target_minutes,
        }, user_id=user.id)
    except Exception:
        # 入队失败不能留下无任务的 generating 占位(永久卡住),回滚占位行让前端可重试
        async with session() as s:
            await s.execute(delete(Plan).where(Plan.id == plan_row_id, Plan.status == "generating"))
            await s.commit()
        raise

    return {"plan_id": plan_row_id, "job_id": job_id, "status": "pending", "job_ids": [job_id]}


@router.post("/plans/{plan_id}/retry", response_model=PlanGenerateOut)
async def retry_plan(plan_id: int, user: User = Depends(get_current_user)) -> dict:
    """大纲生成失败后原参数重试,无需删除重建。"""
    plan_row = await library_service.get_plan_by_id(plan_id, user_id=user.id)
    if plan_row is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "plan not found"})
    async with pipeline.get_plan_lifecycle_lock(plan_row.ext_id):
        async with session() as s:
            fresh = await s.scalar(select(Plan).where(Plan.id == plan_id, Plan.user_id == user.id))
            if fresh is None or fresh.status != "failed":
                raise HTTPException(status_code=409, detail={
                    "code": 409, "message": f"plan is {fresh.status if fresh else 'missing'}, retry not allowed"})
            last = await s.scalar(select(Job).where(Job.kind == "plan", Job.ref_id == fresh.ext_id)
                                  .order_by(Job.id.desc()).limit(1))
            if last is None:
                raise HTTPException(status_code=409, detail={"code": 409, "message": "找不到原始生成参数,请删除后重建"})
            payload = json.loads(last.payload)
            fresh.status = "generating"
            await s.commit()
        job_id = await pipeline.enqueue("plan", plan_row.ext_id, payload, user_id=user.id)
    return {"plan_id": plan_id, "job_id": job_id, "status": "pending", "job_ids": [job_id]}

@router.get("/plans")
async def list_plans(user: User = Depends(get_current_user)) -> list[dict]:
    """计划列表(docs/02 §4);只含当前用户的计划。"""
    return await library_service.list_plans(user_id=user.id)


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: int, user: User = Depends(get_current_user)) -> dict:
    """计划详情(docs/02 §4 示例);跨用户与不存在同 404,不泄露存在性。"""
    detail = await library_service.get_plan(plan_id, user_id=user.id)
    if detail is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "plan not found"})
    return detail


@router.delete("/plans/{plan_id}", response_model=PlanDeleteOut)
async def delete_plan(plan_id: int, user: User = Depends(get_current_user)) -> dict:
    """永久删除计划:设置栅栏、终止正在执行的任务、清理全部文件与数据库图。"""
    from ..jobs import worker
    plan_row = await library_service.get_plan_by_id(plan_id, user_id=user.id)
    if plan_row is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "plan not found"})

    lock = pipeline.get_plan_lifecycle_lock(plan_row.ext_id)
    async with lock:
        fence = await pipeline.fence_plan_for_deletion(plan_id)
        if fence is None:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "plan not found"})

        # 立即停止并等待属于本计划的当前协程/子进程
        await worker.cancel_and_await_plan_tasks(fence.plan_ext_id)

        # 幂等清理所有文件产物与临时文件
        artifacts.delete_plan_artifacts(fence.plan_ext_id, fence.episode_ext_ids)

        # 单事务级联删除所有关联数据库记录
        await finalize_plan_deletion(
            fence.plan_id,
            fence.plan_ext_id,
            fence.topic_id,
            fence.episode_ids,
            fence.episode_ext_ids,
        )

        return {
            "plan_id": plan_id,
            "status": "deleted",
            "cancelled_jobs": len(fence.cancelled_job_ids),
            "deleted_episodes": len(fence.episode_ids),
        }


async def _assert_plan_active(plan_row) -> None:
    """只有 active 计划允许创建/入队生成任务。"""
    if plan_row.status != "active":
        raise HTTPException(
            status_code=409,
            detail={"code": 409, "message": f"plan is {plan_row.status}, generation not allowed"},
        )


# ---------- episodes / jobs ----------

@router.post("/episodes/{episode_id}/generate", response_model=EpisodeGenerateOut)
async def generate_episode(episode_id: int, user: User = Depends(get_current_user)) -> dict:
    """触发单集 script → tts 链(docs/02 §4)。"""
    ep = await library_service.get_episode(episode_id, user_id=user.id)
    if ep is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "episode not found"})
    plan = await library_service.get_plan_by_id(ep.plan_id, user_id=user.id)
    if plan is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "plan not found"})
    async with pipeline.get_plan_lifecycle_lock(plan.ext_id):
        await _assert_plan_active(plan)
        job_id = await pipeline.enqueue_episode(plan.ext_id, ep.seq, ep.ext_id, user_id=user.id)
    return {"episode_id": episode_id, "job_id": job_id, "status": "pending"}


@router.post("/plans/{plan_id}/generate_all", response_model=PlanGenerateOut)
async def generate_plan_all(plan_id: int, user: User = Depends(get_current_user)) -> dict:
    """整计划批量生成(docs/02 §4)。"""
    plan_row = await library_service.get_plan_by_id(plan_id, user_id=user.id)
    if plan_row is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "plan not found"})
    async with pipeline.get_plan_lifecycle_lock(plan_row.ext_id):
        await _assert_plan_active(plan_row)
        plan_rec = planner.load_plan(plan_row.ext_id)
        job_ids = await pipeline.enqueue_plan_all(plan_rec, user_id=user.id)
    status = "pending" if job_ids else "done"
    return {
        "plan_id": plan_id,
        "job_id": job_ids[0] if job_ids else None,
        "status": status,
        "job_ids": job_ids,
    }


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: int, user: User = Depends(get_current_user)) -> dict:
    """任务状态查询(SSE 断线兜底;docs/02 §4);跨用户 job 与不存在同 404。"""
    async with session() as s:
        job = await s.get(Job, job_id)
        if job is None or job.user_id != user.id:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "job not found"})
        return {
            "id": job.id, "kind": job.kind, "ref_id": job.ref_id,
            "status": job.status, "attempt": job.attempt,
            "max_attempt": job.max_attempt, "error": job.error,
            "payload": json.loads(job.payload), "created_at": job.created_at,
            "updated_at": job.updated_at,
        }


@router.post("/episodes/{episode_id}/regenerate", response_model=EpisodeGenerateOut)
async def regenerate_episode(
    episode_id: int, body: RegenerateIn | None = None, user: User = Depends(get_current_user)
) -> dict:
    """勘误重生成(docs/07 §5):备份旧产物 → 状态重置 → 复用 script job 队列。"""
    ep = await library_service.get_episode(episode_id, user_id=user.id)
    if ep is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "episode not found"})
    plan = await library_service.get_plan_by_id(ep.plan_id, user_id=user.id)
    if plan is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "plan not found"})
    await _assert_plan_active(plan)

    async with pipeline.get_plan_lifecycle_lock(plan.ext_id):
        fresh = await library_service.get_plan_by_id(ep.plan_id, user_id=user.id)
        if fresh is None:
            raise HTTPException(status_code=404, detail="plan not found")
        await _assert_plan_active(fresh)
        async with session() as s:
            refs = (await s.scalars(select(Episode.ext_id).where(Episode.plan_id == ep.plan_id))).all()
            busy = await s.scalar(select(Job.id).where(
                Job.ref_id.in_([plan.ext_id, *refs]), Job.status.in_(("pending", "running"))).limit(1))
        if busy is not None:
            raise HTTPException(status_code=409, detail="计划仍有生成任务，请完成后再重新生成")
        reason = (body.reason if body else "").strip()
        if reason:
            artifacts.append_errata(ep.ext_id, reason)
        artifacts.backup_artifacts(ep.ext_id)
        await set_episode_status(ep.ext_id, "pending")
        job_id = await pipeline.enqueue_episode(plan.ext_id, ep.seq, ep.ext_id, user_id=user.id)
    return {"episode_id": episode_id, "job_id": job_id, "status": "pending"}


@router.post("/episodes/{episode_id}/audio/recheck", response_model=EpisodeGenerateOut)
async def recheck_episode_audio(episode_id: int, user: User = Depends(get_current_user)) -> dict:
    """已有音频只重检，不重新生成脚本或语音。"""
    ep = await library_service.get_episode(episode_id, user_id=user.id)
    if ep is None:
        raise HTTPException(status_code=404, detail="episode not found")
    plan = await library_service.get_plan_by_id(ep.plan_id, user_id=user.id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    async with pipeline.get_plan_lifecycle_lock(plan.ext_id):
        fresh = await library_service.get_plan_by_id(ep.plan_id, user_id=user.id)
        if fresh is None:
            raise HTTPException(status_code=404, detail="plan not found")
        await _assert_plan_active(fresh)
        if not settings.episode_audio_path(ep.ext_id).is_file():
            raise HTTPException(status_code=409, detail="无成品音频可重检")
        job_id = await pipeline.enqueue("quality_recheck", ep.ext_id,
                                       {"plan_id": plan.ext_id, "seq": ep.seq}, user_id=user.id)
    return {"episode_id": episode_id, "job_id": job_id, "status": "pending"}


@router.post("/episodes/{episode_id}/qc")
async def qc_episode(episode_id: int, body: QcIn, user: User = Depends(get_current_user)) -> dict:
    """人审标记(docs/07 §4):写 meta 的 human_qc;本期只做 API,不做 UI。"""
    ep = await library_service.get_episode(episode_id, user_id=user.id)
    if ep is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "episode not found"})
    return artifacts.write_human_qc(ep.ext_id, body.passed, body.notes)


@router.get("/episodes/{episode_id}/script", response_model=ScriptOut)
async def get_script(episode_id: int, user: User = Depends(get_current_user)) -> dict:
    """取脚本文本(页面跟读;docs/02 §4),附带审稿结果与勘误历史。"""
    ep = await library_service.get_episode(episode_id, user_id=user.id)
    if ep is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "episode not found"})
    async with session() as s:
        script = await s.scalar(select(Script).where(Script.episode_id == episode_id))
        if script is None:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "script not found"})

    # review/errata 走文件态(DB 不存审稿结果,docs/07 §3.1)
    file_script = scriptwriter.load_script(ep.ext_id)
    try:
        references = json.loads(script.refs) if script.refs else []
        if not isinstance(references, list):
            references = []
    except (TypeError, json.JSONDecodeError):
        references = []
    return {
        "episode_id": episode_id,
        "content_md": script.content_md,
        "knowledge": json.loads(script.knowledge) if script.knowledge else [],
        "references": references,
        "review": file_script.get("review") if file_script else None,
        "errata": artifacts.load_errata(ep.ext_id) if ep else [],
    }


# ---------- SSE ----------

# 进程收到退出信号时置位:SSE 长轮询立即收尾,不必等到优雅关停超时被强杀
server_stopping = asyncio.Event()

_STAGE_PERCENT = {
    "pending": ("outline", "pending", 0),
    "scripted": ("script", "done", 40),
    "tts_ready": ("tts", "generating", 55),
    "synth_partial": ("tts", "generating", 75),
    "ready": ("done", "ready", 100),
    "failed": ("done", "failed", 0),
    "cancelled": ("done", "cancelled", 0),
}


def _episode_event(ep: dict) -> dict:
    stage, stage_status, percent = _STAGE_PERCENT.get(ep["status"], ("outline", "pending", 0))
    event = {
        "episode_id": ep["id"],
        "episode_ext_id": ep.get("ext_id") or f"ep_{ep['id']}",
        "seq": ep["seq"],
        "stage": stage,
        "stage_status": stage_status,
        # 始终保留规范的 DB 状态,防止前端被展示态(done/generating)覆盖
        "status": ep["status"],
        "percent": percent,
        # 生成中细粒度展示:queued/writing/synthesizing/postprocessing;elapsed 为本阶段已用秒数
        "gen_stage": ep.get("stage"),
        "elapsed_sec": ep.get("elapsed_sec"),
        "progress": ep.get("progress"),
    }
    if ep["status"] == "ready":
        event["audio_url"] = f"/api/episodes/{ep['id']}/audio"
        event["duration_sec"] = ep.get("duration_sec", 0)
    return event


async def _plan_events(plan_id: int, user_id: int):
    """SSE 生成进度推送(docs/04 §3):轮询 DB 状态变化 → 推事件,EventSource 断线重连由前端负责。

    轮询每次都带 user_id 重新校验归属:跨用户/不存在统一推 not_found,不泄露存在性。
    """
    poll = 2.0  # 简单可靠,不额外依赖
    last_statuses: dict[int, tuple] = {}
    last_plan_key: tuple[str, int, int, bool] | None = None
    try:
        yield f"retry: 3000\n\n"
        while not server_stopping.is_set():
            detail = await library_service.get_plan(plan_id, user_id=user_id)
            if detail is None:
                yield f"event: plan_status\ndata: {json.dumps({'plan_id': plan_id, 'ext_id': '', 'status': 'not_found', 'ready': 0, 'total': 0, 'all_done': False, 'error': 'not_found'})}\n\n"
                break

            # episode 状态/阶段/细进度变化时推 episode_status(已用时长由前端本地累加,不逐秒推)
            for ep in detail["episodes"]:
                sig = (ep["status"], ep.get("stage"), json.dumps(ep.get("progress"), sort_keys=True))
                if last_statuses.get(ep["id"]) != sig:
                    event = _episode_event(ep)
                    yield f"event: episode_status\ndata: {json.dumps(event)}\n\n"
                    last_statuses[ep["id"]] = sig

            # plan 总进度或状态变化时推 plan_status
            plan_status = detail.get("status", "active")
            ready = detail["ready_count"]
            total = detail["episode_count"]
            all_done = total > 0 and ready == total
            key = (plan_status, ready, total, all_done)
            if key != last_plan_key:
                yield (
                    f"event: plan_status\n"
                    f"data: {json.dumps({'plan_id': plan_id, 'ext_id': detail['ext_id'], 'status': plan_status, 'ready': ready, 'total': total, 'all_done': all_done})}\n\n"
                )
                last_plan_key = key

            try:
                await asyncio.wait_for(server_stopping.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        # 客户端断开,优雅结束 generator
        raise


@router.get("/plans/{plan_id}/events")
async def plan_events(plan_id: int, user: User = Depends(get_current_user)) -> StreamingResponse:
    """SSE 进度主通道(docs/04 §3);EventSource 无法自定义头,鉴权走 ?token= 白名单。"""
    return StreamingResponse(
        _plan_events(plan_id, user.id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

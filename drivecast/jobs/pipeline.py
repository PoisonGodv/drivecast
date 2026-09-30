"""job 编排(docs/02 §2):内容生产全部走 job,web/CLI 只入队不直接生成。

链式:script job 完成 → 自动入队 tts job。整计划 = 每集一个 script job,
worker 文本/音频分通道，TTS 请求严格单路。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update

from ..db import session
from ..models import Episode, Job, Plan

logger = logging.getLogger(__name__)

_plan_locks: dict[str, asyncio.Lock] = {}


def get_plan_lifecycle_lock(plan_ext_id: str) -> asyncio.Lock:
    """按 plan_ext_id 互斥,封堵 worker 认领注册与删除/生成之间的竞态窗口。"""
    if plan_ext_id not in _plan_locks:
        _plan_locks[plan_ext_id] = asyncio.Lock()
    return _plan_locks[plan_ext_id]


@dataclass
class PlanDeletionFence:
    plan_id: int
    plan_ext_id: str
    topic_id: int
    episode_ids: list[int]
    episode_ext_ids: list[str]
    cancelled_job_ids: list[int]
    running_job_ids: list[int]


async def enqueue(kind: str, ep_ext_id: str, payload: dict, user_id: int | None = None) -> int:
    """入队(幂等:同 kind+ref_id 的未完成 job 已存在则复用;计划非存活拒绝)。

    存活 = active / generating(大纲异步生成中);cancelled/删除等终态一律拒绝。
    user_id 为 job 归属(末位可选参数,旧调用不传 → ORM default=1=admin);
    worker 链式入队应传父 job.user_id。
    """
    async with session() as s:
        plan_ext_id = str(payload.get("plan_id") or "")
        if plan_ext_id:
            plan_status = await s.scalar(select(Plan.status).where(Plan.ext_id == plan_ext_id))
            if plan_status not in ("active", "generating"):
                raise RuntimeError(f"plan {plan_ext_id} status={plan_status}, enqueue rejected")

        existing = await s.scalar(
            select(Job.id).where(
                Job.kind == kind, Job.ref_id == ep_ext_id,
                Job.status.in_(("pending", "running")),
            )
        )
        if existing is not None:
            return existing
        job = Job(
            kind=kind, ref_id=ep_ext_id, payload=json.dumps(payload, ensure_ascii=False),
            user_id=user_id if user_id is not None else 1,
        )
        s.add(job)
        await s.commit()
        return job.id


async def fence_plan_for_deletion(plan_id: int) -> PlanDeletionFence | None:
    """设置删除栅栏(单事务):plan/jobs/episodes 全部标 cancelled,提取关联 ID。"""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    async with session() as s:
        plan = await s.get(Plan, plan_id)
        if plan is None:
            return None

        episodes = (
            await s.execute(
                select(Episode.id, Episode.ext_id).where(Episode.plan_id == plan_id).order_by(Episode.seq)
            )
        ).all()
        ep_ids = [row[0] for row in episodes]
        ep_ext_ids = [row[1] for row in episodes]

        # 集任务 + 以计划 ext_id 为 ref_id 的大纲 plan 任务(generating 占位计划尚无集)
        job_rows = (
            await s.execute(
                select(Job.id, Job.status).where(
                    Job.ref_id.in_([*ep_ext_ids, plan.ext_id]),
                    Job.status.in_(("pending", "running")),
                )
            )
        ).all()

        cancelled_job_ids = [row[0] for row in job_rows]
        running_job_ids = [row[0] for row in job_rows if row[1] == "running"]

        # 必须在把 running 标 cancelled 之前结转:删计划不能退回已消耗的额度
        from .. import quota
        await quota.carry_over_deleted(s, ep_ext_ids)

        await s.execute(update(Plan).where(Plan.id == plan_id).values(status="cancelled"))

        await s.execute(
            update(Job)
            .where(Job.ref_id.in_([*ep_ext_ids, plan.ext_id]), Job.status.in_(("pending", "running")))
            .values(status="cancelled", error="plan deleted", updated_at=now)
        )
        if ep_ext_ids:
            await s.execute(
                update(Episode)
                .where(Episode.plan_id == plan_id)
                .values(status="cancelled")
            )

        await s.commit()
        return PlanDeletionFence(
            plan_id=plan.id,
            plan_ext_id=plan.ext_id,
            topic_id=plan.topic_id,
            episode_ids=ep_ids,
            episode_ext_ids=ep_ext_ids,
            cancelled_job_ids=cancelled_job_ids,
            running_job_ids=running_job_ids,
        )


async def cancel_plan_jobs(plan_ext_id: str) -> int:
    """兼容只取消 pending job 的旧辅助,实际删除统一走 fence_plan_for_deletion。"""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    async with session() as s:
        plan_id = await s.scalar(select(Plan.id).where(Plan.ext_id == plan_ext_id))
        if plan_id is None:
            return 0
        ep_ext_ids = (
            (await s.execute(select(Episode.ext_id).where(Episode.plan_id == plan_id)))
            .scalars().all()
        )
        if not ep_ext_ids:
            return 0
        result = await s.execute(
            update(Job)
            .where(Job.ref_id.in_(ep_ext_ids), Job.status == "pending")
            .values(status="cancelled", error="plan cancelled", updated_at=now)
        )
        await s.commit()
        return result.rowcount


async def enqueue_episode(
    plan_id: str, seq: int, ep_ext_id: str, user_id: int | None = None
) -> int:
    from ..config import settings
    payload = {"plan_id": plan_id, "seq": seq}
    if settings.script_path(ep_ext_id).exists():
        payload["reuse_script"] = True  # 写稿会直接复用,额度不再计这一集
    return await enqueue("script", ep_ext_id, payload, user_id=user_id)


async def enqueue_plan_all(plan, user_id: int | None = None) -> list[int]:
    """整计划批量；旧成品缺当前音频元数据时只排本地后处理，不调 LLM/TTS。"""
    from ..config import settings
    from ..tts.service import AUDIO_PIPELINE_VERSION
    from ..tts.quality import stale
    job_ids = []
    for ep in plan.episodes:
        final = settings.episode_audio_path(ep.episode_id)
        if final.exists() and final.stat().st_size > 0:
            meta_path = settings.meta_path(ep.episode_id)
            pipeline_version = 0
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text("utf-8"))
                    pipeline_version = int(meta.get("pipeline_version", 0))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    pipeline_version = 0
            if pipeline_version < AUDIO_PIPELINE_VERSION:
                if not settings.chunks_dir(ep.episode_id).is_dir():
                    # chunk 已回收,本地升级无从做起;成品仍可正常播放,需要新管线请走勘误重生成
                    logger.info(f"{ep.episode_id} chunk 已回收,跳过本地音频升级")
                    continue
                job_ids.append(await enqueue(
                    "audio_upgrade", ep.episode_id,
                    {"plan_id": plan.plan_id, "seq": ep.seq}, user_id=user_id,
                ))
            elif stale(meta):
                job_ids.append(await enqueue(
                    "quality_recheck", ep.episode_id,
                    {"plan_id": plan.plan_id, "seq": ep.seq}, user_id=user_id))
            continue
        job_ids.append(await enqueue_episode(plan.plan_id, ep.seq, ep.episode_id, user_id=user_id))
    return job_ids

"""job 编排(docs/02 §2):内容生产全部走 job,web/CLI 只入队不直接生成。

链式:script job 完成 → 自动入队 tts job。整计划 = 每集一个 script job,
worker 串行消费天然防 edge-tts 限流(docs/03 §3.3)。
"""
from __future__ import annotations

import json

from sqlalchemy import select

from ..db import session
from ..models import Job


async def enqueue(kind: str, ep_ext_id: str, payload: dict) -> int:
    """入队(幂等:同 kind+ref_id 的未完成 job 已存在则复用)。"""
    async with session() as s:
        existing = await s.scalar(
            select(Job.id).where(
                Job.kind == kind, Job.ref_id == ep_ext_id,
                Job.status.in_(("pending", "running")),
            )
        )
        if existing is not None:
            return existing
        job = Job(kind=kind, ref_id=ep_ext_id, payload=json.dumps(payload, ensure_ascii=False))
        s.add(job)
        await s.commit()
        return job.id


async def enqueue_episode(plan_id: str, seq: int, ep_ext_id: str) -> int:
    return await enqueue("script", ep_ext_id, {"plan_id": plan_id, "seq": seq})


async def enqueue_plan_all(plan) -> list[int]:
    """整计划批量；旧成品缺当前音频元数据时只排本地后处理，不调 LLM/TTS。"""
    from ..config import settings
    from ..tts.service import AUDIO_PIPELINE_VERSION
    job_ids = []
    for ep in plan.episodes:
        final = settings.episode_audio_path(ep.episode_id)
        if final.exists() and final.stat().st_size > 0:
            meta_path = settings.meta_path(ep.episode_id)
            pipeline_version = 0
            if meta_path.exists():
                try:
                    pipeline_version = int(json.loads(meta_path.read_text("utf-8")).get("pipeline_version", 0))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    pipeline_version = 0
            if pipeline_version < AUDIO_PIPELINE_VERSION:
                job_ids.append(await enqueue(
                    "audio_upgrade", ep.episode_id, {"plan_id": plan.plan_id, "seq": ep.seq}
                ))
            continue
        job_ids.append(await enqueue_episode(plan.plan_id, ep.seq, ep.episode_id))
    return job_ids

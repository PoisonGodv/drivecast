"""asyncio worker(docs/02 §2/§6):串行消费 job 表,状态机推进,重启续跑。

状态机:pending → running → done/failed;attempt < max_attempt 失败自动回 pending。
启动时把 running 重置为 pending(上次进程死在了半途)。
M3 新增 run_forever():Web 常驻,跑完队列不退出,继续 poll(docs/05 M3)。
"""
from __future__ import annotations

import asyncio
import json
import traceback
from datetime import datetime

from sqlalchemy import select, update

from ..config import settings
from ..db import close_db, session
from ..models import Job
from ..persist import set_episode_status
from ..planner import service as planner
from ..scriptwriter import service as scriptwriter
from ..tts import service as tts

_POLL_INTERVAL = 1.0


async def _rescan_orphans() -> int:
    """启动恢复:running → pending(docs/02 §6 断点续跑核心)。"""
    async with session() as s:
        result = await s.execute(
            update(Job).where(Job.status == "running")
            .values(status="pending", updated_at=datetime.now().isoformat(sep=" ", timespec="seconds"))
        )
        await s.commit()
        return result.rowcount


async def _next_pending() -> Job | None:
    async with session() as s:
        row = await s.scalar(
            select(Job).where(Job.status == "pending").order_by(Job.id).limit(1)
        )
        if row is None:
            return None
        await s.execute(
            update(Job).where(Job.id == row.id)
            .values(status="running", attempt=Job.attempt + 1,
                    updated_at=datetime.now().isoformat(sep=" ", timespec="seconds"))
        )
        await s.commit()
        return row


async def _finish(job_id: int, status: str, error: str = "") -> None:
    async with session() as s:
        await s.execute(
            update(Job).where(Job.id == job_id)
            .values(status=status, error=error,
                    updated_at=datetime.now().isoformat(sep=" ", timespec="seconds"))
        )
        await s.commit()


async def _retry_or_fail(job: Job, exc: BaseException) -> None:
    err = f"{type(exc).__name__}: {exc}"[-500:]
    if job.attempt < job.max_attempt:
        await _finish(job.id, "pending", err)  # 回 pending 重试
        print(f"[worker] job#{job.id} {job.kind}/{job.ref_id} 第{job.attempt}次失败,回队列重试: {err[:120]}")
    else:
        await _finish(job.id, "failed", err)
        await set_episode_status(job.ref_id, "failed")
        print(f"[worker] job#{job.id} {job.kind}/{job.ref_id} 失败(已达{job.max_attempt}次): {err[:120]}")
        traceback.print_exception(exc)


async def _run_script_job(job: Job) -> None:
    payload = json.loads(job.payload)
    plan = planner.load_plan(payload["plan_id"])
    ep = next(e for e in plan.episodes if e.seq == payload["seq"])
    await scriptwriter.write_script(plan, ep)
    await _finish(job.id, "done")
    from .pipeline import enqueue
    await enqueue("tts", ep.episode_id, {"plan_id": plan.plan_id, "seq": ep.seq})  # 链式入队 tts


async def _run_tts_job(job: Job) -> None:
    payload = json.loads(job.payload)
    plan = planner.load_plan(payload["plan_id"])
    script = scriptwriter.load_script(job.ref_id)
    if not script:
        raise RuntimeError(f"{job.ref_id} 无脚本,tts job 无法执行")
    await tts.synthesize_episode(script, album=plan.title)
    await _finish(job.id, "done")


async def _run_audio_upgrade_job(job: Job) -> None:
    """仅复用既有脚本/chunk 做 M6 后处理；缺产物就明确失败，绝不补调 LLM/TTS。"""
    payload = json.loads(job.payload)
    plan = planner.load_plan(payload["plan_id"])
    script = scriptwriter.load_script(job.ref_id)
    if not script:
        raise RuntimeError(f"{job.ref_id} 无既有脚本，无法本地升级音频")
    tts_path = settings.tts_path(job.ref_id)
    if not tts_path.exists():
        raise RuntimeError(f"{job.ref_id} 无既有 tts.json，无法本地升级音频")
    tts_payload = json.loads(tts_path.read_text("utf-8"))
    chunks = tts_payload.get("chunks") or []
    missing = [
        chunk.get("seq")
        for chunk in chunks
        if not (settings.chunks_dir(job.ref_id) / f"{int(chunk.get('seq') or 0):03d}.mp3").is_file()
    ]
    if not chunks or missing:
        raise RuntimeError(f"{job.ref_id} 既有 chunk 不完整:{missing or 'empty'}")
    await tts.synthesize_episode(script, album=plan.title)
    await _finish(job.id, "done")


_EXECUTORS = {
    "script": _run_script_job,
    "tts": _run_tts_job,
    "audio_upgrade": _run_audio_upgrade_job,
}


async def _execute_one(job: Job) -> None:
    """执行单个 job,含错误处理与重试。"""
    print(f"[worker] 执行 job#{job.id} {job.kind}/{job.ref_id} (第{job.attempt + 1}次)")
    try:
        await _EXECUTORS[job.kind](job)
        print(f"[worker] job#{job.id} 完成")
    except Exception as exc:  # noqa: BLE001
        await _retry_or_fail(job, exc)


async def run_until_empty() -> None:
    """串行消费直到队列清空(CLI/批处理模式)。"""
    orphans = await _rescan_orphans()
    if orphans:
        print(f"[worker] 恢复 {orphans} 个中断 job(running→pending)")
    while True:
        job = await _next_pending()
        if job is None:
            break
        await _execute_one(job)
        await asyncio.sleep(_POLL_INTERVAL)  # 串行+间隔,防限流(docs/03 §3.3)
    await close_db()
    print("[worker] 队列已清空")


async def run_forever() -> None:
    """常驻 worker:跑完队列不退出,继续 poll;Web lifespan 负责启动与取消(docs/05 M3)。"""
    orphans = await _rescan_orphans()
    if orphans:
        print(f"[worker] 恢复 {orphans} 个中断 job(running→pending)")
    try:
        while True:
            job = await _next_pending()
            if job is None:
                await asyncio.sleep(_POLL_INTERVAL)
                continue
            await _execute_one(job)
            await asyncio.sleep(_POLL_INTERVAL)
    except asyncio.CancelledError:
        print("[worker] 收到取消信号,优雅退出")
        raise

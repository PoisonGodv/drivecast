"""asyncio worker:有界文本通道与单路音频通道，状态机推进、重启续跑。

状态机:pending → running → done/failed/cancelled;attempt < max_attempt 失败自动回 pending。
启动时把 running 重置为 pending(上次进程死在了半途);常驻期间空轮询时
顺带复位超时 running(进程没死但 job 挂死,如网关连接清理滞后的残留)。
M3 新增 run_forever():Web 常驻,跑完队列不退出,继续 poll(docs/05 M3)。
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from datetime import datetime, timedelta

from sqlalchemy import select, update

from ..config import settings
from ..db import close_db, session
from ..models import Episode, Job, Plan
from ..persist import set_episode_status, set_plan_status_by_ext
from ..planner import service as planner
from ..scriptwriter import service as scriptwriter
from ..tts import service as tts

_STALE_CHECK_INTERVAL = 60.0  # 空轮询时最多每分钟做一次超时 running 复位
_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 60.0

# 进程内正在运行的 job task 映射:plan_ext_id -> {job_id: task}
_active_plan_tasks: dict[str, dict[int, asyncio.Task]] = {}
_claimed_jobs: set[int] = set()
_shutdown_jobs: set[int] = set()


def register_running_job(plan_ext_id: str, job_id: int, task: asyncio.Task) -> None:
    if plan_ext_id not in _active_plan_tasks:
        _active_plan_tasks[plan_ext_id] = {}
    _active_plan_tasks[plan_ext_id][job_id] = task


def unregister_running_job(plan_ext_id: str, job_id: int, task: asyncio.Task) -> None:
    bucket = _active_plan_tasks.get(plan_ext_id)
    if not bucket:
        return
    if bucket.get(job_id) is task:
        bucket.pop(job_id, None)
    if not bucket:
        _active_plan_tasks.pop(plan_ext_id, None)


async def cancel_and_await_plan_tasks(plan_ext_id: str, timeout: float = 15.0) -> int:
    """立即取消属于该计划的所有正在执行的 child task 并等待其停止。"""
    bucket = list(_active_plan_tasks.get(plan_ext_id, {}).values())
    if not bucket:
        return 0
    for task in bucket:
        if not task.done():
            task.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*bucket, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"timeout waiting for plan {plan_ext_id} running jobs to cancel") from exc
    return len(bucket)


def has_running_job_task(plan_ext_id: str, job_id: int) -> bool:
    task = _active_plan_tasks.get(plan_ext_id, {}).get(job_id)
    return bool(task and not task.done())


def _now_text() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


async def _alive_ref_ids(s) -> list[str]:
    """存活计划可复位的 job ref_id 全集:active 计划的集 ext_id + 存活计划的 plan ext_id。

    大纲生成 job(kind=plan)以计划 ext_id 为 ref_id,只数集 ext_id 会漏掉它,
    进程崩溃后该 running 将永久挂死(generating 占位计划还没有任何集)。
    """
    active_plan_ep_ext_ids = (
        await s.execute(
            select(Episode.ext_id)
            .join(Plan, Plan.id == Episode.plan_id)
            .where(Plan.status == "active")
        )
    ).scalars().all()
    alive_plan_ext_ids = (
        await s.execute(select(Plan.ext_id).where(Plan.status.in_(("active", "generating"))))
    ).scalars().all()
    return list(active_plan_ep_ext_ids) + list(alive_plan_ext_ids)


async def _rescan_orphans() -> int:
    """启动恢复:只复位仍属于存活计划的 running job 为 pending。"""
    async with session() as s:
        ref_ids = await _alive_ref_ids(s)
        if not ref_ids:
            return 0
        result = await s.execute(
            update(Job).where(Job.status == "running", Job.ref_id.in_(ref_ids))
            .values(status="pending", updated_at=_now_text())
        )
        await s.commit()
        return result.rowcount


async def _reset_stale_running() -> int:
    """复位挂死的 running(仅限存活计划)。"""
    threshold = (datetime.now() - timedelta(seconds=settings.worker_stale_running_sec)).isoformat(
        sep=" ", timespec="seconds"
    )
    async with session() as s:
        ref_ids = await _alive_ref_ids(s)
        if not ref_ids:
            return 0
        result = await s.execute(
            update(Job)
            .where(
                Job.status == "running",
                Job.updated_at < threshold,
                Job.id.not_in(_claimed_jobs | {j for bucket in _active_plan_tasks.values() for j, t in bucket.items() if not t.done()}),
                Job.ref_id.in_(ref_ids),
            )
            .values(status="pending", updated_at=_now_text())
        )
        await s.commit()
        return result.rowcount


async def _recover_stuck_generating() -> int:
    """generating 计划却没有在途 plan job(入队失败/任务丢失)→ 标 failed,前端可删除重建。"""
    async with session() as s:
        generating = (
            await s.execute(select(Plan.ext_id).where(Plan.status == "generating"))
        ).scalars().all()
        if not generating:
            return 0
        inflight = set(
            (
                await s.execute(
                    select(Job.ref_id).where(
                        Job.kind == "plan",
                        Job.ref_id.in_(generating),
                        Job.status.in_(("pending", "running")),
                    )
                )
            ).scalars().all()
        )
        stuck = [ext_id for ext_id in generating if ext_id not in inflight]
        if not stuck:
            return 0
        await s.execute(update(Plan).where(Plan.ext_id.in_(stuck)).values(status="failed"))
        await s.commit()
        return len(stuck)


async def _next_pending(kinds=None, busy_plans=()) -> Job | None:
    """有空闲执行名额才认领；同计划文本有序，不占着名额等待依赖。"""
    async with session() as s:
        query = select(Job).where(Job.status == "pending")
        if kinds is not None:
            query = query.where(Job.kind.in_(kinds))
        rows = list((await s.scalars(query.order_by(Job.id))).all())
        rows.sort(key=lambda j: (json.loads(j.payload).get("plan_id", ""),
                                 int(json.loads(j.payload).get("seq", 0)), j.id))
        # 跨计划仍按最早排队顺序公平选择。
        first_by_plan = {}
        for row in rows:
            plan_id = json.loads(row.payload).get("plan_id", "")
            if plan_id not in busy_plans:
                first_by_plan.setdefault(plan_id, row)
        for row in sorted(first_by_plan.values(), key=lambda j: j.id):
            if row.kind == "script":
                payload = json.loads(row.payload)
                previous = list((await s.scalars(select(Job).where(
                    Job.kind == "script", Job.id != row.id,
                    Job.status.in_(("pending", "running", "failed"))))).all())
                blockers = [j for j in previous
                            if json.loads(j.payload).get("plan_id") == payload.get("plan_id")
                            and int(json.loads(j.payload).get("seq", 0)) < int(payload.get("seq", 0))]
                if any(j.status in ("pending", "running") for j in blockers):
                    continue
                if any(not settings.script_path(j.ref_id).exists() for j in blockers):
                    await s.execute(update(Job).where(Job.id == row.id, Job.status == "pending")
                                    .values(status="failed", error="上集脚本未完成，无法生成回顾", updated_at=_now_text()))
                    await s.commit()
                    await set_episode_status(row.ref_id, "failed")
                    continue
            result = await s.execute(
                update(Job).where(Job.id == row.id, Job.status == "pending")
                .values(status="running", attempt=Job.attempt + 1, updated_at=_now_text()))
            await s.commit()
            if result.rowcount:
                return row
        return None


async def _finish(job_id: int, status: str, error: str = "") -> bool:
    """带 CAS 守卫更新终态:只能由 running 转移;返回是否成功。"""
    async with session() as s:
        result = await s.execute(
            update(Job).where(Job.id == job_id, Job.status == "running")
            .values(status=status, error=error, updated_at=_now_text())
        )
        await s.commit()
        return result.rowcount > 0


async def _plan_alive(plan_ext_id: str) -> bool:
    """计划存在且存活返回 True(active/generating);其余(cancelled/不存在)返回 False。"""
    if not plan_ext_id:
        return False
    async with session() as s:
        status = await s.scalar(select(Plan.status).where(Plan.ext_id == plan_ext_id))
    return status in ("active", "generating")


async def _plan_generating(plan_ext_id: str) -> bool:
    """计划仍处于大纲生成中(占位态)。"""
    async with session() as s:
        status = await s.scalar(select(Plan.status).where(Plan.ext_id == plan_ext_id))
    return status == "generating"


async def _retry_or_fail(job: Job, exc: BaseException) -> None:
    err = f"{type(exc).__name__}: {exc}"[-500:]
    if job.attempt < job.max_attempt:
        cas_ok = await _finish(job.id, "pending", err)
        if cas_ok:
            print(f"[worker] job#{job.id} {job.kind}/{job.ref_id} 第{job.attempt}次失败,回队列重试: {err[:120]}")
    else:
        cas_ok = await _finish(job.id, "failed", err)
        if cas_ok:
            if job.kind == "plan":
                # 大纲生成彻底失败:占位计划转 failed,前端可见可删除重建
                await set_plan_status_by_ext(job.ref_id, "failed")
            elif job.kind not in ("quality_recheck", "audio_upgrade"):
                await set_episode_status(job.ref_id, "failed")
            print(f"[worker] job#{job.id} {job.kind}/{job.ref_id} 失败(已达{job.max_attempt}次): {err[:120]}")
            traceback.print_exception(exc)


async def _run_plan_job(job: Job) -> None:
    """计划大纲生成(docs/02 §4 kind=plan):LLM 出大纲 → 占位行转正 active → 链式入队各集 script。

    执行前/落库后都校验占位态:生成期间计划被删除时直接丢弃产出,防止删后复活。
    """
    payload = json.loads(job.payload)
    plan_ext_id = payload["plan_id"]
    if not await _plan_generating(plan_ext_id):
        # 大纲已落库但链式入队中断：幂等补齐，不重新请求 LLM。
        if await _plan_alive(plan_ext_id):
            from .pipeline import enqueue_plan_all
            plan_rec = planner.load_plan(plan_ext_id)
            await enqueue_plan_all(plan_rec, user_id=job.user_id)
            await _finish(job.id, "done")
        else:
            await _finish(job.id, "cancelled", "plan not generating")
        return
    plan_rec = await planner.create_plan(
        payload["topic"], direction=payload["direction"],
        episode_count=payload.get("episode_count"), target_minutes=payload.get("target_minutes"),
        plan_id=plan_ext_id, user_id=job.user_id,
    )
    if not await _plan_alive(plan_ext_id):
        settings.plan_path(plan_ext_id).unlink(missing_ok=True)
        await _finish(job.id, "cancelled", "plan deleted during generation")
        return
    from .pipeline import enqueue_plan_all
    # 链式入队继承父 job 归属:各集 script job 的 user_id 与计划属主一致
    await enqueue_plan_all(plan_rec, user_id=job.user_id)
    await _finish(job.id, "done")


async def _run_script_job(job: Job) -> None:
    payload = json.loads(job.payload)
    plan = planner.load_plan(payload["plan_id"])
    ep = next(e for e in plan.episodes if e.seq == payload["seq"])
    await scriptwriter.write_script(plan, ep)
    from .pipeline import enqueue
    if not await _plan_alive(plan.plan_id):
        print(f"[worker] job#{job.id} 所属计划非存活,停止后续入队")
        await _finish(job.id, "cancelled", "plan not active")
        return
    await enqueue("tts", ep.episode_id, {"plan_id": plan.plan_id, "seq": ep.seq}, user_id=job.user_id)
    await _finish(job.id, "done")


async def _run_tts_job(job: Job) -> None:
    payload = json.loads(job.payload)
    plan = planner.load_plan(payload["plan_id"])
    script = scriptwriter.load_script(job.ref_id)
    if not script:
        raise RuntimeError(f"{job.ref_id} 无脚本,tts job 无法执行")
    await tts.synthesize_episode(script, album=plan.title)
    await _finish(job.id, "done")


async def _run_audio_upgrade_job(job: Job) -> None:
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
    await tts.synthesize_episode(script, album=plan.title, local_only=True)
    await _finish(job.id, "done")


async def _run_quality_recheck_job(job: Job) -> None:
    payload = json.loads(job.payload)
    script = scriptwriter.load_script(job.ref_id) or {
        "episode_id": job.ref_id, "plan_id": payload["plan_id"]}
    await tts.recheck_quality(script)
    await _finish(job.id, "done")


_EXECUTORS = {
    "plan": _run_plan_job,
    "script": _run_script_job,
    "tts": _run_tts_job,
    "audio_upgrade": _run_audio_upgrade_job,
    "quality_recheck": _run_quality_recheck_job,
}


async def _execute_job_task(job: Job, plan_ext_id: str) -> None:
    try:
        await _EXECUTORS[job.kind](job)
        print(f"[worker] job#{job.id} 完成")
    except asyncio.CancelledError:
        print(f"[worker] job#{job.id} 收到取消信号")
        if job.id in _shutdown_jobs and await _plan_alive(plan_ext_id):
            async with session() as s:
                await s.execute(update(Job).where(Job.id == job.id, Job.status == "running")
                                .values(status="pending", attempt=Job.attempt - 1, updated_at=_now_text()))
                await s.commit()
        else:
            await _finish(job.id, "cancelled", "job cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        await _retry_or_fail(job, exc)


async def _execute_one(job: Job) -> None:
    """执行单个 job:包装为独立 child task 并登记,支持被计划删除立即终止。"""
    from .pipeline import get_plan_lifecycle_lock
    print(f"[worker] 执行 job#{job.id} {job.kind}/{job.ref_id} (第{job.attempt}次)")
    payload = json.loads(job.payload)
    plan_ext_id = str(payload.get("plan_id") or "")
    lock = get_plan_lifecycle_lock(plan_ext_id)
    current_task = asyncio.current_task()
    assert current_task is not None

    async with lock:
        if not await _plan_alive(plan_ext_id):
            await _finish(job.id, "cancelled", "plan not active")
            print(f"[worker] job#{job.id} 所属计划非存活,标记 cancelled 跳过")
            return
        child_task = asyncio.create_task(_execute_job_task(job, plan_ext_id))
        register_running_job(plan_ext_id, job.id, child_task)

    started = time.monotonic()
    try:
        await child_task
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling() > 0:
            # worker 自身被取消(Web lifespan 关停 / CLI Ctrl-C):必须向上传播。
            # 吞掉它会导致 await run_forever() 永不返回,进程关停挂死只能强杀,
            # 在途 job 变孤儿、20+ 分钟 LLM 产出随强杀作废。
            raise
        # child task 被计划删除 API 取消属正常业务中断,不杀常驻 worker
        pass
    finally:
        unregister_running_job(plan_ext_id, job.id, child_task)
        print(f"[worker] job#{job.id} {job.kind} 耗时 {time.monotonic() - started:.1f}s")


async def _dispatch(*, forever: bool) -> None:
    await _rescan_orphans()
    await _recover_stuck_generating()
    active: dict[asyncio.Task, tuple[Job, str]] = {}
    last_stale_check = time.monotonic()
    backoff = _BACKOFF_INITIAL
    try:
        while True:
            try:
                for task in list(active):
                    if task.done():
                        job, _ = active.pop(task)
                        _claimed_jobs.discard(job.id)
                        if not task.cancelled():
                            task.result()
                for lane, kinds, capacity in (
                    ("text", ("plan", "script"), settings.worker_text_concurrency),
                    ("audio", ("tts", "audio_upgrade", "quality_recheck"), 1),
                ):
                    while sum(l == lane for _, l in active.values()) < capacity:
                        busy = {json.loads(j.payload).get("plan_id", "")
                                for j, l in active.values() if l == lane}
                        job = await _next_pending(kinds, busy)
                        if job is None:
                            break
                        _claimed_jobs.add(job.id)
                        task = asyncio.create_task(_execute_one(job))
                        active[task] = (job, lane)
                if time.monotonic() - last_stale_check >= _STALE_CHECK_INTERVAL:
                    await _reset_stale_running()
                    last_stale_check = time.monotonic()
                if not active and not forever:
                    async with session() as s:
                        pending = await s.scalar(select(Job.id).where(Job.status == "pending").limit(1))
                    if pending is None:
                        break
                backoff = _BACKOFF_INITIAL
                if active:
                    await asyncio.wait(active, timeout=settings.worker_poll_interval_sec,
                                       return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(settings.worker_poll_interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                traceback.print_exception(exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
    finally:
        jobs = [j for j, _ in active.values()]
        _shutdown_jobs.update(j.id for j in jobs)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        # 认领后尚未进入 child 的任务也需恢复；删除栅栏的 cancelled 不可覆盖。
        for job in jobs:
            if await _plan_alive(json.loads(job.payload).get("plan_id", "")):
                async with session() as s:
                    await s.execute(update(Job).where(Job.id == job.id, Job.status == "running")
                                    .values(status="pending", attempt=Job.attempt - 1, updated_at=_now_text()))
                    await s.commit()
            _claimed_jobs.discard(job.id)
            _shutdown_jobs.discard(job.id)


async def run_until_empty() -> None:
    try:
        await _dispatch(forever=False)
    finally:
        await close_db()


async def run_forever() -> None:
    await _dispatch(forever=True)

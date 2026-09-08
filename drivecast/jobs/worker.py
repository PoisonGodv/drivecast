"""asyncio worker(docs/02 §2/§6):串行消费 job 表,状态机推进,重启续跑。

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
from ..persist import set_episode_status
from ..planner import service as planner
from ..scriptwriter import service as scriptwriter
from ..tts import service as tts

_STALE_CHECK_INTERVAL = 60.0  # 空轮询时最多每分钟做一次超时 running 复位
_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 60.0

# 进程内正在运行的 job task 映射:plan_ext_id -> {job_id: task}
_active_plan_tasks: dict[str, dict[int, asyncio.Task]] = {}


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


async def _rescan_orphans() -> int:
    """启动恢复:只复位仍属于 active 计划的 running job 为 pending。"""
    async with session() as s:
        active_plan_ep_ext_ids = (
            await s.execute(
                select(Episode.ext_id)
                .join(Plan, Plan.id == Episode.plan_id)
                .where(Plan.status == "active")
            )
        ).scalars().all()
        if not active_plan_ep_ext_ids:
            return 0
        result = await s.execute(
            update(Job).where(Job.status == "running", Job.ref_id.in_(active_plan_ep_ext_ids))
            .values(status="pending", updated_at=_now_text())
        )
        await s.commit()
        return result.rowcount


async def _reset_stale_running() -> int:
    """复位挂死的 running(仅限 active 计划)。"""
    threshold = (datetime.now() - timedelta(seconds=settings.worker_stale_running_sec)).isoformat(
        sep=" ", timespec="seconds"
    )
    async with session() as s:
        active_plan_ep_ext_ids = (
            await s.execute(
                select(Episode.ext_id)
                .join(Plan, Plan.id == Episode.plan_id)
                .where(Plan.status == "active")
            )
        ).scalars().all()
        if not active_plan_ep_ext_ids:
            return 0
        result = await s.execute(
            update(Job)
            .where(
                Job.status == "running",
                Job.updated_at < threshold,
                Job.ref_id.in_(active_plan_ep_ext_ids),
            )
            .values(status="pending", updated_at=_now_text())
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
        result = await s.execute(
            update(Job).where(Job.id == row.id, Job.status == "pending")
            .values(status="running", attempt=Job.attempt + 1, updated_at=_now_text())
        )
        await s.commit()
        if result.rowcount == 0:
            return None
        return row


async def _finish(job_id: int, status: str, error: str = "") -> bool:
    """带 CAS 守卫更新终态:只能由 running 转移;返回是否成功。"""
    async with session() as s:
        result = await s.execute(
            update(Job).where(Job.id == job_id, Job.status == "running")
            .values(status=status, error=error, updated_at=_now_text())
        )
        await s.commit()
        return result.rowcount > 0


async def _plan_active(plan_ext_id: str) -> bool:
    """计划存在且为 active 返回 True;其余(cancelled/deleting/不存在)返回 False。"""
    if not plan_ext_id:
        return False
    async with session() as s:
        status = await s.scalar(select(Plan.status).where(Plan.ext_id == plan_ext_id))
    return status == "active"


async def _retry_or_fail(job: Job, exc: BaseException) -> None:
    err = f"{type(exc).__name__}: {exc}"[-500:]
    if job.attempt < job.max_attempt:
        cas_ok = await _finish(job.id, "pending", err)
        if cas_ok:
            print(f"[worker] job#{job.id} {job.kind}/{job.ref_id} 第{job.attempt}次失败,回队列重试: {err[:120]}")
    else:
        cas_ok = await _finish(job.id, "failed", err)
        if cas_ok:
            await set_episode_status(job.ref_id, "failed")
            print(f"[worker] job#{job.id} {job.kind}/{job.ref_id} 失败(已达{job.max_attempt}次): {err[:120]}")
            traceback.print_exception(exc)


async def _run_script_job(job: Job) -> None:
    payload = json.loads(job.payload)
    plan = planner.load_plan(payload["plan_id"])
    ep = next(e for e in plan.episodes if e.seq == payload["seq"])
    await scriptwriter.write_script(plan, ep)
    from .pipeline import enqueue
    if not await _plan_active(plan.plan_id):
        print(f"[worker] job#{job.id} 所属计划非 active,停止后续入队")
        await _finish(job.id, "cancelled", "plan not active")
        return
    await enqueue("tts", ep.episode_id, {"plan_id": plan.plan_id, "seq": ep.seq})
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
    await tts.synthesize_episode(script, album=plan.title)
    await _finish(job.id, "done")


_EXECUTORS = {
    "script": _run_script_job,
    "tts": _run_tts_job,
    "audio_upgrade": _run_audio_upgrade_job,
}


async def _execute_job_task(job: Job, plan_ext_id: str) -> None:
    try:
        await _EXECUTORS[job.kind](job)
        print(f"[worker] job#{job.id} 完成")
    except asyncio.CancelledError:
        print(f"[worker] job#{job.id} 收到取消信号")
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
        if not await _plan_active(plan_ext_id):
            await _finish(job.id, "cancelled", "plan not active")
            print(f"[worker] job#{job.id} 所属计划非 active,标记 cancelled 跳过")
            return
        child_task = asyncio.create_task(_execute_job_task(job, plan_ext_id))
        register_running_job(plan_ext_id, job.id, child_task)

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
        await asyncio.sleep(settings.worker_poll_interval_sec)  # 串行+间隔,防限流(docs/03 §3.3)
    await close_db()
    print("[worker] 队列已清空")


async def run_forever() -> None:
    """常驻 worker:跑完队列不退出,继续 poll;Web lifespan 负责启动与取消(docs/05 M3)。

    消费循环自带兜底:DB 抖动(database is locked 等)只退避重试,绝不穿出 while——
    task 一旦静默死亡,enqueue 的幂等去重会命中残留 pending/running job 拦住重试,
    UI 将永久停留"排队中"且无任何日志(docs/06 §10 实故障形态)。
    """
    orphans = await _rescan_orphans()
    if orphans:
        print(f"[worker] 恢复 {orphans} 个中断 job(running→pending)")
    last_stale_check = time.monotonic()
    backoff = _BACKOFF_INITIAL
    try:
        while True:
            try:
                job = await _next_pending()
                if job is None:
                    now = time.monotonic()
                    if now - last_stale_check >= _STALE_CHECK_INTERVAL:
                        last_stale_check = now
                        stale = await _reset_stale_running()
                        if stale:
                            print(f"[worker] 复位 {stale} 个超时 running job(→pending)")
                    await asyncio.sleep(settings.worker_poll_interval_sec)
                    continue
                await _execute_one(job)
                backoff = _BACKOFF_INITIAL  # 正常推进则重置退避
                await asyncio.sleep(settings.worker_poll_interval_sec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                traceback.print_exception(exc)
                print(
                    f"[worker] 消费循环异常({type(exc).__name__}: {exc}),"
                    f"退避 {backoff:.0f}s 后继续"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
    except asyncio.CancelledError:
        print("[worker] 收到取消信号,优雅退出")
        raise

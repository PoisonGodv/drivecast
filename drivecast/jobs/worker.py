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
from ..models import Job, Plan
from ..persist import set_episode_status
from ..planner import service as planner
from ..scriptwriter import service as scriptwriter
from ..tts import service as tts

_STALE_CHECK_INTERVAL = 60.0  # 空轮询时最多每分钟做一次超时 running 复位
_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 60.0


def _now_text() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


async def _rescan_orphans() -> int:
    """启动恢复:running → pending(docs/02 §6 断点续跑核心)。"""
    async with session() as s:
        result = await s.execute(
            update(Job).where(Job.status == "running")
            .values(status="pending", updated_at=_now_text())
        )
        await s.commit()
        return result.rowcount


async def _reset_stale_running() -> int:
    """复位挂死的 running(超过 worker_stale_running_sec 无进展视为孤儿)。

    只在空轮询分支调用:队列还有 pending 时说明 worker 活着,不冒险重复执行。
    """
    threshold = (datetime.now() - timedelta(seconds=settings.worker_stale_running_sec)).isoformat(
        sep=" ", timespec="seconds"
    )
    async with session() as s:
        result = await s.execute(
            update(Job).where(Job.status == "running", Job.updated_at < threshold)
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
            # 原子认领:WHERE 带 status='pending' 守卫,web 常驻 worker 与 CLI 并存时不重复执行
            update(Job).where(Job.id == row.id, Job.status == "pending")
            .values(status="running", attempt=Job.attempt + 1, updated_at=_now_text())
        )
        await s.commit()
        if result.rowcount == 0:
            return None  # 被并发消费者抢先,本轮放弃
        # ORM-enabled UPDATE(synchronize_session=auto)已把内存 attempt 同步为
        # DB 更新后的值(实测验证),不再手动 +1——那会双重累加。
        return row


async def _finish(job_id: int, status: str, error: str = "") -> None:
    async with session() as s:
        await s.execute(
            update(Job).where(Job.id == job_id)
            .values(status=status, error=error, updated_at=_now_text())
        )
        await s.commit()


async def _plan_cancelled(plan_ext_id: str) -> bool:
    """计划已取消则 True;纯文件态计划(未入库)视为未取消。"""
    if not plan_ext_id:
        return False
    async with session() as s:
        status = await s.scalar(select(Plan.status).where(Plan.ext_id == plan_ext_id))
    return status == "cancelled"


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
    if await _plan_cancelled(plan.plan_id):
        print(f"[worker] job#{job.id} 所属计划已取消,不链式入队 tts")
        return
    try:
        await enqueue("tts", ep.episode_id, {"plan_id": plan.plan_id, "seq": ep.seq})  # 链式入队 tts
    except Exception as exc:  # noqa: BLE001
        # tts 断链比脚本白跑更糟(DB 抖动常见):显式标 failed 让 UI 可重试,不静默吞掉
        print(f"[worker] job#{job.id} tts 链式入队失败: {exc}")
        await _finish(job.id, "failed", f"tts enqueue failed: {exc}")


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
    print(f"[worker] 执行 job#{job.id} {job.kind}/{job.ref_id} (第{job.attempt}次)")
    payload = json.loads(job.payload)
    if await _plan_cancelled(str(payload.get("plan_id") or "")):
        await _finish(job.id, "cancelled")
        print(f"[worker] job#{job.id} 所属计划已取消,标记 cancelled 跳过")
        return
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

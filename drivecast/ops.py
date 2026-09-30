"""运维:磁盘水位告警、audio/chunks 过期回收、健康检查。"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, text

from .config import settings
from .db import session
from .models import Episode, Job

logger = logging.getLogger(__name__)

AUDIO_KINDS = ("tts", "audio_upgrade", "quality_recheck")
DISK_WARN_INTERVAL_SEC = 3600.0
GC_INTERVAL_SEC = 3600.0

_last_disk_warn: float | None = None
_disk_low = False


def disk_free_mb(path: Path | None = None) -> float:
    """data_dir 所在盘剩余空间(MB);目录尚未建出来时向上找已存在的祖先。"""
    target = Path(path or settings.data_dir)
    while not target.exists() and target.parent != target:
        target = target.parent
    return shutil.disk_usage(target).free / (1024 * 1024)


def disk_ok(*, warn: bool = True) -> bool:
    """空闲量 >= DISK_MIN_FREE_MB。warn=True 时低水位每小时最多告警一次,恢复时记一条。"""
    global _last_disk_warn, _disk_low
    try:
        free = disk_free_mb()
    except OSError as exc:
        free, ok = -1.0, False
        if warn:
            logger.warning(f"无法读取磁盘空间: {exc}")
    else:
        ok = free >= settings.disk_min_free_mb
    if not warn:
        return ok
    now = time.monotonic()
    if not ok:
        if _last_disk_warn is None or now - _last_disk_warn >= DISK_WARN_INTERVAL_SEC:
            logger.warning(f"磁盘空间不足:剩余 {free:.0f}MB < {settings.disk_min_free_mb}MB,"
                           "暂停认领音频任务直至空间恢复")
            _last_disk_warn = now
        _disk_low = True
    elif _disk_low:
        logger.info(f"磁盘空间已恢复:剩余 {free:.0f}MB,恢复音频任务")
        _disk_low = False
        _last_disk_warn = None
    return ok


def _pipeline_current(ep_ext_id: str) -> bool:
    """meta 的 pipeline_version 落后时 audio_upgrade 还要用 chunk 本地重做,不能回收。"""
    from .tts.service import AUDIO_PIPELINE_VERSION
    try:
        meta = json.loads(settings.meta_path(ep_ext_id).read_text("utf-8"))
        return int(meta.get("pipeline_version", 0)) >= AUDIO_PIPELINE_VERSION
    except (OSError, TypeError, ValueError):
        return False


async def gc_chunks(*, dry_run: bool = False, now: float | None = None) -> list[str]:
    """回收成品就绪超过 CHUNKS_RETENTION_DAYS 的集的 chunk 目录,返回(将)删除的集 ext_id。"""
    days = settings.chunks_retention_days
    root = settings.data_dir / "audio" / "chunks"
    if days <= 0 or not root.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - days * 86400
    candidates = sorted(p.name for p in root.iterdir() if p.is_dir())
    if not candidates:
        return []
    async with session() as s:
        ready = set((await s.scalars(select(Episode.ext_id).where(
            Episode.ext_id.in_(candidates), Episode.status == "ready"))).all())
        busy = set((await s.scalars(select(Job.ref_id).where(
            Job.ref_id.in_(candidates), Job.kind.in_(AUDIO_KINDS),
            Job.status.in_(("pending", "running"))))).all())
    removed = []
    for ep_ext_id in candidates:
        if ep_ext_id not in ready or ep_ext_id in busy:
            continue
        final = settings.episode_audio_path(ep_ext_id)
        try:
            st = final.stat()
        except OSError:
            continue
        if st.st_size <= 0 or st.st_mtime > cutoff or not _pipeline_current(ep_ext_id):
            continue
        removed.append(ep_ext_id)
        if not dry_run:
            await asyncio.to_thread(shutil.rmtree, settings.chunks_dir(ep_ext_id), True)
    if removed and not dry_run:
        logger.info(f"回收 {len(removed)} 集过期 chunk 目录: {', '.join(removed)}")
    return removed


async def _db_ok() -> bool:
    try:
        async with asyncio.timeout(3):
            async with session() as s:
                await s.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — 健康检查只报布尔,不外抛
        logger.warning("healthz: 数据库检查失败", exc_info=True)
        return False


async def _queue_ok() -> bool:
    """有 pending 却长时间没有任何 job 推进(认领/完成)才算卡住;正常积压不告警。"""
    if not settings.worker_enabled:
        return True
    try:
        async with session() as s:
            pending = await s.scalar(select(Job.id).where(Job.status == "pending").limit(1))
            if pending is None:
                return True
            # 与 pending 同时存在的 running 行,或最近一次状态变化(updated_at,本地时间)
            running = await s.scalar(select(Job.id).where(Job.status == "running").limit(1))
            if running is not None:
                return True
            last_move = await s.scalar(select(func.max(Job.updated_at)).where(
                Job.status.in_(("done", "failed", "cancelled", "running"))))
            if last_move is None:
                # 从未推进过:以最早 pending 的入队时间为起点(created_at 为 UTC,转本地)
                last_move = await s.scalar(select(func.datetime(func.min(Job.created_at), "localtime"))
                                           .where(Job.status == "pending"))
    except Exception:  # noqa: BLE001
        return False
    if last_move is None:
        return True
    try:
        idle = (datetime.now() - datetime.fromisoformat(str(last_move))).total_seconds()
    except ValueError:
        return True
    return idle < settings.queue_stall_alert_sec


async def health_report(worker_alive: bool) -> dict:
    db = await _db_ok()
    worker = (not settings.worker_enabled) or worker_alive
    disk = disk_ok(warn=False)
    queue = await _queue_ok() if db else False
    if not db or not worker:
        status = "down"
    elif not (disk and queue):
        status = "degraded"
    else:
        status = "ok"
    return {"status": status, "checks": {"db": db, "worker": worker, "disk": disk, "queue": queue}}

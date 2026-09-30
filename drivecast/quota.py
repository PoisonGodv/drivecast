"""每用户每日生成额度 + 用量记账。

额度口径:当日(服务器本地日期)创建、状态 pending/running/done 的 script job 目标分钟数之和;
failed/cancelled 不占额度。在途的大纲 plan job 按 集数×分钟 预占,否则大纲生成的几分钟内
可以连开多个计划绕过额度(大纲完成后由其 script job 接替计数)。
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select

from .config import settings
from .db import session
from .models import Job, UsageEvent, User

_CONSUMING = ("pending", "running", "done")
_INFLIGHT = ("pending", "running")
USAGE_KINDS = ("llm_tokens", "tts_chars")
_user_locks: dict[int, asyncio.Lock] = {}


def _local_date(column):
    # created_at 是 SQLite datetime('now') 写入的 UTC,按服务器本地日期归日
    return func.date(column, "localtime")


def is_exempt(user: User) -> bool:
    names = {n.strip() for n in settings.quota_exempt_usernames.split(",") if n.strip()}
    return settings.quota_daily_episode_minutes <= 0 or user.username in names


def _load_plan(plan_id: str, cache: dict):
    if plan_id not in cache:
        from .planner import service as planner
        try:
            cache[plan_id] = planner.load_plan(plan_id)
        except (OSError, ValueError):
            cache[plan_id] = None
    return cache[plan_id]


def episode_minutes(plan_id: str, seq: int, cache: dict | None = None) -> int:
    plan = _load_plan(plan_id, {} if cache is None else cache)
    if plan is None:
        return settings.target_minutes
    ep = next((e for e in plan.episodes if e.seq == int(seq)), None)
    return int(ep.target_minutes if ep else plan.target_minutes)


def plan_request_minutes(episode_count: int | None, target_minutes: int | None) -> int:
    count = episode_count or settings.default_episode_count
    return int(count) * int(target_minutes or settings.target_minutes)


def _job_minutes(job: Job, cache: dict) -> int:
    try:
        payload = json.loads(job.payload or "{}")
    except ValueError:
        payload = {}
    if job.kind == "plan":
        return plan_request_minutes(payload.get("episode_count"), payload.get("target_minutes"))
    return episode_minutes(str(payload.get("plan_id") or ""), int(payload.get("seq") or 0), cache)


async def minutes_used(user_id: int, day: str | None = None) -> int:
    day = day or date.today().isoformat()
    async with session() as s:
        jobs = (await s.scalars(select(Job).where(
            Job.user_id == user_id,
            _local_date(Job.created_at) == day,
            or_(
                and_(Job.kind == "script", Job.status.in_(_CONSUMING)),
                and_(Job.kind == "plan", Job.status.in_(_INFLIGHT)),
            ),
        ))).all()
    cache: dict = {}
    return sum(_job_minutes(j, cache) for j in jobs)


async def remaining_minutes(user: User) -> int | None:
    """None = 不限额。"""
    if is_exempt(user):
        return None
    return max(settings.quota_daily_episode_minutes - await minutes_used(user.id), 0)


async def check(user: User, minutes: int) -> None:
    if minutes <= 0:
        return
    left = await remaining_minutes(user)
    if left is not None and minutes > left:
        raise HTTPException(status_code=429, detail={
            "code": 429,
            "message": (
                f"今日生成额度不足:本次需要 {minutes} 分钟,今日剩余 {left} 分钟"
                f"(每日上限 {settings.quota_daily_episode_minutes} 分钟),请减少集数/时长或明天再试"
            ),
            "minutes_remaining": left,
        })


@asynccontextmanager
async def reserve(user: User, minutes: int):
    """检查额度并在持锁期间入队:同用户并发请求串行,避免两个请求同时通过检查后超额。"""
    lock = _user_locks.setdefault(user.id, asyncio.Lock())
    async with lock:
        await check(user, minutes)
        yield


async def new_script_minutes(plan_rec) -> int:
    """整计划批量只对将新建的 script job 计额:已有成品音频或已在途的集不算。"""
    refs = [ep.episode_id for ep in plan_rec.episodes]
    async with session() as s:
        inflight = set((await s.scalars(select(Job.ref_id).where(
            Job.kind == "script", Job.ref_id.in_(refs), Job.status.in_(_INFLIGHT)))).all())
    total = 0
    for ep in plan_rec.episodes:
        final = settings.episode_audio_path(ep.episode_id)
        if (final.exists() and final.stat().st_size > 0) or ep.episode_id in inflight:
            continue
        total += int(ep.target_minutes)
    return total


async def has_inflight(kind: str, ref_id: str) -> bool:
    async with session() as s:
        return await s.scalar(select(Job.id).where(
            Job.kind == kind, Job.ref_id == ref_id, Job.status.in_(_INFLIGHT)).limit(1)) is not None


# ---------- 用量记账 ----------

async def record_usage(user_id: int, kind: str, amount, ref_id: str = "") -> None:
    """记账失败只打日志,不能让已完成的生成因此重试。"""
    try:
        amount = int(amount or 0)
        if amount <= 0 or kind not in USAGE_KINDS:
            return
        async with session() as s:
            s.add(UsageEvent(user_id=user_id, kind=kind, amount=amount, ref_id=ref_id))
            await s.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[usage] 记账失败 user={user_id} {kind}={amount} {ref_id}: {exc}")


async def _usage_sums(user_id: int, day: str) -> dict[str, int]:
    async with session() as s:
        rows = (await s.execute(
            select(UsageEvent.kind, func.coalesce(func.sum(UsageEvent.amount), 0))
            .where(UsageEvent.user_id == user_id, _local_date(UsageEvent.created_at) == day)
            .group_by(UsageEvent.kind)
        )).all()
    sums = {kind: 0 for kind in USAGE_KINDS}
    sums.update({kind: int(total) for kind, total in rows})
    return sums


async def today_usage(user: User) -> dict:
    day = date.today().isoformat()
    used = await minutes_used(user.id, day)
    limit = None if is_exempt(user) else settings.quota_daily_episode_minutes
    sums = await _usage_sums(user.id, day)
    return {
        "date": day,
        "minutes_used": used,
        "minutes_limit": limit,
        "minutes_remaining": None if limit is None else max(limit - used, 0),
        "llm_tokens": sums["llm_tokens"],
        "tts_chars": sums["tts_chars"],
    }


async def daily_report(days: int = 7) -> list[dict]:
    """近 N 天(含今天)每用户每日汇总,供 CLI `usage` 使用。"""
    start = (date.today() - timedelta(days=max(days, 1) - 1)).isoformat()
    rows: dict[tuple[str, int], dict] = defaultdict(
        lambda: {"minutes": 0, "llm_tokens": 0, "tts_chars": 0})
    async with session() as s:
        names = dict((await s.execute(select(User.id, User.username))).all())
        jobs = (await s.execute(select(Job, _local_date(Job.created_at)).where(
            Job.kind == "script", Job.status.in_(_CONSUMING),
            _local_date(Job.created_at) >= start))).all()
        events = (await s.execute(
            select(_local_date(UsageEvent.created_at), UsageEvent.user_id, UsageEvent.kind,
                   func.sum(UsageEvent.amount))
            .where(_local_date(UsageEvent.created_at) >= start)
            .group_by(_local_date(UsageEvent.created_at), UsageEvent.user_id, UsageEvent.kind)
        )).all()
    cache: dict = {}
    for job, day in jobs:
        rows[(day, job.user_id)]["minutes"] += _job_minutes(job, cache)
    for day, user_id, kind, total in events:
        if kind in USAGE_KINDS:
            rows[(day, user_id)][kind] += int(total or 0)
    return [
        {"date": day, "user_id": uid, "username": names.get(uid, f"#{uid}"), **vals}
        for (day, uid), vals in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1]), reverse=True)
    ]

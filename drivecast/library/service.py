"""library 聚合层:计划/集/进度/题库与 M5 复习闭环。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, case, delete, func, or_, select

from ..db import session
from ..models import (
    Audio,
    Episode,
    Plan,
    Question,
    ReviewReplay,
    ReviewSettings,
    Topic,
)

_VALID_MASTERY = frozenset({"unknown", "fuzzy", "mastered"})
_WEAK_MASTERY = ("unknown", "fuzzy")
_MASTERY_INTERVAL_DAYS = {"unknown": 1, "fuzzy": 2, "mastered": 7}


def _now() -> datetime:
    """集中时钟入口,测试可替换,生产使用本地时间。"""
    return datetime.now()


def _dt_text(value: datetime) -> str:
    return value.isoformat(sep=" ", timespec="seconds")


def _safe_json_list(raw: str) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _kp_key(q: Question) -> str:
    """旧数据 kp_id 为空时仍按来源集形成稳定、可解释的知识点分组。"""
    value = (q.kp_id or "").strip()
    return value or f"episode:{q.episode_id}"


def _kp_name(kp_id: str, episode_title: str) -> str:
    return episode_title if kp_id.startswith("episode:") else kp_id


def _question_view(q: Question, ep: Episode, plan: Plan, topic: Topic) -> dict:
    kp_id = _kp_key(q)
    return {
        "id": q.id,
        "question": q.question,
        "framework": _safe_json_list(q.framework),
        "mastery": q.mastery,
        "last_reviewed_at": q.last_reviewed_at,
        "next_review_at": q.next_review_at,
        "review_count": q.review_count,
        "difficulty": q.difficulty,
        "qtype": q.qtype,
        "kp_id": kp_id,
        "knowledge_point": _kp_name(kp_id, ep.title),
        "episode_id": ep.id,
        "episode_title": ep.title,
        "plan_id": plan.id,
        "plan_title": plan.title,
        "direction": topic.direction,
    }


async def list_plans() -> list[dict]:
    """计划列表,附带已就绪集数(docs/02 §4)。"""
    async with session() as s:
        result = await s.execute(select(Plan).order_by(Plan.id.desc()))
        rows = result.scalars().all()
        out = []
        for p in rows:
            ready = await s.scalar(
                select(func.count(Episode.id)).where(
                    Episode.plan_id == p.id, Episode.status == "ready"
                )
            )
            out.append({
                "id": p.id,
                "title": p.title,
                "status": p.status,
                "episode_count": p.episode_count,
                "ready_count": ready or 0,
            })
        return out


async def get_plan(plan_id: int) -> dict | None:
    """计划详情,含 episodes + 各集 status/duration/position/listen_status(docs/02 §4 示例)。"""
    async with session() as s:
        p = await s.scalar(select(Plan).where(Plan.id == plan_id))
        if p is None:
            return None
        result = await s.execute(
            select(Episode, Audio)
            .outerjoin(Audio, Audio.episode_id == Episode.id)
            .where(Episode.plan_id == plan_id)
            .order_by(Episode.seq)
        )
        episodes = []
        for ep, audio in result:
            path = Path(audio.file_path) if audio else None
            has_audio = bool(audio and path and path.exists() and path.stat().st_size > 0)
            episodes.append({
                "id": ep.id,
                "ext_id": ep.ext_id,
                "seq": ep.seq,
                "title": ep.title,
                "status": ep.status,
                "has_audio": has_audio,
                "duration_sec": audio.duration_sec if audio else 0,
                "position_sec": ep.position_sec,
                "listen_status": ep.listen_status,
            })
        ready_count = sum(1 for e in episodes if e["status"] == "ready")
        return {
            "id": p.id,
            "title": p.title,
            "status": p.status,
            "ext_id": p.ext_id,
            "episode_count": p.episode_count,
            "ready_count": ready_count,
            "episodes": episodes,
        }


async def get_episode(episode_id: int) -> Episode | None:
    """按整数 id 取 episode 行(用于 API 把 /api/episodes/{id} 映射到 ext_id)。"""
    async with session() as s:
        return await s.scalar(select(Episode).where(Episode.id == episode_id))


async def get_plan_by_id(plan_id: int) -> Plan | None:
    """按整数 id 取 plan 行。"""
    async with session() as s:
        return await s.scalar(select(Plan).where(Plan.id == plan_id))


def _review_stmt():
    return (
        select(Question, Episode, Plan, Topic)
        .join(Episode, Episode.id == Question.episode_id)
        .join(Plan, Plan.id == Episode.plan_id)
        .join(Topic, Topic.id == Plan.topic_id)
    )


async def today_review(
    mastery: str | None = None,
    direction: str | None = None,
) -> list[dict]:
    """今日待复习:未排期题优先,其后按到期时间和薄弱程度稳定排序。"""
    if mastery is not None and mastery not in _VALID_MASTERY:
        raise ValueError(f"不支持的 mastery: {mastery}")
    async with session() as s:
        now = _dt_text(_now())
        where = or_(Question.next_review_at.is_(None), Question.next_review_at <= now)
        if mastery:
            where = and_(where, Question.mastery == mastery)
        if direction:
            where = and_(where, Topic.direction == direction)
        mastery_order = case(
            (Question.mastery == "unknown", 0),
            (Question.mastery == "fuzzy", 1),
            else_=2,
        )
        result = await s.execute(
            _review_stmt()
            .where(where)
            .order_by(
                case((Question.next_review_at.is_(None), 0), else_=1),
                Question.next_review_at.asc(),
                mastery_order,
                Question.last_reviewed_at.asc(),
                Question.id,
            )
        )
        return [_question_view(q, ep, plan, topic) for q, ep, plan, topic in result]


async def list_questions(
    mastery: str | None = None,
    direction: str | None = None,
) -> list[dict]:
    """题库筛选:按 mastery/direction 过滤,不限制到期时间。"""
    if mastery is not None and mastery not in _VALID_MASTERY:
        raise ValueError(f"不支持的 mastery: {mastery}")
    async with session() as s:
        where = True
        if mastery:
            where = and_(where, Question.mastery == mastery)
        if direction:
            where = and_(where, Topic.direction == direction)
        result = await s.execute(_review_stmt().where(where).order_by(Question.id.desc()))
        return [_question_view(q, ep, plan, topic) for q, ep, plan, topic in result]


async def grade_question(question_id: int, mastery: str) -> dict:
    """自评三态可直接切换;标不会即回退 unknown,并重算间隔与重听队列。"""
    if mastery not in _VALID_MASTERY:
        raise ValueError(f"不支持的 mastery: {mastery}")
    async with session() as s:
        q = await s.scalar(select(Question).where(Question.id == question_id))
        if q is None:
            raise LookupError("题目不存在")

        reviewed_at = _now()
        q.kp_id = _kp_key(q)  # 顺手修复 init_db 未跑 migration 的旧库空值
        q.mastery = mastery
        q.review_count = (q.review_count or 0) + 1
        q.last_reviewed_at = _dt_text(reviewed_at)
        q.next_review_at = _dt_text(
            reviewed_at + timedelta(days=_MASTERY_INTERVAL_DAYS[mastery])
        )
        await s.flush()
        replay = await _sync_replay_for_kp(s, q.kp_id, reviewed_at.date())
        await s.commit()
        return {
            "id": q.id,
            "mastery": q.mastery,
            "last_reviewed_at": q.last_reviewed_at,
            "next_review_at": q.next_review_at,
            "review_count": q.review_count,
            "replay": replay,
        }


async def _weak_groups(s) -> list[dict]:
    """Python 侧归并兼容空 kp_id;题量很小,换来迁移前后语义一致。"""
    result = await s.execute(
        select(
            Question,
            Episode,
            Plan,
            Topic,
            Audio,
            func.count(Question.id).over(
                partition_by=(Question.kp_id, Question.episode_id)
            ).label("episode_weak_count"),
        )
        .join(Episode, Episode.id == Question.episode_id)
        .join(Plan, Plan.id == Episode.plan_id)
        .join(Topic, Topic.id == Plan.topic_id)
        .outerjoin(Audio, Audio.episode_id == Episode.id)
        .where(Question.mastery.in_(_WEAK_MASTERY))
        .order_by(Question.id)
    )
    rows = list(result)
    groups: dict[str, dict] = {}
    for q, ep, plan, topic, audio, episode_weak_count in rows:
        kp_id = _kp_key(q)
        group = groups.setdefault(kp_id, {
            "kp_id": kp_id,
            "knowledge_point": _kp_name(kp_id, ep.title),
            "direction": topic.direction,
            "question_ids": [],
            "episode_candidates": {},
        })
        group["question_ids"].append(q.id)
        duration = audio.duration_sec if audio and audio.duration_sec > 0 else None
        current = group["episode_candidates"].get(ep.id)
        candidate = {
            "episode_id": ep.id,
            "episode_title": ep.title,
            "plan_id": plan.id,
            "plan_title": plan.title,
            "duration_sec": duration,
            "weak_question_count": episode_weak_count,
        }
        if current is None or (
            duration is not None
            and (current["duration_sec"] is None or duration < current["duration_sec"])
        ):
            group["episode_candidates"][ep.id] = candidate
    return list(groups.values())


def _shortest_episode(group: dict) -> dict:
    candidates = list(group["episode_candidates"].values())
    return min(
        candidates,
        key=lambda item: (
            item["duration_sec"] is None,
            item["duration_sec"] if item["duration_sec"] is not None else 0,
            item["episode_id"],
        ),
    )


async def _sync_replay_for_kp(s, kp_id: str, today: date) -> dict | None:
    """知识点仍有至少 2 道薄弱题才保留明日任务;恢复后撤销未来 pending。"""
    groups = await _weak_groups(s)
    group = next((item for item in groups if item["kp_id"] == kp_id), None)
    tomorrow = today + timedelta(days=1)
    if group is None or len(group["question_ids"]) < 2:
        await s.execute(
            delete(ReviewReplay).where(
                ReviewReplay.kp_id == kp_id,
                ReviewReplay.scheduled_for >= tomorrow.isoformat(),
                ReviewReplay.status == "pending",
            )
        )
        return None

    episode = _shortest_episode(group)
    replay = await s.scalar(
        select(ReviewReplay).where(
            ReviewReplay.kp_id == kp_id,
            ReviewReplay.scheduled_for == tomorrow.isoformat(),
        )
    )
    if replay is None:
        replay = ReviewReplay(
            kp_id=kp_id,
            episode_id=episode["episode_id"],
            scheduled_for=tomorrow.isoformat(),
            weak_question_count=len(group["question_ids"]),
        )
        s.add(replay)
        await s.flush()
    else:
        replay.episode_id = episode["episode_id"]
        replay.weak_question_count = len(group["question_ids"])
        replay.status = "pending"
    return {
        "id": replay.id,
        "scheduled_for": replay.scheduled_for,
        "episode_id": replay.episode_id,
        "weak_question_count": replay.weak_question_count,
    }


async def weak_points() -> list[dict]:
    """薄弱知识点:unknown/fuzzy 合计至少 2 题,推荐最短有时长的相关集。"""
    async with session() as s:
        groups = await _weak_groups(s)
        out = []
        for group in groups:
            count = len(group["question_ids"])
            if count < 2:
                continue
            episode = _shortest_episode(group)
            out.append({
                "kp_id": group["kp_id"],
                "knowledge_point": group["knowledge_point"],
                "direction": group["direction"],
                "weak_question_count": count,
                "question_ids": group["question_ids"],
                "recommended_episode": episode,
            })
        return sorted(out, key=lambda item: (-item["weak_question_count"], item["kp_id"]))


async def sync_replay_queue() -> list[dict]:
    """同步次日排期，并优先返回今天仍到期的持久化任务。"""
    async with session() as s:
        today = _now().date()
        groups = await _weak_groups(s)
        active = {
            group["kp_id"]
            for group in groups
            if len(group["question_ids"]) >= 2
        }
        for kp_id in active:
            await _sync_replay_for_kp(s, kp_id, today)
        tomorrow = (today + timedelta(days=1)).isoformat()
        stale = delete(ReviewReplay).where(
            ReviewReplay.scheduled_for == tomorrow,
            ReviewReplay.status == "pending",
        )
        if active:
            stale = stale.where(ReviewReplay.kp_id.not_in(active))
        await s.execute(stale)
        await s.commit()
    # 今天已有排期时不能被新建的明日任务遮住；为空才提供明日预览。
    due = await replay_queue(today)
    return due or await replay_queue(today + timedelta(days=1))


async def replay_queue(for_date: date | None = None) -> list[dict]:
    """读取某日重听队列；默认今天，返回可直接播放的 episode 元数据。"""
    scheduled_for = (for_date or _now().date()).isoformat()
    async with session() as s:
        result = await s.execute(
            select(ReviewReplay, Episode, Plan, Topic, Audio)
            .join(Episode, Episode.id == ReviewReplay.episode_id)
            .join(Plan, Plan.id == Episode.plan_id)
            .join(Topic, Topic.id == Plan.topic_id)
            .outerjoin(Audio, Audio.episode_id == Episode.id)
            .where(
                ReviewReplay.scheduled_for == scheduled_for,
                ReviewReplay.status == "pending",
            )
            .order_by(ReviewReplay.weak_question_count.desc(), Audio.duration_sec, Episode.id)
        )
        return [{
            "id": replay.id,
            "scheduled_for": replay.scheduled_for,
            "status": replay.status,
            "kp_id": replay.kp_id,
            "knowledge_point": _kp_name(replay.kp_id, ep.title),
            "weak_question_count": replay.weak_question_count,
            "episode_id": ep.id,
            "episode_title": ep.title,
            "plan_id": plan.id,
            "plan_title": plan.title,
            "direction": topic.direction,
            "duration_sec": audio.duration_sec if audio else None,
        } for replay, ep, plan, topic, audio in result]


async def set_interview_date(interview_date: date | None) -> dict:
    """持久化单用户面试日;传 null 可退出冲刺模式。"""
    async with session() as s:
        settings = await s.get(ReviewSettings, 1)
        if settings is None:
            settings = ReviewSettings(id=1)
            s.add(settings)
        settings.interview_date = interview_date.isoformat() if interview_date else None
        settings.updated_at = _dt_text(_now())
        await s.commit()
        return {
            "interview_date": settings.interview_date,
            "updated_at": settings.updated_at,
        }


async def get_interview_date() -> date | None:
    async with session() as s:
        settings = await s.get(ReviewSettings, 1)
        if settings is None or not settings.interview_date:
            return None
        return date.fromisoformat(settings.interview_date)


def _sprint_days(interview_date: date) -> list[date]:
    return [interview_date - timedelta(days=offset) for offset in (3, 2, 1)]


async def sprint_list() -> dict:
    """考前三天冲刺清单:方向分组,组内按单集薄弱题数倒序。"""
    interview_date = await get_interview_date()
    if interview_date is None:
        raise ValueError("请先设置面试日")

    sprint_days = _sprint_days(interview_date)
    async with session() as s:
        result = await s.execute(
            select(
                Topic.direction,
                Episode.id,
                Episode.title,
                Plan.id,
                Plan.title,
                Audio.duration_sec,
                func.count(Question.id),
            )
            .join(Plan, Plan.topic_id == Topic.id)
            .join(Episode, Episode.plan_id == Plan.id)
            .join(Question, Question.episode_id == Episode.id)
            .outerjoin(Audio, Audio.episode_id == Episode.id)
            .where(Question.mastery != "mastered")
            .group_by(
                Topic.direction,
                Episode.id,
                Episode.title,
                Plan.id,
                Plan.title,
                Audio.duration_sec,
            )
            .order_by(Topic.direction, func.count(Question.id).desc(), Episode.id)
        )
        rows = list(result)
        grouped: dict[str, list[dict]] = {}
        for direction, ep_id, ep_title, plan_id, plan_title, duration, count in rows:
            item = {
                "episode_id": ep_id,
                "episode_title": ep_title,
                "plan_id": plan_id,
                "plan_title": plan_title,
                "duration_sec": duration,
                "weak_question_count": count,
            }
            grouped.setdefault(direction, []).append(item)

        directions = []
        for direction, items in grouped.items():
            ordered = sorted(
                items,
                key=lambda item: (-item["weak_question_count"], item["episode_id"]),
            )
            for index, item in enumerate(ordered):
                # 每个方向都从题最多的集开始排;超过三集则轮转,保证都有日期。
                item["scheduled_for"] = sprint_days[index % 3].isoformat()
            directions.append({
                "direction": direction,
                "weak_question_count": sum(item["weak_question_count"] for item in ordered),
                "episodes": ordered,
            })
        directions.sort(key=lambda item: (-item["weak_question_count"], item["direction"]))
        return {
            "interview_date": interview_date.isoformat(),
            "sprint_start": sprint_days[0].isoformat(),
            "sprint_end": sprint_days[-1].isoformat(),
            "directions": directions,
        }

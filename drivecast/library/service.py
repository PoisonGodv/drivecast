"""library 聚合层:计划/集/进度/题库与 M5 复习闭环。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, case, delete, func, or_, select

from ..content.loader import get_question_bank
from ..db import session
from ..models import (
    Audio,
    Episode,
    Plan,
    Question,
    ReviewReplay,
    ReviewSettings,
    Script,
    Topic,
)
from ..schemas import DIRECTION_NAMES

_VALID_MASTERY = frozenset({"unknown", "fuzzy", "mastered"})
_WEAK_MASTERY = ("unknown", "fuzzy")
# 遗忘曲线阶梯:unknown/fuzzy 固定短间隔;
# mastered 按已复习次数取 1/2/4/7/15/30 天(第 6 次起稳定 30 天)
_FIXED_INTERVAL_DAYS = {"unknown": 1, "fuzzy": 2}
_MASTERED_STEPS_DAYS = [1, 2, 4, 7, 15, 30]


def _interval_days(mastery: str, review_count: int) -> int:
    """review_count 为自增后的已复习次数;mastered 第 N 次取阶梯第 N 级。"""
    if mastery != "mastered":
        return _FIXED_INTERVAL_DAYS[mastery]
    index = min(max(review_count - 1, 0), len(_MASTERED_STEPS_DAYS) - 1)
    return _MASTERED_STEPS_DAYS[index]


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


def _question_view(q: Question, ep: Episode, plan: Plan, topic: Topic,
                   script: Script | None = None) -> dict:
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
        # 复习卡片附参考链接(脚本 refs,题库题 refs 的集级汇总)
        "references": _safe_json_list(script.refs) if script else [],
    }


async def list_plans(user_id: int | None = None) -> list[dict]:
    """计划列表,附带已就绪集数(docs/02 §4)。user_id=None 表示不过滤(内部/CLI)。"""
    async with session() as s:
        stmt = select(Plan)
        if user_id is not None:
            stmt = stmt.where(Plan.user_id == user_id)
        result = await s.execute(stmt.order_by(Plan.id.desc()))
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


async def get_plan(plan_id: int, user_id: int | None = None) -> dict | None:
    """计划详情,含 episodes + 各集 status/duration/position/listen_status(docs/02 §4 示例)。

    user_id 非 None 时按归属过滤(跨用户与不存在同返回 None,不泄露存在性)。
    """
    from ..library import artifacts
    from ..scriptwriter import service as scriptwriter

    async with session() as s:
        stmt = select(Plan).where(Plan.id == plan_id)
        if user_id is not None:
            stmt = stmt.where(Plan.user_id == user_id)
        p = await s.scalar(stmt)
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
            # 审稿/人审徽章数据走文件态(docs/07 §3.1/§4),读不到不影响详情
            review = (scriptwriter.load_script(ep.ext_id) or {}).get("review") or {}
            episodes.append({
                "id": ep.id,
                "ext_id": ep.ext_id,
                "seq": ep.seq,
                "title": ep.title,
                "status": ep.status,
                "scheduled_date": ep.scheduled_date,  # 排期日 YYYY-MM-DD,前端展示排期
                "has_audio": has_audio,
                "duration_sec": audio.duration_sec if audio else 0,
                "position_sec": ep.position_sec,
                "listen_status": ep.listen_status,
                "review_soft_flags": sum(
                    1 for f in review.get("flags", []) if f.get("type") == "soft"
                ),
                "review_pending_error": bool(review.get("pending_error")),
                "human_qc_passed": (artifacts.load_human_qc(ep.ext_id) or {}).get("status") == "passed",
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


async def get_episode(episode_id: int, user_id: int | None = None) -> Episode | None:
    """按整数 id 取 episode 行(用于 API 把 /api/episodes/{id} 映射到 ext_id)。

    episode 无冗余 user_id,user_id 非 None 时经 plan 关联校验归属。
    """
    async with session() as s:
        stmt = select(Episode).where(Episode.id == episode_id)
        if user_id is not None:
            stmt = stmt.join(Plan, Plan.id == Episode.plan_id).where(Plan.user_id == user_id)
        return await s.scalar(stmt)


async def get_plan_by_id(plan_id: int, user_id: int | None = None) -> Plan | None:
    """按整数 id 取 plan 行;user_id 非 None 时按归属过滤。"""
    async with session() as s:
        stmt = select(Plan).where(Plan.id == plan_id)
        if user_id is not None:
            stmt = stmt.where(Plan.user_id == user_id)
        return await s.scalar(stmt)


def _review_stmt(user_id: int | None = None):
    stmt = (
        select(Question, Episode, Plan, Topic, Script)
        .join(Episode, Episode.id == Question.episode_id)
        .join(Plan, Plan.id == Episode.plan_id)
        .join(Topic, Topic.id == Plan.topic_id)
        .outerjoin(Script, Script.episode_id == Episode.id)  # 取复习卡片参考链接
    )
    if user_id is not None:
        stmt = stmt.where(Plan.user_id == user_id)
    return stmt


async def today_review(
    mastery: str | None = None,
    direction: str | None = None,
    user_id: int | None = None,
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
            _review_stmt(user_id)
            .where(where)
            .order_by(
                case((Question.next_review_at.is_(None), 0), else_=1),
                Question.next_review_at.asc(),
                mastery_order,
                Question.last_reviewed_at.asc(),
                Question.id,
            )
        )
        return [_question_view(q, ep, plan, topic, script)
                for q, ep, plan, topic, script in result]


async def list_questions(
    mastery: str | None = None,
    direction: str | None = None,
    user_id: int | None = None,
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
        result = await s.execute(_review_stmt(user_id).where(where).order_by(Question.id.desc()))
        return [_question_view(q, ep, plan, topic, script)
                for q, ep, plan, topic, script in result]


async def grade_question(
    question_id: int,
    mastery: str,
    user_id: int | None = None,
) -> dict:
    """自评三态可直接切换;标不会即回退 unknown,并重算间隔与重听队列。

    user_id 非 None 时经 episode→plan 校验归属,跨用户与不存在同抛 LookupError(→404)。
    """
    if mastery not in _VALID_MASTERY:
        raise ValueError(f"不支持的 mastery: {mastery}")
    async with session() as s:
        stmt = select(Question).where(Question.id == question_id)
        if user_id is not None:
            stmt = (
                stmt.join(Episode, Episode.id == Question.episode_id)
                .join(Plan, Plan.id == Episode.plan_id)
                .where(Plan.user_id == user_id)
            )
        q = await s.scalar(stmt)
        if q is None:
            raise LookupError("题目不存在")

        reviewed_at = _now()
        q.kp_id = _kp_key(q)  # 顺手修复 init_db 未跑 migration 的旧库空值
        q.mastery = mastery
        q.review_count = (q.review_count or 0) + 1
        q.last_reviewed_at = _dt_text(reviewed_at)
        q.next_review_at = _dt_text(
            reviewed_at + timedelta(days=_interval_days(mastery, q.review_count))
        )
        await s.flush()
        replay = await _sync_replay_for_kp(s, q.kp_id, reviewed_at.date(), user_id)
        await s.commit()
        return {
            "id": q.id,
            "mastery": q.mastery,
            "last_reviewed_at": q.last_reviewed_at,
            "next_review_at": q.next_review_at,
            "review_count": q.review_count,
            "replay": replay,
        }


async def _weak_groups(s, user_id: int | None = None) -> list[dict]:
    """Python 侧归并兼容空 kp_id;题量很小,换来迁移前后语义一致。"""
    stmt = (
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
    if user_id is not None:
        stmt = stmt.where(Plan.user_id == user_id)
    result = await s.execute(stmt)
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


async def _sync_replay_for_kp(s, kp_id: str, today: date, user_id: int | None = None) -> dict | None:
    """知识点仍有至少 2 道薄弱题才保留明日任务;恢复后撤销未来 pending。

    user_id 非 None 时只增删该用户的重听任务(kp_id 是全局文本命名空间,
    隔离完全依赖 user_id 列 + UNIQUE(user_id,kp_id,scheduled_for) 兜底)。
    """
    owner = user_id if user_id is not None else 1
    groups = await _weak_groups(s, user_id)
    group = next((item for item in groups if item["kp_id"] == kp_id), None)
    tomorrow = today + timedelta(days=1)
    replay_filters = [
        ReviewReplay.kp_id == kp_id,
        ReviewReplay.scheduled_for >= tomorrow.isoformat(),
        ReviewReplay.status == "pending",
    ]
    if user_id is not None:
        replay_filters.append(ReviewReplay.user_id == user_id)
    if group is None or len(group["question_ids"]) < 2:
        await s.execute(delete(ReviewReplay).where(*replay_filters))
        return None

    episode = _shortest_episode(group)
    replay = await s.scalar(
        select(ReviewReplay).where(
            ReviewReplay.kp_id == kp_id,
            ReviewReplay.scheduled_for == tomorrow.isoformat(),
            ReviewReplay.user_id == owner,
        )
    )
    if replay is None:
        replay = ReviewReplay(
            user_id=owner,
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


async def daily_plan(user_id: int | None = None) -> dict:
    """今日学习视图:今天排期的新集 + 复习题三态统计(到期/逾期/未排期)。"""
    now = _now()
    today = now.date().isoformat()
    async with session() as s:
        stmt = (
            select(Episode, Plan, Topic)
            .join(Plan, Plan.id == Episode.plan_id)
            .join(Topic, Topic.id == Plan.topic_id)
            .where(Episode.scheduled_date == today, Plan.status == "active")
            .order_by(Plan.id, Episode.seq)
        )
        if user_id is not None:
            stmt = stmt.where(Plan.user_id == user_id)
        result = await s.execute(stmt)
        new_episodes = [{
            "episode_id": ep.id,
            "plan_id": plan.id,
            "plan_title": plan.title,
            "seq": ep.seq,
            "title": ep.title,
            "scheduled_date": ep.scheduled_date,
            "listen_status": ep.listen_status,
            "status": ep.status,
            "direction": topic.direction,
        } for ep, plan, topic in result]

        async def _count(*extra) -> int:
            # question 无冗余 user_id,统计一律经 episode→plan 关联过滤
            stmt = (
                select(func.count(Question.id))
                .join(Episode, Episode.id == Question.episode_id)
                .join(Plan, Plan.id == Episode.plan_id)
            )
            if user_id is not None:
                stmt = stmt.where(Plan.user_id == user_id)
            return (await s.scalar(stmt.where(*extra))) or 0

        now_text = _dt_text(now)
        due_count = await _count(
            Question.next_review_at.is_not(None),
            Question.next_review_at <= now_text,
        )
        overdue_count = await _count(
            Question.next_review_at.is_not(None),
            Question.next_review_at < f"{today} 00:00:00",
        )
        new_count = await _count(Question.next_review_at.is_(None))
    return {
        "date": today,
        "new_episodes": new_episodes,
        "due_count": due_count,
        "overdue_count": overdue_count,
        "new_count": new_count,
    }


async def coverage(user_id: int | None = None) -> dict:
    """题库全局覆盖视角:各方向已覆盖/未覆盖高频题统计(数据源是 episode.covers 并集)。"""
    async with session() as s:
        stmt = (
            select(Topic.direction, Episode.covers)
            .join(Plan, Plan.topic_id == Topic.id)
            .join(Episode, Episode.plan_id == Plan.id)
        )
        if user_id is not None:
            stmt = stmt.where(Plan.user_id == user_id)
        result = await s.execute(stmt)
        covered_by_direction: dict[str, set[str]] = {}
        for direction, covers_raw in result:
            covered_by_direction.setdefault(direction, set()).update(
                str(qid) for qid in _safe_json_list(covers_raw)
            )

    directions = []
    for direction in DIRECTION_NAMES:  # 只输出题库文件存在的方向
        bank = get_question_bank(direction)
        if not bank:
            continue
        covered_ids = covered_by_direction.get(direction, set())
        covered_count = sum(1 for q in bank if str(q.get("id")) in covered_ids)
        total = len(bank)
        uncovered_high = [
            {"id": q.get("id"), "question": q.get("question", "")}
            for q in bank
            if q.get("frequency") == "high" and str(q.get("id")) not in covered_ids
        ][:20]
        directions.append({
            "direction": direction,
            "name": DIRECTION_NAMES[direction],
            "total": total,
            "covered": covered_count,
            "percent": round(covered_count * 100 / total) if total else 0,
            "uncovered_high": uncovered_high,
        })
    return {"directions": directions}


async def weak_points(user_id: int | None = None) -> list[dict]:
    """薄弱知识点:unknown/fuzzy 合计至少 2 题,推荐最短有时长的相关集。"""
    async with session() as s:
        groups = await _weak_groups(s, user_id)
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


async def sync_replay_queue(user_id: int | None = None) -> list[dict]:
    """同步次日排期，并优先返回今天仍到期的持久化任务。"""
    async with session() as s:
        today = _now().date()
        groups = await _weak_groups(s, user_id)
        active = {
            group["kp_id"]
            for group in groups
            if len(group["question_ids"]) >= 2
        }
        for kp_id in active:
            await _sync_replay_for_kp(s, kp_id, today, user_id)
        tomorrow = (today + timedelta(days=1)).isoformat()
        stale = delete(ReviewReplay).where(
            ReviewReplay.scheduled_for == tomorrow,
            ReviewReplay.status == "pending",
        )
        if user_id is not None:
            stale = stale.where(ReviewReplay.user_id == user_id)
        if active:
            stale = stale.where(ReviewReplay.kp_id.not_in(active))
        await s.execute(stale)
        await s.commit()
    # 今天已有排期时不能被新建的明日任务遮住；为空才提供明日预览。
    due = await replay_queue(today, user_id)
    return due or await replay_queue(today + timedelta(days=1), user_id)


async def replay_queue(for_date: date | None = None, user_id: int | None = None) -> list[dict]:
    """读取某日重听队列；默认今天，返回可直接播放的 episode 元数据。"""
    scheduled_for = (for_date or _now().date()).isoformat()
    async with session() as s:
        stmt = (
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
        if user_id is not None:
            stmt = stmt.where(ReviewReplay.user_id == user_id)
        result = await s.execute(stmt)
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


async def set_interview_date(interview_date: date | None, user_id: int | None = None) -> dict:
    """持久化面试日(按用户一行);传 null 可退出冲刺模式。user_id 缺省落 admin(=1)。"""
    uid = user_id if user_id is not None else 1
    async with session() as s:
        row = await s.scalar(select(ReviewSettings).where(ReviewSettings.user_id == uid))
        if row is None:
            row = ReviewSettings(user_id=uid)
            s.add(row)
        row.interview_date = interview_date.isoformat() if interview_date else None
        row.updated_at = _dt_text(_now())
        await s.commit()
        return {
            "interview_date": row.interview_date,
            "updated_at": row.updated_at,
        }


async def get_interview_date(user_id: int | None = None) -> date | None:
    uid = user_id if user_id is not None else 1
    async with session() as s:
        row = await s.scalar(select(ReviewSettings).where(ReviewSettings.user_id == uid))
        if row is None or not row.interview_date:
            return None
        return date.fromisoformat(row.interview_date)


def _sprint_days(interview_date: date) -> list[date]:
    return [interview_date - timedelta(days=offset) for offset in (3, 2, 1)]


async def sprint_list(user_id: int | None = None) -> dict:
    """考前三天冲刺清单:方向分组,组内按单集薄弱题数倒序。"""
    interview_date = await get_interview_date(user_id)
    if interview_date is None:
        raise ValueError("请先设置面试日")

    sprint_days = _sprint_days(interview_date)
    async with session() as s:
        stmt = (
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
        if user_id is not None:
            stmt = stmt.where(Plan.user_id == user_id)
        result = await s.execute(stmt)
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

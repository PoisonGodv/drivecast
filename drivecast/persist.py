"""文件产物 → DB 双写(docs/05 M2-1):文件是过程态,DB 是 Web 查询态。

所有内容模块只调这里的函数,不直接碰 SQLAlchemy,保持模块边界(docs/02 §2)。
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

from sqlalchemy import select, update

from .db import session
from .models import Audio, Episode, Job, Plan, Question, ReviewReplay, Script, Topic
from .schemas import PlanRec


def _now() -> datetime:
    """集中时钟入口,测试可替换,生产使用本地时间。"""
    return datetime.now()


def _dt_text(value: datetime) -> str:
    # 与 library/service.py 的 _dt_text 一致:空格分隔,秒精度
    return value.isoformat(sep=" ", timespec="seconds")


def _initial_review_at(scheduled_date: str | None) -> str:
    """新题首排期:所在集排期日当天 09:00;排期缺失/已过期则今天 09:00。

    今天 09:00 已过就取当前时间,保证新题今天就能进入今日复习;
    避免历史上一生成即 next_review_at=NULL 全部挤爆今日复习。
    """
    now = _now()
    day: date | None = None
    if scheduled_date:
        try:
            day = date.fromisoformat(scheduled_date)
        except ValueError:
            day = None
    if day is None or day < now.date():
        day = now.date()
    target = datetime.combine(day, time(9, 0, 0))
    return _dt_text(target if target > now else now)


async def upsert_plan(plan: PlanRec, user_id: int | None = None) -> None:
    """文件态计划落 DB;归属解析:既有占位行(Web 异步创建已带 user_id)> 入参 > admin(=1)。

    topic 按标题+方向在归属者范围内复用,避免两用户同题名互相接管对方 topic。
    """
    async with session() as s:
        plan_row = await s.scalar(select(Plan).where(Plan.ext_id == plan.plan_id))
        owner = plan_row.user_id if plan_row is not None else (user_id if user_id is not None else 1)

        topic_id = await s.scalar(
            select(Topic.id).where(
                Topic.title == plan.user_topic,
                Topic.direction == plan.direction,
                Topic.user_id == owner,
            )
        )
        if topic_id is None:
            t = Topic(title=plan.user_topic, direction=plan.direction, user_id=owner)
            s.add(t)
            await s.flush()
            topic_id = t.id

        if plan_row is None:
            p = Plan(ext_id=plan.plan_id, topic_id=topic_id, title=plan.title,
                     status="active", episode_count=len(plan.episodes), user_id=owner)
            s.add(p)
            await s.flush()
            plan_id_db = p.id
        else:
            plan_id_db = plan_row.id
            values: dict = {"title": plan.title, "episode_count": len(plan.episodes)}
            # 异步创建的 generating 占位行在此转正;cancelled 等终态绝不回捞
            if plan_row.status == "generating":
                values["status"] = "active"
            await s.execute(update(Plan).where(Plan.id == plan_id_db).values(**values))

        for ep in plan.episodes:
            covers_json = json.dumps(ep.covers, ensure_ascii=False)
            scheduled_date = ep.scheduled_date or None
            exists = await s.scalar(select(Episode.id).where(Episode.ext_id == ep.episode_id))
            if exists is None:
                s.add(Episode(
                    ext_id=ep.episode_id, plan_id=plan_id_db, seq=ep.seq, title=ep.title,
                    outline=ep.outline, objectives=json.dumps(ep.objectives, ensure_ascii=False),
                    script_format=ep.script_format, status=ep.status,
                    scheduled_date=scheduled_date, covers=covers_json,
                ))
            else:
                await s.execute(
                    update(Episode).where(Episode.ext_id == ep.episode_id)
                    .values(status=ep.status, title=ep.title, script_format=ep.script_format,
                            scheduled_date=scheduled_date, covers=covers_json)
                )
        await s.commit()


async def upsert_script(ep_ext_id: str, data: dict) -> None:
    """脚本 + 题库(docs/05 M2-5:question 随脚本一次产出,mastery=unknown)。

    新题按所在集排期日 09:00 起排(见 _initial_review_at),不再一律 NULL。
    """
    async with session() as s:
        ep = await s.scalar(select(Episode).where(Episode.ext_id == ep_ext_id))
        if ep is None:
            return  # plan 未入库(纯文件态运行),跳过双写
        ep_id = ep.id
        # 先更新 Episode 状态:若已被删除栅栏标为 cancelled,则整笔写入作废,不插入残留行
        result = await s.execute(
            update(Episode).where(Episode.id == ep_id, Episode.status != "cancelled")
            .values(status="scripted")
        )
        if result.rowcount == 0:
            return
        refs_json = json.dumps(data.get("references", []), ensure_ascii=False)
        exists = await s.scalar(select(Script.id).where(Script.episode_id == ep_id))
        if exists is None:
            s.add(Script(episode_id=ep_id, content_md=data["content"],
                         knowledge=json.dumps(data.get("knowledge", []), ensure_ascii=False),
                         refs=refs_json,
                         token_usage=data.get("usage_tokens", 0)))
        else:
            await s.execute(
                update(Script).where(Script.episode_id == ep_id)
                .values(content_md=data["content"],
                        knowledge=json.dumps(data.get("knowledge", []), ensure_ascii=False),
                        refs=refs_json,
                        token_usage=data.get("usage_tokens", 0))
            )
            from sqlalchemy import delete
            await s.execute(delete(Question).where(Question.episode_id == ep_id))
        first_review = _initial_review_at(ep.scheduled_date)  # 重生成重建同样按排期起排
        knowledge = [str(item).strip() for item in data.get("knowledge", []) if str(item).strip()]
        default_kp = f"episode:{ep_id}"
        if knowledge:
            default_kp = f"knowledge:{knowledge[0]}"
        for q in data.get("questions", []):
            kp_id = str(q.get("kp_id", "")).strip() or default_kp
            s.add(Question(
                episode_id=ep_id, kp_id=kp_id, qtype=q.get("qtype", "concept"),
                question=q["question"],
                framework=json.dumps(q.get("framework", []), ensure_ascii=False),
                difficulty=q.get("difficulty", "medium"),
                next_review_at=first_review,
            ))
        await s.commit()


async def upsert_audio(ep_ext_id: str, meta: dict, voice: str) -> None:
    async with session() as s:
        ep_id = await s.scalar(select(Episode.id).where(Episode.ext_id == ep_ext_id))
        if ep_id is None:
            return
        result = await s.execute(
            update(Episode).where(Episode.id == ep_id, Episode.status != "cancelled")
            .values(status="ready")
        )
        if result.rowcount == 0:
            return
        path = meta["audio_path"]
        size = Path(path).stat().st_size if Path(path).exists() else 0
        exists = await s.scalar(select(Audio.id).where(Audio.episode_id == ep_id))
        meta_json = json.dumps(meta, ensure_ascii=False)
        quality_status = meta.get("quality", {}).get("status", "unknown")
        if exists is None:
            s.add(Audio(episode_id=ep_id, file_path=path, voice=voice,
                        duration_sec=int(round(meta.get("duration_sec", 0))), size_bytes=size,
                        meta_json=meta_json, quality_status=quality_status))
        else:
            await s.execute(
                update(Audio).where(Audio.episode_id == ep_id)
                .values(file_path=path, voice=voice,
                        duration_sec=int(round(meta.get("duration_sec", 0))), size_bytes=size,
                        meta_json=meta_json, quality_status=quality_status)
            )
        await s.commit()


async def set_episode_status(ep_ext_id: str, status: str) -> None:
    async with session() as s:
        # 同上:cancelled 为终态,失败标记/重生成复位等生产链路写入均不覆盖
        await s.execute(
            update(Episode).where(Episode.ext_id == ep_ext_id, Episode.status != "cancelled")
            .values(status=status)
        )
        await s.commit()


async def finalize_plan_deletion(
    plan_id: int,
    plan_ext_id: str,
    topic_id: int,
    ep_ids: list[int],
    ep_ext_ids: list[str],
) -> None:
    """单事务级联物理删除 Plan 及所有关联业务与学习数据;孤立 Topic 顺带清理。"""
    from sqlalchemy import delete
    async with session() as s:
        if ep_ids:
            # 1. 学习闭环派生记录
            await s.execute(delete(ReviewReplay).where(ReviewReplay.episode_id.in_(ep_ids)))
            await s.execute(delete(Question).where(Question.episode_id.in_(ep_ids)))
            # 2. 生产产物记录
            await s.execute(delete(Audio).where(Audio.episode_id.in_(ep_ids)))
            await s.execute(delete(Script).where(Script.episode_id.in_(ep_ids)))
        # 3. 队列表任务(集任务 + 以计划 ext_id 为 ref_id 的大纲生成 plan 任务)
        await s.execute(delete(Job).where(Job.ref_id.in_([*ep_ext_ids, plan_ext_id])))
        # 4. 集与计划本体
        await s.execute(delete(Episode).where(Episode.plan_id == plan_id))
        await s.execute(delete(Plan).where(Plan.id == plan_id))

        # 5. 孤立 Topic 检查并删除
        other_plans = await s.scalar(select(Plan.id).where(Plan.topic_id == topic_id).limit(1))
        if other_plans is None:
            await s.execute(delete(Topic).where(Topic.id == topic_id))

        await s.commit()


async def set_plan_status(plan_row_id: int, status: str) -> None:
    """计划状态流转(active ↔ cancelled);plan.status 此前写入即冻结,这里补上更新入口。"""
    async with session() as s:
        await s.execute(update(Plan).where(Plan.id == plan_row_id).values(status=status))
        await s.commit()


async def set_plan_status_by_ext(plan_ext_id: str, status: str) -> None:
    """按 ext_id 更新计划状态(大纲生成失败等终态);cancelled 为终态,不覆盖。"""
    async with session() as s:
        await s.execute(
            update(Plan).where(Plan.ext_id == plan_ext_id, Plan.status != "cancelled")
            .values(status=status)
        )
        await s.commit()


async def set_plan_episodes_status(plan_row_id: int, from_statuses: tuple[str, ...], status: str) -> int:
    """批量改计划下集状态(取消时把未完成集标 cancelled);返回受影响行数。"""
    async with session() as s:
        result = await s.execute(
            update(Episode)
            .where(Episode.plan_id == plan_row_id, Episode.status.in_(from_statuses))
            .values(status=status)
        )
        await s.commit()
        return result.rowcount

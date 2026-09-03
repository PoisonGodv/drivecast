"""文件产物 → DB 双写(docs/05 M2-1):文件是过程态,DB 是 Web 查询态。

所有内容模块只调这里的函数,不直接碰 SQLAlchemy,保持模块边界(docs/02 §2)。
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select, update

from .db import session
from .models import Audio, Episode, Plan, Question, Script, Topic
from .schemas import PlanRec


async def upsert_plan(plan: PlanRec) -> None:
    async with session() as s:
        topic_id = await s.scalar(
            select(Topic.id).where(Topic.title == plan.user_topic, Topic.direction == plan.direction)
        )
        if topic_id is None:
            t = Topic(title=plan.user_topic, direction=plan.direction)
            s.add(t)
            await s.flush()
            topic_id = t.id

        plan_id = await s.scalar(select(Plan.id).where(Plan.ext_id == plan.plan_id))
        if plan_id is None:
            p = Plan(ext_id=plan.plan_id, topic_id=topic_id, title=plan.title,
                     status="active", episode_count=len(plan.episodes))
            s.add(p)
            await s.flush()
            plan_id = p.id

        for ep in plan.episodes:
            exists = await s.scalar(select(Episode.id).where(Episode.ext_id == ep.episode_id))
            if exists is None:
                s.add(Episode(
                    ext_id=ep.episode_id, plan_id=plan_id, seq=ep.seq, title=ep.title,
                    outline=ep.outline, objectives=json.dumps(ep.objectives, ensure_ascii=False),
                    script_format=ep.script_format, status=ep.status,
                ))
            else:
                await s.execute(
                    update(Episode).where(Episode.ext_id == ep.episode_id)
                    .values(status=ep.status, title=ep.title, script_format=ep.script_format)
                )
        await s.commit()


async def upsert_script(ep_ext_id: str, data: dict) -> None:
    """脚本 + 题库(docs/05 M2-5:question 随脚本一次产出,mastery=unknown)。"""
    async with session() as s:
        ep_id = await s.scalar(select(Episode.id).where(Episode.ext_id == ep_ext_id))
        if ep_id is None:
            return  # plan 未入库(纯文件态运行),跳过双写
        exists = await s.scalar(select(Script.id).where(Script.episode_id == ep_id))
        if exists is None:
            s.add(Script(episode_id=ep_id, content_md=data["content"],
                         knowledge=json.dumps(data.get("knowledge", []), ensure_ascii=False),
                         token_usage=data.get("usage_tokens", 0)))
        else:
            await s.execute(
                update(Script).where(Script.episode_id == ep_id)
                .values(content_md=data["content"],
                        knowledge=json.dumps(data.get("knowledge", []), ensure_ascii=False),
                        token_usage=data.get("usage_tokens", 0))
            )
            # 重生成:旧题作废重插(掌握度记录随之重置,可接受——内容已变)
            from sqlalchemy import delete
            await s.execute(delete(Question).where(Question.episode_id == ep_id))
        knowledge = [str(item).strip() for item in data.get("knowledge", []) if str(item).strip()]
        default_kp = f"episode:{ep_id}"
        if knowledge:
            # 当前 LLM 题目尚未逐题携带 kp_id;同集作为一个可独立讲透的知识单元,
            # 先用首个知识点作稳定分组。旧行由 0002 回填 episode:<id>。
            default_kp = f"knowledge:{knowledge[0]}"
        for q in data.get("questions", []):
            kp_id = str(q.get("kp_id", "")).strip() or default_kp
            s.add(Question(
                episode_id=ep_id, kp_id=kp_id, qtype=q.get("qtype", "concept"),
                question=q["question"],
                framework=json.dumps(q.get("framework", []), ensure_ascii=False),
                difficulty=q.get("difficulty", "medium"),
            ))
        await s.execute(
            # cancelled 是集级终态:取消时在途的 script/tts job 完成后不得覆盖,
            # 否则该集卡在 scripted 等一个已被拦掉的链式 tts(幽灵"写脚本中")
            update(Episode).where(Episode.id == ep_id, Episode.status != "cancelled")
            .values(status="scripted")
        )
        await s.commit()


async def upsert_audio(ep_ext_id: str, meta: dict, voice: str) -> None:
    async with session() as s:
        ep_id = await s.scalar(select(Episode.id).where(Episode.ext_id == ep_ext_id))
        if ep_id is None:
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
        await s.execute(
            update(Episode).where(Episode.id == ep_id, Episode.status != "cancelled")
            .values(status="ready")
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


async def set_plan_status(plan_row_id: int, status: str) -> None:
    """计划状态流转(active ↔ cancelled);plan.status 此前写入即冻结,这里补上更新入口。"""
    async with session() as s:
        await s.execute(update(Plan).where(Plan.id == plan_row_id).values(status=status))
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

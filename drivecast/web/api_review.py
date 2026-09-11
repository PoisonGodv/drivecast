"""题库复习 API(docs/01 §5.3 + docs/05 M5)。"""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..library import service as library_service

router = APIRouter(prefix="/api", tags=["review"])


class GradeBody(BaseModel):
    mastery: Literal["unknown", "fuzzy", "mastered"]


class GradeOut(BaseModel):
    id: int
    mastery: str
    last_reviewed_at: str
    next_review_at: str
    review_count: int
    replay: dict | None = None


class InterviewDateBody(BaseModel):
    interview_date: date | None


@router.get("/review/daily")
async def review_daily() -> dict:
    """今日学习视图:今天排期的新集 + 复习题到期/逾期/未排期统计。"""
    return await library_service.daily_plan()


@router.get("/review/coverage")
async def review_coverage() -> dict:
    """题库全局覆盖视角:各方向覆盖进度与未覆盖高频题清单。"""
    return await library_service.coverage()


@router.get("/review/today")
async def review_today(
    mastery: Literal["unknown", "fuzzy", "mastered"] | None = Query(
        None, description="按掌握度过滤"
    ),
    direction: str | None = Query(None, description="按内容方向过滤"),
) -> list[dict]:
    """今日待复习题:未排期题优先,随后按到期时间排序。"""
    return await library_service.today_review(mastery=mastery, direction=direction)


@router.get("/review/questions")
async def list_questions(
    mastery: Literal["unknown", "fuzzy", "mastered"] | None = Query(
        None, description="按掌握度过滤"
    ),
    direction: str | None = Query(None, description="按内容方向过滤"),
) -> list[dict]:
    """题库筛选:按 mastery/direction。"""
    return await library_service.list_questions(mastery=mastery, direction=direction)


@router.post("/questions/{question_id}/grade", response_model=GradeOut)
async def grade_question(question_id: int, body: GradeBody) -> dict:
    """三态可直接流转;标不会回退 unknown,并联动次日重听队列。"""
    try:
        return await library_service.grade_question(question_id, body.mastery)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": 404, "message": str(exc)}) from exc


@router.get("/review/weak-points")
async def weak_points() -> list[dict]:
    """列出至少两道薄弱题的知识点及最短相关集推荐。"""
    return await library_service.weak_points()


@router.get("/review/replay-queue")
async def replay_queue(
    for_date: date | None = Query(None, description="播放日期;默认今天"),
    refresh: bool = Query(True, description="读取前先按当前薄弱题同步明日队列"),
) -> list[dict]:
    """持久化重听队列;默认优先返回今日到期任务,今天为空时预览明日。"""
    if refresh and for_date is None:
        return await library_service.sync_replay_queue()
    return await library_service.replay_queue(for_date)


@router.get("/review/interview-date")
async def get_interview_date() -> dict:
    interview_date = await library_service.get_interview_date()
    return {"interview_date": interview_date.isoformat() if interview_date else None}


@router.put("/review/interview-date")
async def set_interview_date(body: InterviewDateBody) -> dict:
    """设置面试日;null 清除冲刺模式。"""
    return await library_service.set_interview_date(body.interview_date)


@router.get("/review/sprint")
async def sprint_list() -> dict:
    """按方向分组、按薄弱题数排序的考前三天清单。"""
    try:
        return await library_service.sprint_list()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": 409, "message": str(exc)}) from exc

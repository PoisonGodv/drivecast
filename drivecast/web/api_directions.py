"""内容方向列表 API:创建页据此展示可用/实验/不可用方向。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..content import directions as content_directions
from ..schemas import DIRECTION_NAMES

router = APIRouter(prefix="/api", tags=["directions"])


@router.get("/directions")
async def list_directions() -> list[dict]:
    return content_directions.list_directions()


def ensure_direction_available(direction: str) -> None:
    reason = content_directions.unavailable_reason(direction)
    if reason is not None:
        label = DIRECTION_NAMES.get(direction, direction)
        raise HTTPException(
            status_code=409,
            detail={"code": 409, "message": f"内容方向「{label}」暂不可用:{reason}"},
        )

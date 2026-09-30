"""内容方向可用性:按可靠性资产(事实卡/实时搜索)如实标注,不让用户误以为各方向同等可靠。"""
from __future__ import annotations

from ..config import settings
from ..schemas import DIRECTION_NAMES
from .loader import _fact_cards_dir, _load_json

NO_FACT_CARDS_REASON = "暂无事实卡，审稿缺少依据"
NO_SEARCH_REASON = "未配置实时搜索，资讯容易过时或失实，暂不可用"


def fact_card_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    dir_path = _fact_cards_dir()
    if not dir_path.exists():
        return counts
    for path in dir_path.glob("*.json"):
        data = _load_json(path)
        if isinstance(data, dict) and data.get("direction"):
            key = str(data["direction"])
            counts[key] = counts.get(key, 0) + 1
    return counts


def search_configured() -> bool:
    return bool(settings.ref_search_provider.strip() and settings.ref_search_api_key.strip())


def list_directions() -> list[dict]:
    counts = fact_card_counts()
    out = []
    for key, label in DIRECTION_NAMES.items():
        available, experimental, reason = True, False, ""
        # tech_news 没有实时搜索就只能靠模型记忆,必然过时/编造,直接不可用
        if key == "tech_news" and not search_configured():
            available, reason = False, NO_SEARCH_REASON
        elif counts.get(key, 0) == 0:
            experimental, reason = True, NO_FACT_CARDS_REASON
        out.append({"key": key, "label": label, "available": available,
                    "experimental": experimental, "reason": reason})
    return out


def unavailable_reason(direction: str) -> str | None:
    """方向不可用时返回原因,可用返回 None。"""
    for d in list_directions():
        if d["key"] == direction:
            return None if d["available"] else d["reason"]
    return "未知的内容方向"

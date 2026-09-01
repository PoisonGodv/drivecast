"""内容资产加载层:题库与事实卡(同步、无 DB、无 LLM)。

契约: docs/07 §1.3
- get_question_bank(direction) 按 frequency 排序返回 questions 列表
- get_fact_cards(topic_keywords) 按关键词模糊匹配事实卡,空目录/非法 JSON 均不抛错
"""

from __future__ import annotations

import json
from pathlib import Path

_FREQ_ORDER = {"high": 0, "mid": 1, "low": 2}


def _content_dir() -> Path:
    return Path(__file__).resolve().parent


def _question_bank_dir() -> Path:
    return _content_dir() / "question_bank"


def _fact_cards_dir() -> Path:
    return _content_dir() / "fact_cards"


def _load_json(path: Path) -> dict:
    """加载 JSON,文件不存在或非法时返回空 dict(不抛错)。"""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}


def get_question_bank(direction: str) -> list[dict]:
    """按方向加载题库,按 frequency(high→mid→low)排序。

    Args:
        direction: 与 schemas.Direction 一致的键,如 backend_arch。

    Returns:
        questions 列表;文件缺失/非法时返回空列表。
    """
    path = _question_bank_dir() / f"{direction}.json"
    data = _load_json(path)
    questions = data.get("questions", [])
    if not isinstance(questions, list):
        return []
    # 保持同 frequency 内原始顺序(题号顺序),仅按 frequency 分组
    questions.sort(key=lambda q: _FREQ_ORDER.get(q.get("frequency"), 99))
    return questions


def _card_match_score(data: dict, keywords: set[str]) -> int:
    """计算事实卡与关键词的匹配分,用于按相关度排序。"""
    text_parts = [
        str(data.get("title", "")).lower(),
        str(data.get("direction", "")).lower(),
        " ".join(str(t).lower() for t in data.get("tags", [])),
        str(data.get("slug", "")).lower(),
    ]
    text = " ".join(text_parts)
    return sum(1 for kw in keywords if kw in text)


def get_fact_cards(topic_keywords: list[str]) -> list[dict]:
    """按主题关键词模糊匹配事实卡。

    匹配字段:title / direction / tags / slug(大小写不敏感)。
    空目录返回空列表,非法 JSON 跳过,均不抛错。

    Args:
        topic_keywords: 主题关键词列表,如 ["分布式锁", "redlock"]。

    Returns:
        命中的事实卡数据列表,按命中关键词数倒序。
    """
    dir_path = _fact_cards_dir()
    if not dir_path.exists():
        return []
    keywords = {kw.lower().strip() for kw in topic_keywords if str(kw).strip()}
    if not keywords:
        return []

    matched: list[tuple[int, dict]] = []
    for path in sorted(dir_path.glob("*.json")):
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        score = _card_match_score(data, keywords)
        if score > 0:
            matched.append((score, data))

    matched.sort(key=lambda item: (-item[0], str(item[1].get("title", ""))))
    return [data for _, data in matched]

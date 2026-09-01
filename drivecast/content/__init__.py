"""内容资产包:题库与事实卡(随 git 走,无 DB/LLM 依赖)。"""

from __future__ import annotations

from .loader import get_fact_cards, get_question_bank

__all__ = ["get_question_bank", "get_fact_cards"]

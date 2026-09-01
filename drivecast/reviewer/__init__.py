"""审稿模块(docs/07 §3)。"""
from __future__ import annotations

from .service import Flag, ReviewResult, ReviewerError, review_script

__all__ = ["Flag", "ReviewResult", "ReviewerError", "review_script"]

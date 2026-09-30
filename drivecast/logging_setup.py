"""标准 logging 初始化:单行格式(时间/级别/logger 名),run.py 与 CLI 入口调用。"""
from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_MARK = "_drivecast_handler"


class _StderrHandler(logging.StreamHandler):
    """每次写时取当前 sys.stderr:测试/CLI 重定向 stderr 后日志跟着走,不绑死初始化时的流。"""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def setup_logging(level: str | int | None = None) -> None:
    """幂等:重复调用只调整级别,不叠加 handler。"""
    if level is None:
        from .config import settings

        level = settings.log_level
    if isinstance(level, str):
        level = logging.getLevelName(level.strip().upper())
        if not isinstance(level, int):
            level = logging.INFO
    root = logging.getLogger()
    if not any(getattr(h, _MARK, False) for h in root.handlers):
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        setattr(handler, _MARK, True)
        root.addHandler(handler)
    root.setLevel(level)
    # 第三方库 INFO 太吵(每次请求一行),只保留告警
    for noisy in ("httpx", "httpcore", "aiosqlite", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

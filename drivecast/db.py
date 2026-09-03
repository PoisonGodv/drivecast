"""异步 DB 引擎/会话(docs/02 §1):SQLite WAL,读写不互斥,支撑边生成边播放。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .config import settings

_engine: AsyncEngine | None = None
_session: async_sessionmaker | None = None


def get_engine() -> AsyncEngine:
    global _engine, _session
    if _engine is None:
        url = f"sqlite+aiosqlite:///{settings.data_dir / 'drivecast.db'}"
        _engine = create_async_engine(url, echo=False)

        @event.listens_for(_engine.sync_engine, "connect")
        def _wal(dbapi_conn, _):  # WAL + 外键(docs/02 §1)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            # busy_timeout: WAL 下写冲突默认直接抛 OperationalError(database is locked),
            # 会穿透 worker 循环杀死消费 task;5s 等锁足够覆盖同进程短事务(docs/06 §10)。
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        _session = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


@asynccontextmanager
async def session() -> AsyncIterator:
    get_engine()
    async with _session() as s:  # type: ignore[union-attr]
        yield s


async def init_db() -> None:
    """建表并识别旧 schema；存量库必须先跑 Alembic，避免到 ORM 查询时才报错。"""
    from .models import Base
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        missing = await conn.run_sync(_missing_runtime_schema)
    if missing:
        fields = "、".join(missing)
        raise RuntimeError(
            f"数据库 schema 过旧，缺少 {fields}；请先运行 .venv/bin/alembic upgrade head"
        )


def _missing_runtime_schema(sync_conn) -> list[str]:
    """create_all 不会给旧表补列；这里给出启动期、可操作的错误提示。"""
    inspector = inspect(sync_conn)
    required = {
        "question": {"last_reviewed_at"},
        "audio": {"meta_json", "quality_status"},
    }
    missing: list[str] = []
    for table, columns in required.items():
        if not inspector.has_table(table):
            continue
        present = {column["name"] for column in inspector.get_columns(table)}
        missing.extend(f"{table}.{column}" for column in sorted(columns - present))
    return missing


async def close_db() -> None:
    global _engine, _session
    if _engine is not None:
        await _engine.dispose()
        _engine = _session = None

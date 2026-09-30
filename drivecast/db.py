"""异步 DB 引擎/会话(docs/02 §1):SQLite WAL,读写不互斥,支撑边生成边播放。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .config import settings

_engine: AsyncEngine | None = None
_session: async_sessionmaker | None = None
_engine_data_dir: Path | None = None


def get_engine() -> AsyncEngine:
    global _engine, _session, _engine_data_dir
    if _engine is not None and _engine_data_dir != settings.data_dir:
        # 数据目录切换(测试常换 tmp 目录):旧引擎指向已删除目录,继续复用会
        # 在 WAL 写入时报 disk I/O error(unlink 后 -wal 文件无处创建)。
        # 这里同步弃用,不 await dispose;旧连接由 GC 收尾,进程内无害。
        _engine = _session = None
    if _engine is None:
        _engine_data_dir = Path(settings.data_dir)
        url = f"sqlite+aiosqlite:///{_engine_data_dir / 'drivecast.db'}"
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
    """所有生产入口使用同一 Alembic 初始化，建号前先保留锁定的 admin。

    run_sync 复用当前连接执行同步迁移，不嵌套 asyncio.run，也不连接默认 data/。
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    async with get_engine().begin() as conn:
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        await conn.run_sync(_upgrade_database)


def _upgrade_database(sync_conn) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    config.attributes["connection"] = sync_conn
    command.upgrade(config, "head")
    missing = _missing_runtime_schema(sync_conn)
    if missing:
        raise RuntimeError(f"数据库 schema 不完整，缺少 {'、'.join(missing)}；请备份后人工核查迁移记录")
    ensure_admin_identity(sync_conn)


def ensure_admin_identity(sync_conn) -> None:
    """每次初始化保留默认所有者，包括已在 head 但管理员行缺失的库。"""
    check_admin_identity(sync_conn)
    sync_conn.execute(text(
        "INSERT INTO user(id, username, password_hash) VALUES (1, 'admin', '!locked') "
        "ON CONFLICT(id) DO NOTHING"
    ))


def check_admin_identity(sync_conn) -> None:
    """默认所有者 1 必须是 admin；历史冲突绝不通过重编号/重分配修复。"""
    inspector = inspect(sync_conn)
    users = {}
    if inspector.has_table("user"):
        columns = {c["name"] for c in inspector.get_columns("user")}
        if not {"id", "username", "password_hash", "created_at"} <= columns:
            raise RuntimeError("user 表结构不完整；请备份数据库并人工核查，禁止自动迁移")
        users = dict(sync_conn.exec_driver_sql("SELECT id, username FROM user").all())
        if (1 in users and users[1] != "admin") or any(
            name == "admin" and uid != 1 for uid, name in users.items()
        ):
            raise RuntimeError(
                "数据库所有者冲突：admin 必须占用保留 ID 1，但已有其他用户占用该 ID "
                "或 admin 位于其他 ID。初始化已拒绝；请备份数据库，人工审计所有 user_id、"
                "文件产物与已签发 token 后制定显式迁移方案。不要删除用户、重编号或直接修改归属。"
            )
    for table in ("topic", "plan", "job", "review_replay", "review_settings"):
        if inspector.has_table(table) and "user_id" in {
            c["name"] for c in inspector.get_columns(table)
        }:
            owners = set(sync_conn.exec_driver_sql(f"SELECT DISTINCT user_id FROM {table}").scalars())
            if owners - (set(users) | {1}):
                raise RuntimeError(
                    f"{table} 存在无法对应用户的所有者；请备份并人工核查，禁止自动重分配数据"
                )


def prepare_migration(sync_conn) -> None:
    """兼容未 stamp 的完整 create_all 库；未知/部分 schema 不猜测版本。

    只接受完整 0005 或当前 ORM 的结构，不重放会抹掉多租户归属的 0004。
    已有版本库仍走原迁移链。
    """
    from .models import Base

    check_admin_identity(sync_conn)
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names()) - {"alembic_version"}
    if not tables:
        return
    if inspector.has_table("alembic_version") and sync_conn.exec_driver_sql(
        "SELECT version_num FROM alembic_version"
    ).first():
        return
    expected = set(Base.metadata.tables) - {"user"}
    if tables not in (expected, expected | {"user"}):
        raise RuntimeError("未标记版本的数据库不是完整已知 schema；请备份并核查后显式设置正确 Alembic 基线")
    ownership = ("topic", "plan", "job", "review_replay", "review_settings")
    tenant_flags = []
    for name in sorted(tables):
        actual = {c["name"]: c for c in inspector.get_columns(name)}
        model = Base.metadata.tables[name]
        # 早于 0008 的 create_all 库没有 token_version,由迁移补列
        optional = {"token_version"} if name == "user" else set()
        required = set(model.columns.keys()) - optional
        if name in ownership:
            tenant_flags.append("user_id" in actual)
            required.discard("user_id")
        if set(actual) - {"user_id"} - optional != required or any(
            actual[c.name]["type"]._type_affinity != c.type._type_affinity
            or (not c.nullable and not c.primary_key and actual[c.name]["nullable"])
            for c in model.columns if c.name in required
        ) or inspector.get_pk_constraint(name)["constrained_columns"] != ["id"]:
            raise RuntimeError(f"未标记版本的 {name} 表不符合完整已知 schema；请备份并人工核查迁移基线")
        unique = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(name)}
        needed = {
            "user": {("username",)}, "plan": {("ext_id",)},
            "episode": {("ext_id",), ("plan_id", "seq")},
            "script": {("episode_id",)}, "audio": {("episode_id",)},
            "review_replay": {("user_id", "kp_id", "scheduled_for")} if "user_id" in actual else {("kp_id", "scheduled_for")},
            "review_settings": {("user_id",)} if "user_id" in actual else set(),
        }.get(name, set())
        if "user_id" in actual and (
            actual["user_id"]["nullable"]
            or actual["user_id"]["type"]._type_affinity != model.c.user_id.type._type_affinity
        ):
            raise RuntimeError(f"{name}.user_id 类型或非空约束不合法；请备份并人工核查")
        if name == "review_settings" and "user_id" not in actual:
            checks = inspector.get_check_constraints(name)
            if not any("".join(c["sqltext"].lower().split()) == "id=1" for c in checks):
                raise RuntimeError("旧 review_settings 缺少已知单例约束；请人工核查迁移基线")
        if not needed <= unique:
            raise RuntimeError(f"未标记版本的 {name} 缺少必要唯一约束；请备份并人工核查迁移基线")
    if any(tenant_flags) and not all(tenant_flags):
        raise RuntimeError("未标记版本的数据库只有部分所有者列；请备份并人工核查，禁止自动猜测归属")
    sync_conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    sync_conn.exec_driver_sql("INSERT INTO alembic_version(version_num) VALUES ('0005')")


def _missing_runtime_schema(sync_conn) -> list[str]:
    """迁移版本不能代替实际 schema 校验，损坏库在启动期拒绝。"""
    inspector = inspect(sync_conn)
    required = {
        "question": {"last_reviewed_at"},
        "audio": {"meta_json", "quality_status"},
        "episode": {"scheduled_date", "covers"},
        "script": {"refs"},
        "user": {"password_hash", "token_version"},
        "topic": {"user_id"},
        "plan": {"user_id"},
        "job": {"user_id"},
        "review_replay": {"user_id"},
        "review_settings": {"user_id"},
    }
    missing: list[str] = []
    for table, columns in required.items():
        if not inspector.has_table(table):
            missing.append(table)
            continue
        present = {column["name"] for column in inspector.get_columns(table)}
        missing.extend(f"{table}.{column}" for column in sorted(columns - present))
    return missing


async def close_db() -> None:
    global _engine, _session, _engine_data_dir
    if _engine is not None:
        await _engine.dispose()
        _engine = _session = None
        _engine_data_dir = None

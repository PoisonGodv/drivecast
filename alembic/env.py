"""alembic env:同步驱动跑迁移(运行时用 aiosqlite,互不影响)。"""
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, inspect, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根入 path

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 以 models.py 的 metadata 为准,支持未来的 autogenerate
from drivecast.models import Base  # noqa: E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection) -> None:
    from drivecast.db import check_admin_identity, ensure_admin_identity, prepare_migration

    # 显式事务让 SQLite DDL、基线标记及迁移一起提交/回滚，拒绝冲突时不留半成品。
    with connection.begin_nested() if connection.in_transaction() else connection.begin():
        prepare_migration(connection)
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        check_admin_identity(connection)
        if inspect(connection).has_table("user"):
            ensure_admin_identity(connection)


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _migrate(connection)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        with connectable.connect() as connection:
            # Python sqlite3 legacy 模式的 begin() 不会在 DDL 前发 BEGIN。
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            _migrate(connection)
            connection.commit()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

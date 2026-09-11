"""排期与参考链接:episode 排期日/覆盖题集,script 参考链接。

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-11
"""
from alembic import op
from sqlalchemy import inspect

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # create_all 可能先在旧库建出新列;迁移仍须可幂等补齐 Alembic 版本(同 0003 手法)。
    episode_columns = {column["name"] for column in inspect(op.get_bind()).get_columns("episode")}
    if "scheduled_date" not in episode_columns:
        op.execute("ALTER TABLE episode ADD COLUMN scheduled_date TEXT")
    if "covers" not in episode_columns:
        op.execute("ALTER TABLE episode ADD COLUMN covers TEXT NOT NULL DEFAULT '[]'")

    script_columns = {column["name"] for column in inspect(op.get_bind()).get_columns("script")}
    if "refs" not in script_columns:
        op.execute("ALTER TABLE script ADD COLUMN refs TEXT NOT NULL DEFAULT '[]'")


def downgrade() -> None:
    # SQLite 3.35+ 支持 DROP COLUMN;降级只用于开发回滚。
    episode_columns = {column["name"] for column in inspect(op.get_bind()).get_columns("episode")}
    if "covers" in episode_columns:
        op.execute("ALTER TABLE episode DROP COLUMN covers")
    if "scheduled_date" in episode_columns:
        op.execute("ALTER TABLE episode DROP COLUMN scheduled_date")

    script_columns = {column["name"] for column in inspect(op.get_bind()).get_columns("script")}
    if "refs" in script_columns:
        op.execute("ALTER TABLE script DROP COLUMN refs")

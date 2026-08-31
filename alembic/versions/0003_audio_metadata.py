"""M6 音频元数据与质检字段

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-20
"""
from alembic import op
from sqlalchemy import inspect

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("audio")}
    # create_all 可能先在旧库建出新列；迁移仍须可幂等补齐 Alembic 版本。
    if "meta_json" not in columns:
        op.execute("ALTER TABLE audio ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
    if "quality_status" not in columns:
        op.execute("ALTER TABLE audio ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'unknown'")


def downgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("audio")}
    # SQLite 新版本支持 DROP COLUMN；本项目本地环境由 ffmpeg/Python 同机维护。
    if "quality_status" in columns:
        op.execute("ALTER TABLE audio DROP COLUMN quality_status")
    if "meta_json" in columns:
        op.execute("ALTER TABLE audio DROP COLUMN meta_json")

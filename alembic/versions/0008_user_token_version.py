"""user.token_version:JWT 吊销版本号,改密/重置密码时自增使旧 token 失效。

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30

create_all 可能已按新 ORM 建出该列,inspect 判断到位即跳过(同 0005 手法)。
"""
from alembic import op
from sqlalchemy import inspect

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns("user")}


def upgrade() -> None:
    if "token_version" not in _columns():
        op.execute("ALTER TABLE user ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    # SQLite 3.35+ 支持 DROP COLUMN;降级只用于开发回滚。
    if "token_version" in _columns():
        op.execute("ALTER TABLE user DROP COLUMN token_version")

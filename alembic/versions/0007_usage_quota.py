"""用量记账:usage_event 表(每日额度本身按 job 表现算,不落表)。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30

幂等:create_all 已建出该表的库直接跳过。
"""
from alembic import op
from sqlalchemy import inspect

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not inspect(op.get_bind()).has_table("usage_event"):
        op.execute("""
            CREATE TABLE usage_event (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id    INTEGER NOT NULL,
              kind       TEXT NOT NULL,
              amount     INTEGER NOT NULL DEFAULT 0,
              ref_id     TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_event_user_day ON usage_event(user_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_usage_event_user_day")
    op.execute("DROP TABLE IF EXISTS usage_event")

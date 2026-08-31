"""M5 复习闭环:复习时间、知识点归一、次日重听队列与面试日。

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-20
"""
from alembic import op
from sqlalchemy import inspect

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    question_columns = {column["name"] for column in inspector.get_columns("question")}

    # SQLite ALTER TABLE ADD COLUMN 对存量行安全;历史已复习行用 next_review 反推不可靠,
    # 因此只补字段,后续一次真实评分会写入准确时间。
    if "last_reviewed_at" not in question_columns:
        op.execute("ALTER TABLE question ADD COLUMN last_reviewed_at TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_question_kp_mastery "
        "ON question(kp_id, mastery)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS review_replay (
          id                  INTEGER PRIMARY KEY AUTOINCREMENT,
          kp_id               TEXT NOT NULL,
          episode_id          INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
          scheduled_for       TEXT NOT NULL,
          weak_question_count INTEGER NOT NULL,
          status              TEXT NOT NULL DEFAULT 'pending',
          created_at          TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(kp_id, scheduled_for)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_replay_due "
        "ON review_replay(scheduled_for, status)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS review_settings (
          id             INTEGER PRIMARY KEY CHECK (id = 1),
          interview_date TEXT,
          updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    op.execute("INSERT OR IGNORE INTO review_settings(id) VALUES (1)")

    # M2 题目未携带 kp_id。按来源集归一成稳定知识点,让历史题立即可聚合;
    # 同集三道自测题本就覆盖同一可独立讲透的知识单元。
    op.execute("""
        UPDATE question
        SET kp_id = 'episode:' || episode_id
        WHERE trim(coalesce(kp_id, '')) = ''
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS review_settings")
    op.execute("DROP INDEX IF EXISTS idx_review_replay_due")
    op.execute("DROP TABLE IF EXISTS review_replay")
    op.execute("DROP INDEX IF EXISTS idx_question_kp_mastery")

    # SQLite 3.35+ 支持 DROP COLUMN;项目目标环境满足。降级只用于开发回滚。
    op.execute("ALTER TABLE question DROP COLUMN last_reviewed_at")

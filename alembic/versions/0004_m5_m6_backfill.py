"""M5/M6 存量 schema 修复与音频升级队列。

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-20
"""
from alembic import op
from sqlalchemy import inspect

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


_REPLAY_DDL = """
CREATE TABLE review_replay__fixed (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  kp_id               TEXT NOT NULL,
  episode_id          INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
  scheduled_for       TEXT NOT NULL,
  weak_question_count INTEGER NOT NULL,
  status              TEXT NOT NULL DEFAULT 'pending',
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(kp_id, scheduled_for)
)
"""

_SETTINGS_DDL = """
CREATE TABLE review_settings__fixed (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  interview_date TEXT,
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _default_sql(table: str, column: str) -> str:
    for item in inspect(op.get_bind()).get_columns(table):
        if item["name"] == column:
            return str(item.get("default") or "")
    return ""


def _table_sql(table: str) -> str:
    row = op.get_bind().exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).first()
    return str(row[0] or "") if row else ""


def _repair_review_replay() -> None:
    if not inspect(op.get_bind()).has_table("review_replay"):
        op.execute(_REPLAY_DDL.replace("review_replay__fixed", "review_replay"))
        return
    status_default = _default_sql("review_replay", "status").strip("()'\"").lower()
    created_default = _default_sql("review_replay", "created_at").lower()
    if status_default == "pending" and "datetime" in created_default and "'(datetime" not in created_default:
        op.execute("""
            UPDATE review_replay
            SET status = coalesce(nullif(status, ''), 'pending'),
                created_at = CASE
                  WHEN created_at IS NULL OR created_at LIKE '%datetime(%' THEN datetime('now')
                  ELSE created_at
                END
        """)
        return
    op.execute("DROP INDEX IF EXISTS idx_review_replay_due")
    op.execute("DROP TABLE IF EXISTS review_replay__fixed")
    op.execute(_REPLAY_DDL)
    op.execute("""
        INSERT INTO review_replay__fixed(
          id, kp_id, episode_id, scheduled_for, weak_question_count, status, created_at
        )
        SELECT id, kp_id, episode_id, scheduled_for, weak_question_count,
               coalesce(nullif(status, ''), 'pending'),
               CASE
                 WHEN created_at IS NULL OR created_at LIKE '%datetime(%' THEN datetime('now')
                 ELSE created_at
               END
        FROM review_replay
    """)
    op.execute("DROP TABLE review_replay")
    op.execute("ALTER TABLE review_replay__fixed RENAME TO review_replay")
    op.execute(
        "CREATE INDEX idx_review_replay_due ON review_replay(scheduled_for, status)"
    )


def _repair_review_settings() -> None:
    if not inspect(op.get_bind()).has_table("review_settings"):
        op.execute(_SETTINGS_DDL.replace("review_settings__fixed", "review_settings"))
        op.execute("INSERT INTO review_settings(id) VALUES (1)")
        return
    created_default = _default_sql("review_settings", "updated_at").lower()
    has_singleton_check = "check (id = 1)" in _table_sql("review_settings").lower()
    if has_singleton_check and "datetime" in created_default and "'(datetime" not in created_default:
        op.execute("""
            UPDATE review_settings
            SET updated_at = CASE
              WHEN updated_at IS NULL OR updated_at LIKE '%datetime(%' THEN datetime('now')
              ELSE updated_at
            END
            WHERE id = 1
        """)
        return
    op.execute("DROP TABLE IF EXISTS review_settings__fixed")
    op.execute(_SETTINGS_DDL)
    op.execute("""
        INSERT INTO review_settings__fixed(id, interview_date, updated_at)
        SELECT 1, interview_date,
               CASE
                 WHEN updated_at IS NULL OR updated_at LIKE '%datetime(%' THEN datetime('now')
                 ELSE updated_at
               END
        FROM review_settings
        WHERE id = 1
    """)
    op.execute("INSERT OR IGNORE INTO review_settings__fixed(id) VALUES (1)")
    op.execute("DROP TABLE review_settings")
    op.execute("ALTER TABLE review_settings__fixed RENAME TO review_settings")


def upgrade() -> None:
    # create_all 在 0002 前运行时会按 ORM 先建表；这里统一重建成迁移定义。
    _repair_review_replay()
    _repair_review_settings()

    # 历史 review_count 已有值但时间为空时，用既有排期提供可解释的审计时间。
    op.execute("""
        UPDATE question
        SET last_reviewed_at = next_review_at
        WHERE review_count > 0
          AND last_reviewed_at IS NULL
          AND next_review_at IS NOT NULL
    """)

    # 已完成但尚未经过 M6 后处理的本地成品，只入本地 upgrade job；worker 不调 LLM/TTS。
    op.execute("""
        INSERT INTO job(kind, ref_id, status, payload)
        SELECT 'audio_upgrade', e.ext_id, 'pending',
               json_object('plan_id', p.ext_id, 'seq', e.seq)
        FROM episode e
        JOIN plan p ON p.id = e.plan_id
        JOIN audio a ON a.episode_id = e.id
        WHERE e.status = 'ready'
          AND length(trim(coalesce(a.file_path, ''))) > 0
          AND NOT EXISTS (
            SELECT 1 FROM job j
            WHERE j.kind = 'audio_upgrade'
              AND j.ref_id = e.ext_id
              AND j.status IN ('pending', 'running', 'done')
          )
          AND (
            coalesce(a.meta_json, '{}') = '{}'
            OR CASE
                 WHEN json_valid(coalesce(a.meta_json, '{}'))
                 THEN coalesce(json_extract(a.meta_json, '$.pipeline_version'), 0) < 7
                 ELSE 1
               END
          )
    """)


def downgrade() -> None:
    op.execute("DELETE FROM job WHERE kind = 'audio_upgrade' AND status = 'pending'")

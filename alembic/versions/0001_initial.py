"""0001 初始七表(docs/02 §3 + ext_id 文件态映射)

Revision ID: 0001
Revises:
Create Date: 2026-08-20
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE topic (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT NOT NULL,
  direction     TEXT NOT NULL,
  depth         TEXT NOT NULL DEFAULT 'senior',
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE plan (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ext_id        TEXT NOT NULL UNIQUE,
  topic_id      INTEGER NOT NULL REFERENCES topic(id),
  title         TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'draft',
  episode_count INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE episode (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ext_id        TEXT NOT NULL UNIQUE,
  plan_id       INTEGER NOT NULL REFERENCES plan(id),
  seq           INTEGER NOT NULL,
  title         TEXT NOT NULL,
  outline       TEXT NOT NULL DEFAULT '',
  objectives    TEXT NOT NULL DEFAULT '',
  script_format TEXT NOT NULL DEFAULT 'monologue',
  status        TEXT NOT NULL DEFAULT 'pending',
  position_sec  INTEGER NOT NULL DEFAULT 0,
  listen_status TEXT NOT NULL DEFAULT 'unlistened',
  UNIQUE(plan_id, seq)
);
CREATE INDEX idx_episode_plan ON episode(plan_id, seq);

CREATE TABLE script (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id    INTEGER NOT NULL UNIQUE REFERENCES episode(id),
  content_md    TEXT NOT NULL,
  knowledge     TEXT NOT NULL DEFAULT '',
  token_usage   INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE audio (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id    INTEGER NOT NULL UNIQUE REFERENCES episode(id),
  file_path     TEXT NOT NULL,
  voice         TEXT NOT NULL DEFAULT '',
  duration_sec  INTEGER NOT NULL DEFAULT 0,
  size_bytes    INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'done',
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE question (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id    INTEGER NOT NULL REFERENCES episode(id),
  kp_id         TEXT NOT NULL DEFAULT '',
  qtype         TEXT NOT NULL DEFAULT 'concept',
  question      TEXT NOT NULL,
  framework     TEXT NOT NULL DEFAULT '',
  difficulty    TEXT NOT NULL DEFAULT 'medium',
  mastery       TEXT NOT NULL DEFAULT 'unknown',
  review_count  INTEGER NOT NULL DEFAULT 0,
  next_review_at TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_question_review ON question(next_review_at);

CREATE TABLE job (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL,
  ref_id        TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',
  attempt       INTEGER NOT NULL DEFAULT 0,
  max_attempt   INTEGER NOT NULL DEFAULT 3,
  error         TEXT NOT NULL DEFAULT '',
  payload       TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_job_status ON job(status, id);
"""


def upgrade() -> None:
    for stmt in DDL.split(";"):
        stmt = stmt.strip()
        if stmt:
            op.execute(stmt)


def downgrade() -> None:
    for table in ("job", "question", "audio", "script", "episode", "plan", "topic"):
        op.execute(f"DROP TABLE IF EXISTS {table}")

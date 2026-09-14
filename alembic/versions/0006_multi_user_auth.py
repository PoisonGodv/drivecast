"""多用户认证:user 表、topic/plan/job 加 user_id、复习表按用户隔离重建。

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-14

场景与幂等性(沿用 0004/0005 的 inspect 手法):
- 老库(data/drivecast.db @0005):建 user 表、admin 占位 '!locked'、加列回填 admin_id、
  重建 review_replay(UNIQUE 加 user_id,kp_id 是全局文本命名空间必须按用户隔离)与
  review_settings(去 CHECK(id=1) 单例,改 user_id 唯一)。
- 全新库走 alembic 建表:0001-0002 建出的旧结构表在此统一升级。
- create_all 先按新 ORM 建过表的库:各步 inspect 判断已到位即跳过。

迁移后必须跑 `python -m drivecast reset-password admin` 给占位哈希后置真实密码。
"""
from alembic import op
from sqlalchemy import inspect

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def _has_table(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def _unique_columns(table: str) -> list[set[str]]:
    return [set(uc["column_names"]) for uc in inspect(op.get_bind()).get_unique_constraints(table)]


_REPLAY_FINAL_DDL = """
CREATE TABLE review_replay__new (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id             INTEGER NOT NULL DEFAULT 1,
  kp_id               TEXT NOT NULL,
  episode_id          INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
  scheduled_for       TEXT NOT NULL,
  weak_question_count INTEGER NOT NULL,
  status              TEXT NOT NULL DEFAULT 'pending',
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(user_id, kp_id, scheduled_for)
)
"""

_SETTINGS_FINAL_DDL = """
CREATE TABLE review_settings__new (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        INTEGER NOT NULL UNIQUE,
  interview_date TEXT,
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_REPLAY_OLD_DDL = """
CREATE TABLE review_replay__old (
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

_SETTINGS_OLD_DDL = """
CREATE TABLE review_settings__old (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  interview_date TEXT,
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _admin_id() -> int:
    return int(
        op.get_bind()
        .exec_driver_sql("SELECT id FROM user WHERE username = 'admin'")
        .scalar_one()
    )


def upgrade() -> None:
    # STEP1 用户表(无注册接口,用户仅来自 CLI create-user)
    op.execute("""
        CREATE TABLE IF NOT EXISTS user (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          username      TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # STEP2 admin 占位:'!locked' 在 verify_password 里恒 False,迁移完即可安全启动
    from drivecast.db import check_admin_identity

    check_admin_identity(op.get_bind())
    # 显式保留 ID，而非依赖自增序列；历史冲突在任何写入之前拒绝。
    op.execute("INSERT OR IGNORE INTO user(id, username, password_hash) VALUES (1, 'admin', '!locked')")
    admin_id = _admin_id()

    # STEP3 业务表加 user_id(常量 DEFAULT 兜底存量,SQLite ALTER 免重建免回填)
    for table in ("topic", "plan", "job"):
        if "user_id" not in _columns(table):
            op.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT {admin_id}")
    op.execute("CREATE INDEX IF NOT EXISTS idx_topic_user ON topic(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_plan_user ON plan(user_id)")
    # job 按主键查,不建索引

    # STEP4 重建 review_replay:UNIQUE(kp_id, scheduled_for) → UNIQUE(user_id, kp_id, scheduled_for)
    if not _has_table("review_replay"):
        op.execute(_REPLAY_FINAL_DDL.replace("review_replay__new", "review_replay"))
    elif "user_id" in _columns("review_replay") and {
        "user_id", "kp_id", "scheduled_for"
    } in _unique_columns("review_replay"):
        pass  # create_all 已按新 ORM 建好,幂等跳过
    else:
        op.execute("DROP INDEX IF EXISTS idx_review_replay_due")
        op.execute("DROP TABLE IF EXISTS review_replay__new")
        op.execute(_REPLAY_FINAL_DDL)
        owner_column = "user_id" if "user_id" in _columns("review_replay") else str(admin_id)
        op.execute(f"""
            INSERT INTO review_replay__new(
              id, user_id, kp_id, episode_id, scheduled_for, weak_question_count, status, created_at
            )
            SELECT id, {owner_column}, kp_id, episode_id, scheduled_for, weak_question_count,
                   coalesce(nullif(status, ''), 'pending'), created_at
            FROM review_replay
        """)
        op.execute("DROP TABLE review_replay")
        op.execute("ALTER TABLE review_replay__new RENAME TO review_replay")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_replay_due ON review_replay(scheduled_for, status)"
    )

    # STEP5 重建 review_settings:CHECK(id=1) 单例 → user_id 唯一;旧 id=1 行迁给 admin
    if not _has_table("review_settings"):
        op.execute(_SETTINGS_FINAL_DDL.replace("review_settings__new", "review_settings"))
    elif "user_id" in _columns("review_settings"):
        pass  # 已是新结构
    else:
        op.execute("DROP TABLE IF EXISTS review_settings__new")
        op.execute(_SETTINGS_FINAL_DDL)
        op.execute(f"""
            INSERT OR IGNORE INTO review_settings__new(user_id, interview_date, updated_at)
            SELECT {admin_id}, interview_date,
                   CASE
                     WHEN updated_at IS NULL OR updated_at LIKE '%datetime(%' THEN datetime('now')
                     ELSE updated_at
                   END
            FROM review_settings
            WHERE id = 1
        """)
        op.execute("DROP TABLE review_settings")
        op.execute("ALTER TABLE review_settings__new RENAME TO review_settings")


def downgrade() -> None:
    # 反向:恢复单用户 admin 形态。admin 捕获须在 DROP user 之前。
    if _has_table("user"):
        admin_id = _admin_id()
    else:
        admin_id = 1

    # review_settings 回插 id=1 单例(admin 的 interview_date 带走)
    if _has_table("review_settings"):
        op.execute("DROP TABLE IF EXISTS review_settings__old")
        op.execute(_SETTINGS_OLD_DDL)
        op.execute(f"""
            INSERT OR IGNORE INTO review_settings__old(id, interview_date, updated_at)
            SELECT 1, interview_date,
                   CASE
                     WHEN updated_at IS NULL OR updated_at LIKE '%datetime(%' THEN datetime('now')
                     ELSE updated_at
                   END
            FROM review_settings
            WHERE user_id = {admin_id}
        """)
        op.execute("DROP TABLE review_settings")
        op.execute("ALTER TABLE review_settings__old RENAME TO review_settings")

    # review_replay 回旧 UNIQUE;跨用户同 kp_id 同日按 id 序保留先到者(开发回滚用途)
    if _has_table("review_replay"):
        op.execute("DROP INDEX IF EXISTS idx_review_replay_due")
        op.execute("DROP TABLE IF EXISTS review_replay__old")
        op.execute(_REPLAY_OLD_DDL)
        op.execute("""
            INSERT OR IGNORE INTO review_replay__old(
              kp_id, episode_id, scheduled_for, weak_question_count, status, created_at
            )
            SELECT kp_id, episode_id, scheduled_for, weak_question_count, status, created_at
            FROM review_replay
            ORDER BY id
        """)
        op.execute("DROP TABLE review_replay")
        op.execute("ALTER TABLE review_replay__old RENAME TO review_replay")
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_replay_due ON review_replay(scheduled_for, status)"
        )

    # 业务表去 user_id 列(SQLite 3.35+;列上有索引须先删,否则 DROP COLUMN 拒绝)
    op.execute("DROP INDEX IF EXISTS idx_topic_user")
    op.execute("DROP INDEX IF EXISTS idx_plan_user")
    for table in ("job", "plan", "topic"):
        if "user_id" in _columns(table):
            op.execute(f"ALTER TABLE {table} DROP COLUMN user_id")

    op.execute("DROP TABLE IF EXISTS user")

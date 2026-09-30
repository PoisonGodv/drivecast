"""SQLAlchemy 2.0 表定义(docs/02 §3 七表)。

与文件产物的映射:plan/episode 增加 ext_id(文件态 plan_id/episode_id 字符串),
文件仍是过程态,DB 是 Web 查询态(docs/02 §2)。
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True, autoincrement=True)


def _now() -> Mapped[str]:
    return mapped_column(Text, nullable=False, server_default=text("(datetime('now'))"))


class User(Base):
    """登录用户(多租户根)。密码哈希格式见 drivecast/auth.py;无注册接口,仅 CLI 建户。"""

    __tablename__ = "user"
    id: Mapped[int] = _id()
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = _now()


class Topic(Base):
    __tablename__ = "topic"
    __table_args__ = (Index("idx_topic_user", "user_id"),)
    id: Mapped[int] = _id()
    # user_id ORM 默认 1=迁移约定的 admin(0006 第一个 INSERT 保证);测试/CLI 种子免传
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[str] = mapped_column(Text, nullable=False, default="senior")
    created_at: Mapped[str] = _now()


class Plan(Base):
    __tablename__ = "plan"
    __table_args__ = (Index("idx_plan_user", "user_id"),)
    id: Mapped[int] = _id()
    # user_id ORM 默认 1=迁移约定的 admin;测试/CLI 种子免传
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ext_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # plan_20260820_01
    topic_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    episode_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = _now()


class Episode(Base):
    __tablename__ = "episode"
    __table_args__ = (
        UniqueConstraint("plan_id", "seq"),
        Index("idx_episode_plan", "plan_id", "seq"),
    )
    id: Mapped[int] = _id()
    ext_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # ep_20260820_01_01
    plan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    outline: Mapped[str] = mapped_column(Text, nullable=False, default="")
    objectives: Mapped[str] = mapped_column(Text, nullable=False, default="")
    script_format: Mapped[str] = mapped_column(Text, nullable=False, default="monologue")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    position_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    listen_status: Mapped[str] = mapped_column(Text, nullable=False, default="unlistened")
    scheduled_date: Mapped[str | None] = mapped_column(Text, nullable=True)  # 排期日 YYYY-MM-DD
    covers: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # 覆盖题库题 id JSON 数组


class Script(Base):
    __tablename__ = "script"
    id: Mapped[int] = _id()
    episode_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge: Mapped[str] = mapped_column(Text, nullable=False, default="")
    refs: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # 参考链接 JSON:[{title,url,note}]
    token_usage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = _now()


class Audio(Base):
    __tablename__ = "audio"
    id: Mapped[int] = _id()
    episode_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    voice: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    quality_status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="done")
    created_at: Mapped[str] = _now()


class Question(Base):
    __tablename__ = "question"
    __table_args__ = (
        Index("idx_question_review", "next_review_at"),
        Index("idx_question_kp_mastery", "kp_id", "mastery"),
    )
    id: Mapped[int] = _id()
    episode_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kp_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    qtype: Mapped[str] = mapped_column(Text, nullable=False, default="concept")
    question: Mapped[str] = mapped_column(Text, nullable=False)
    framework: Mapped[str] = mapped_column(Text, nullable=False, default="")
    difficulty: Mapped[str] = mapped_column(Text, nullable=False, default="medium")
    mastery: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reviewed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_review_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = _now()


class ReviewReplay(Base):
    """薄弱知识点触发的次日重听队列;同一用户同一天同一知识点只保留一条。

    kp_id 形如 'knowledge:MySQL 索引' 是全局文本命名空间,唯一约束必须带 user_id,
    否则两用户同主题生成会互相顶掉对方的重听任务。
    """

    __tablename__ = "review_replay"
    __table_args__ = (
        UniqueConstraint("user_id", "kp_id", "scheduled_for"),
        Index("idx_review_replay_due", "scheduled_for", "status"),
    )
    id: Mapped[int] = _id()
    # server_default 1:兼容迁移前 create_all 建出的表上的裸 SQL 插入(scripts/存量测试)
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    kp_id: Mapped[str] = mapped_column(Text, nullable=False)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episode.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_for: Mapped[str] = mapped_column(Text, nullable=False)
    weak_question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default=text("'pending'")
    )
    created_at: Mapped[str] = _now()


class ReviewSettings(Base):
    """复习设置(按用户一行;旧版 CHECK(id=1) 单例已由 0006 重建为 user_id 唯一)。"""

    __tablename__ = "review_settings"
    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, default=1)  # 默认 1=admin
    interview_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = _now()


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (Index("idx_job_status", "status", "id"),)
    id: Mapped[int] = _id()
    # user_id 用途:GET /api/jobs/{id} 按用户 404;worker 链式入队从父 job 继承。
    # 按主键查、不建索引。ORM 默认 1=admin(迁移约定),测试/CLI 种子免传。
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(Text, nullable=False)      # script/tts
    ref_id: Mapped[str] = mapped_column(Text, nullable=False)    # episode ext_id
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = _now()
    updated_at: Mapped[str] = _now()


class UsageEvent(Base):
    """用量流水(成本观察):kind=llm_tokens/tts_chars,ref_id 为集 ext_id。"""

    __tablename__ = "usage_event"
    __table_args__ = (Index("idx_usage_event_user_day", "user_id", "created_at"),)
    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ref_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = _now()

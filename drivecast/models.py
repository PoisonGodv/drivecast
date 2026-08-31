"""SQLAlchemy 2.0 表定义(docs/02 §3 七表)。

与文件产物的映射:plan/episode 增加 ext_id(文件态 plan_id/episode_id 字符串),
文件仍是过程态,DB 是 Web 查询态(docs/02 §2)。
"""
from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True, autoincrement=True)


def _now() -> Mapped[str]:
    return mapped_column(Text, nullable=False, server_default=text("(datetime('now'))"))


class Topic(Base):
    __tablename__ = "topic"
    id: Mapped[int] = _id()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[str] = mapped_column(Text, nullable=False, default="senior")
    created_at: Mapped[str] = _now()


class Plan(Base):
    __tablename__ = "plan"
    id: Mapped[int] = _id()
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


class Script(Base):
    __tablename__ = "script"
    id: Mapped[int] = _id()
    episode_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge: Mapped[str] = mapped_column(Text, nullable=False, default="")
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
    """薄弱知识点触发的次日重听队列;同一天同一知识点只保留一条。"""

    __tablename__ = "review_replay"
    __table_args__ = (
        UniqueConstraint("kp_id", "scheduled_for"),
        Index("idx_review_replay_due", "scheduled_for", "status"),
    )
    id: Mapped[int] = _id()
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
    """单用户复习设置;固定 id=1,避免把全局状态塞进 JSON。"""

    __tablename__ = "review_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_review_settings_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    interview_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = _now()


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (Index("idx_job_status", "status", "id"),)
    id: Mapped[int] = _id()
    kind: Mapped[str] = mapped_column(Text, nullable=False)      # script/tts
    ref_id: Mapped[str] = mapped_column(Text, nullable=False)    # episode ext_id
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = _now()
    updated_at: Mapped[str] = _now()

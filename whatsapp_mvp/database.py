# WhatsApp MVP - Database Models (SQLite via SQLAlchemy)

from __future__ import annotations

import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, relationship, Session, mapped_column

from .config import get_config


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    RECEIVED = "RECEIVED"
    COLLECTING_ASSETS = "COLLECTING_ASSETS"
    NEEDS_TARGET_CHOICE = "NEEDS_TARGET_CHOICE"
    DOWNLOADING_MEDIA = "DOWNLOADING_MEDIA"
    PLANNING = "PLANNING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    RUNNING_PIPELINE = "RUNNING_PIPELINE"
    RENDERING = "RENDERING"
    DELIVERING = "DELIVERING"
    PREVIEW_READY = "PREVIEW_READY"
    DONE = "DONE"
    ERROR = "ERROR"


class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageType(str, Enum):
    TEXT = "text"
    VIDEO = "video"
    IMAGE = "image"
    BUTTON_REPLY = "button_reply"
    INTERACTIVE = "interactive"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    whatsapp_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )
    last_active_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    jobs: Mapped[list["Job"]] = relationship(back_populates="user", lazy="selectin")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus), default=JobStatus.RECEIVED, nullable=False
    )
    pipeline: Mapped[str] = mapped_column(String(64), default="talking-head")

    # Input
    input_video_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    input_caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    whatsapp_media_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    edit_request: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # b-roll / multi-asset intake: JSON list of
    # {role: "target"|"broll", media_id, local_path, label, order}.
    # whatsapp_media_id/input_video_path above still point at the target (back-compat).
    assets: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # LLM planner output (JSON)
    planned_edit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Output
    preview_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    preview_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    final_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    final_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 非致命失败被跳过的操作（JSON list，如 ["apply_style"]）。Node 网关靠它
    # 在预览消息里如实告知用户"哪步没成、当前版本缺什么、可回复 retry"——
    # 此前这信息只活在管线返回值里，Python 侧的提醒又走的是死代码发送路径，
    # 用户从头到尾被蒙在鼓里。
    degraded_operations: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    user: Mapped["User"] = relationship(back_populates="jobs")
    messages: Mapped[list["Message"]] = relationship(back_populates="job", lazy="selectin")

    @property
    def job_dir(self) -> Path:
        config = get_config()
        return config.jobs_dir / self.id

    @property
    def input_path(self) -> Path:
        return self.job_dir / "input.mp4"

    @property
    def preview_path_local(self) -> Path:
        return self.job_dir / "preview.mp4"

    @property
    def final_path_local(self) -> Path:
        return self.job_dir / "final.mp4"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id"), nullable=False, index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        SAEnum(MessageDirection), nullable=False
    )
    message_type: Mapped[MessageType] = mapped_column(
        SAEnum(MessageType), default=MessageType.TEXT
    )
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    whatsapp_message_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, unique=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )

    job: Mapped["Job"] = relationship(back_populates="messages")


# ---------------------------------------------------------------------------
# Engine & Session
# ---------------------------------------------------------------------------

_engine = None
_SessionLocal = None


def _migrate_schema(engine) -> None:
    """轻量幂等迁移：create_all 不会给已存在的表加列，后加的可空列在这里补。
    对 sqlite / postgres 都适用（ALTER TABLE ADD COLUMN）。列已存在则 no-op。"""
    from sqlalchemy import inspect as _inspect, text as _text
    try:
        cols = {c["name"] for c in _inspect(engine).get_columns("jobs")}
    except Exception:
        return
    if "assets" not in cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE jobs ADD COLUMN assets TEXT"))
    if "degraded_operations" not in cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE jobs ADD COLUMN degraded_operations TEXT"))


def _init_engine() -> None:
    global _engine, _SessionLocal
    config = get_config()
    db_url = config.database_url

    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    _engine = create_engine(db_url, connect_args=connect_args, echo=False)
    Base.metadata.create_all(bind=_engine)
    _migrate_schema(_engine)

    from sqlalchemy.orm import sessionmaker

    _SessionLocal = sessionmaker(bind=_engine)


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _init_engine()
    return _SessionLocal()
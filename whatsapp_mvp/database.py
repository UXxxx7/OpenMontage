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
    Float,
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
    CLIPS_READY = "CLIPS_READY"  # clip-factory 管线的终态，等价于 talking-head 的 PREVIEW_READY
    DONE = "DONE"
    ERROR = "ERROR"


class ClipStatus(str, Enum):
    PENDING = "PENDING"      # selection 阶段选中，还没开始渲染
    RENDERING = "RENDERING"
    READY = "READY"
    FAILED = "FAILED"        # 真失败或者被 wall-time 预算跳过，用 error_message 区分


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

    # ElevenLabs Instant Voice Clone（voice_clone.py）——一个 WhatsApp 号一份克隆
    # 音色，注册一次、之后每次生成 C-roll/social batch 都复用，不是一次性资源。
    # 跟 heygen_croll 的 talking_photo（用完即删）性质不同：这个是长期持有的。
    elevenlabs_voice_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

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

    # AI 生成花的真金白银累计（b-roll/背景音乐等，来自 pipeline_runner 的
    # 成本账本）。Node 网关靠它在预览消息里如实告知花费，防止用户/团队
    # 完全看不到生成类操作的真实成本。
    generation_cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── 社媒批次（social_batch.py）──────────────────────────────────────
    # 一次语音+照片提交 -> 多个平台变体，每个变体是独立一条 Job（复用整套
    # 既有的 job 生命周期/文件存储/状态机），batch_id 把它们串成一组，给
    # Studio 预览页按批次查询用。非批次任务（普通视频/单条 C-roll）这三个
    # 字段一律为 None，不受影响。
    batch_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    platform: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # 见 social_batch.PLATFORM_SPECS
    social_caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    social_hashtags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON list[str]

    # 发帖配文 + hashtag（social_caption.py 生成，JSON: {lang, caption, hashtags}）。
    # 命名为 talkinghead_social_caption 而不是 social_caption——那个名字已经被
    # 上面 social_batch.py 的字段占了（纯字符串 + 独立的 social_hashtags 列，
    # 形状不一样），两边都叫 social_caption 会互相踩。每次 update_job_fields
    # 都必须显式传（哪怕是 None）——retry 会重新走到写这个字段的调用点，漏传
    # kwarg 会让上一轮的旧文案在内容可能已经变了的情况下静默留存。
    talkinghead_social_caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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


class Clip(Base):
    """clip-factory 管线的产出——一个父 Job（一次对话/一次确认/一套状态机，
    完全不动）对应 N 条 Clip。没有用 social_batch.py 那种"多条 sibling Job
    共享 batch_id"的模式：get_active_job_for_user() 和 Node 那整套会话状态机
    （activeJobKey/armIdle/waitForStatus）都硬编码假设"每个用户同一时间只有
    一条活跃 Job"，N 条并存的 sibling Job 会让 confirm/retry/cancel 解析到
    错的那条。子表 + 外键，既有的会话管线一行都不用动。"""
    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"), nullable=False, index=True)
    status: Mapped[ClipStatus] = mapped_column(SAEnum(ClipStatus), default=ClipStatus.PENDING, nullable=False)

    rank: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 = 最强，发布顺序
    # 自由字符串，不做数据库枚举——clip_factory.py 的 prompt 给了固定词表
    # （hook/insight/story/proof/opinion），但 LLM 输出偶尔会跑偏，DB 层不
    # 因为一个没见过的取值就整条写入失败。
    clip_family: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    hook_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    score_hook: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_coherence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_energy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_platform_fit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_total: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # /files/{job_id}/{filename} 这条路由（FastAPI 和 Express 两边都一样）
    # 不支持嵌套路径，所以最终产物是平铺在 job_dir 根目录下的文件名，不是
    # 完整路径——跟 job.preview_path/final_path 存完整路径不是一回事。
    output_filename: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # {lang, caption, hashtags}，social_caption.generate_caption() 的原样输出。
    # 故意不叫 social_caption——那个名字在这个文件里已经被 Job.social_caption
    # （social_batch.py 用，纯字符串）和 Job.talkinghead_social_caption
    # （transcript-based caption 功能，JSON blob）两边占用了，三个不同形状的
    # 字段抢同一个名字迟早互相踩。
    caption_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    degraded_operations: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON list，同 Job 的用法
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )
    # 没有加 relationship 到 Job——get_job() 现有那套 detached-instance/expunge
    # 处理比较敏感，不想因为这个新功能牵连改动。clips 通过 job_manager.get_clips()
    # 单独查询。


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
    if "generation_cost_usd" not in cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE jobs ADD COLUMN generation_cost_usd FLOAT"))
    if "batch_id" not in cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE jobs ADD COLUMN batch_id VARCHAR(36)"))
    if "platform" not in cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE jobs ADD COLUMN platform VARCHAR(32)"))
    if "social_caption" not in cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE jobs ADD COLUMN social_caption TEXT"))
    if "social_hashtags" not in cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE jobs ADD COLUMN social_hashtags TEXT"))
    if "talkinghead_social_caption" not in cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE jobs ADD COLUMN talkinghead_social_caption TEXT"))

    try:
        user_cols = {c["name"] for c in _inspect(engine).get_columns("users")}
    except Exception:
        user_cols = set()
    if user_cols and "elevenlabs_voice_id" not in user_cols:
        with engine.begin() as conn:
            conn.execute(_text("ALTER TABLE users ADD COLUMN elevenlabs_voice_id VARCHAR(64)"))


def _init_engine() -> None:
    global _engine, _SessionLocal
    config = get_config()
    db_url = config.database_url

    connect_args = {}
    is_sqlite = db_url.startswith("sqlite")
    if is_sqlite:
        connect_args["check_same_thread"] = False

    _engine = create_engine(db_url, connect_args=connect_args, echo=False)

    if is_sqlite:
        # Fix C29（2026-07-20，真实生产复现——job_f7b171f8d952 真实卡死：apply_style
        # 的后台管线线程在跑的同时（_run_in_background 起的真实 OS 线程，
        # check_same_thread=False 允许它跨线程读写同一个 SQLite 文件），
        # /files/{job_id}/{filename}（qa_stills 的 6 个并发 Chrome tab 反复调用）
        # 也在同一时间读同一个 jobs 表——SQLite 默认的 rollback-journal 模式下，
        # 写事务持有的是整个数据库文件的排他锁，读端会被卡住等锁；这里既没配
        # busy_timeout（默认 0，锁冲突要么立刻报错要么无限等，取决于底层 OS
        # 文件锁语义），也没开 WAL（允许读写并发、互不阻塞的标准模式）。Windows
        # 上 SQLite 的默认文件锁实现又比 Linux/macOS 更容易在这种反复短事务的
        # 场景下卡住——真实复现：先是这个 /files 路由反复超时（Remotion 自己的
        # delayRender 28s 超时），最后连 /health 都完全不响应，整个 uvicorn 进程
        # 卡死，需要手动 kill 重启。
        #
        # WAL（write-ahead log）模式让读操作不再需要等写事务释放锁（读的是
        # WAL 文件里已提交的快照，写继续追加到 WAL，定期 checkpoint 回主库），
        # 是官方文档明确推荐的"多线程/多进程并发读写同一个 SQLite 文件"标准
        # 配置。busy_timeout 兜底剩下那极少数真的撞上写锁的瞬间——阻塞式重试
        # 到超时为止，而不是立刻报错或者（更糟）无限期挂起。
        from sqlalchemy import event as _sa_event

        @_sa_event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    Base.metadata.create_all(bind=_engine)
    _migrate_schema(_engine)

    from sqlalchemy.orm import sessionmaker

    _SessionLocal = sessionmaker(bind=_engine)


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _init_engine()
    return _SessionLocal()
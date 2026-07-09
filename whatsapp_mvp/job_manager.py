# WhatsApp MVP - Job Manager (CRUD + lifecycle)

from __future__ import annotations

import json
import uuid
from typing import Optional

from .database import (
    Job,
    JobStatus,
    Message,
    MessageDirection,
    MessageType,
    User,
    get_session,
)
from .config import get_config


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def get_or_create_user(whatsapp_id: str) -> User:
    session = get_session()
    try:
        user = session.query(User).filter(User.whatsapp_id == whatsapp_id).first()
        if user is None:
            user = User(whatsapp_id=whatsapp_id)
            session.add(user)
            session.commit()
            session.refresh(user)
        return user
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------

def create_job(
    user_id: int,
    pipeline: str = "talking-head",
    input_caption: Optional[str] = None,
) -> Job:
    session = get_session()
    try:
        job = Job(
            id=f"job_{uuid.uuid4().hex[:12]}",
            user_id=user_id,
            status=JobStatus.RECEIVED,
            pipeline=pipeline,
            input_caption=input_caption,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        config = get_config()
        job.job_dir.mkdir(parents=True, exist_ok=True)
        return job
    finally:
        session.close()


def get_job(job_id: str) -> Optional[Job]:
    """获取任务，预加载 user 关系以避免 DetachedInstanceError。"""
    from sqlalchemy.orm import joinedload

    session = get_session()
    try:
        job = (
            session.query(Job)
            .options(joinedload(Job.user), joinedload(Job.messages))
            .filter(Job.id == job_id)
            .first()
        )
        # 将关联对象从 session 中剥离，允许 session 关闭后仍可访问
        if job:
            session.expunge(job)
            if job.user:
                session.expunge(job.user)
        return job
    finally:
        session.close()


def update_job_status(job_id: str, status: JobStatus, error_message: Optional[str] = None) -> Optional[Job]:
    session = get_session()
    try:
        job = session.query(Job).filter(Job.id == job_id).first()
        if job:
            job.status = status
            if error_message:
                job.error_message = error_message
            session.commit()
            session.refresh(job)
        return job
    finally:
        session.close()


def update_job_fields(job_id: str, **kwargs) -> Optional[Job]:
    session = get_session()
    try:
        job = session.query(Job).filter(Job.id == job_id).first()
        if job:
            for key, value in kwargs.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            session.commit()
            session.refresh(job)
        return job
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Asset helpers (b-roll / multi-asset intake) — thin JSON on Job.assets
# ---------------------------------------------------------------------------

def get_assets(job: Job) -> list[dict]:
    """解析 job.assets（JSON）为列表；为空/损坏时返回 []。"""
    raw = getattr(job, "assets", None)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def append_asset(job_id: str, media_id: str, kind: str, label: str = "") -> Optional[Job]:
    """往 job.assets 追加一条资产（kind = "video"|"image"）。
    角色在 collecting 期一律 provisional="broll"，"go" 时由 finalize_target 定 target。
    order 自动取当前长度。"""
    session = get_session()
    try:
        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            return None
        assets = get_assets(job)
        assets.append({
            "role": "broll",          # provisional; finalized at "go"
            "kind": kind,
            "media_id": media_id,
            "local_path": None,
            "label": label or "",
            "order": len(assets),
        })
        job.assets = json.dumps(assets, ensure_ascii=False)
        session.commit()
        session.refresh(job)
        return job
    finally:
        session.close()


def finalize_target(job_id: str, target_media_id: str) -> Optional[Job]:
    """'go' 时定角色：target_media_id 那条设 role=target，其余都 broll。"""
    session = get_session()
    try:
        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            return None
        assets = get_assets(job)
        for a in assets:
            a["role"] = "target" if a.get("media_id") == target_media_id else "broll"
        job.assets = json.dumps(assets, ensure_ascii=False)
        session.commit()
        session.refresh(job)
        return job
    finally:
        session.close()


def set_asset_local_path(job_id: str, media_id: str, local_path: str) -> None:
    """下载后回填某个资产的本地路径。"""
    session = get_session()
    try:
        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            return
        assets = get_assets(job)
        for a in assets:
            if a.get("media_id") == media_id:
                a["local_path"] = local_path
        job.assets = json.dumps(assets, ensure_ascii=False)
        session.commit()
    finally:
        session.close()


def get_active_job_for_user(user_id: int) -> Optional[Job]:
    from sqlalchemy.orm import joinedload

    session = get_session()
    try:
        job = (
            session.query(Job)
            .options(joinedload(Job.user), joinedload(Job.messages))
            .filter(
                Job.user_id == user_id,
                Job.status.notin_([JobStatus.DONE, JobStatus.ERROR]),
            )
            .order_by(Job.created_at.desc())
            .first()
        )
        if job:
            session.expunge(job)
            if job.user:
                session.expunge(job.user)
        return job
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------

def save_message(
    job_id: str,
    direction: MessageDirection,
    message_type: MessageType,
    content: Optional[str] = None,
    whatsapp_message_id: Optional[str] = None,
) -> Message:
    session = get_session()
    try:
        msg = Message(
            job_id=job_id,
            direction=direction,
            message_type=message_type,
            content=content,
            whatsapp_message_id=whatsapp_message_id,
        )
        session.add(msg)
        session.commit()
        session.refresh(msg)
        return msg
    finally:
        session.close()


def message_exists(whatsapp_message_id: str) -> bool:
    session = get_session()
    try:
        return (
            session.query(Message)
            .filter(Message.whatsapp_message_id == whatsapp_message_id)
            .first()
            is not None
        )
    finally:
        session.close()
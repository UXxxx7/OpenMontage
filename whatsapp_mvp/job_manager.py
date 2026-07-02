# WhatsApp MVP - Job Manager (CRUD + lifecycle)

from __future__ import annotations

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



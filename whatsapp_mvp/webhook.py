# WhatsApp MVP - FastAPI Webhook Service (Phase 2)

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, FileResponse

from .config import get_config
from .database import JobStatus, MessageDirection, MessageType
from .job_manager import (
    create_job,
    get_active_job_for_user,
    get_job,
    get_or_create_user,
    message_exists,
    save_message,
    update_job_fields,
    update_job_status,
)
from .whatsapp_client import WhatsAppClient

logger = logging.getLogger(__name__)

app = FastAPI(title="OpenMontage WhatsApp MVP")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_send(wa: WhatsAppClient, to: str, text: str) -> None:
    try:
        wa.send_text_message(to, text)
    except Exception as e:
        logger.warning(f"Failed to send WhatsApp message: {e}")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/workers/health")
async def workers_health():
    try:
        from redis import Redis
        config = get_config()
        r = Redis.from_url(config.redis_url, socket_connect_timeout=2, socket_timeout=2)
        r.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as e:
        return {"status": "degraded", "redis": str(e)}


# ---------------------------------------------------------------------------
# WhatsApp Webhook
# ---------------------------------------------------------------------------

@app.get("/webhook/whatsapp")
async def whatsapp_verify(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
):
    config = get_config()
    if hub_mode == "subscribe" and hub_verify_token == config.whatsapp_verify_token:
        logger.info("WhatsApp webhook verified successfully")
        return PlainTextResponse(hub_challenge)
    logger.warning("WhatsApp webhook verification failed")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    config = get_config()
    wa = WhatsAppClient(config)
    body = await request.body()

    signature = request.headers.get("X-Hub-Signature-256", "")
    if signature and not wa.verify_signature(signature, body):
        logger.warning("Invalid WhatsApp webhook signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    data = await request.json()
    logger.info(f"Webhook received: {json.dumps(data, indent=2)}")

    try:
        entries = data.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                contacts = value.get("contacts", [])

                for msg in messages:
                    await _handle_message(msg, contacts, wa)

    except Exception as e:
        logger.exception(f"Error processing webhook: {e}")

    return {"status": "received"}


async def _handle_message(msg: dict, contacts: list[dict], wa: WhatsAppClient) -> None:
    message_id = msg.get("id", "")
    from_number = msg.get("from", "")

    if message_exists(message_id):
        logger.info(f"Duplicate message {message_id}, skipping")
        return

    if not from_number:
        return

    user = get_or_create_user(from_number)
    msg_type = msg.get("type", "unknown")

    # --- Text messages ---
    if msg_type == "text":
        text_body = msg.get("text", {}).get("body", "").strip().lower()
        active_job = get_active_job_for_user(user.id)

        if active_job:
            if active_job.status == JobStatus.WAITING_CONFIRMATION:
                if text_body in ("confirm", "continue", "yes", "ok"):
                    await _handle_confirmation(active_job, wa)
                elif text_body in ("cancel", "no", "stop"):
                    update_job_status(active_job.id, JobStatus.ERROR, "Cancelled by user")
                    _safe_send(wa, from_number, "Edit cancelled. Send a new video to start again.")
                else:
                    _safe_send(wa, from_number, "Reply 'confirm' to proceed or 'cancel' to stop.")
                save_message(active_job.id, MessageDirection.INBOUND, MessageType.TEXT, text_body, message_id)
                return

            if active_job.status == JobStatus.PREVIEW_READY:
                if text_body in ("export", "final", "render"):
                    _enqueue_final_render(active_job.id)
                    _safe_send(wa, from_number, "Generating final video... this may take a few minutes.")
                elif text_body == "retry":
                    _enqueue_pipeline(active_job.id)
                    _safe_send(wa, from_number, "Re-running edit with your feedback...")
                else:
                    _safe_send(wa, from_number, "Reply 'export' to generate the final video or 'retry' to redo the edit.")
                save_message(active_job.id, MessageDirection.INBOUND, MessageType.TEXT, text_body, message_id)
                return

            if active_job.status == JobStatus.ERROR:
                if text_body in ("retry", "start over"):
                    _enqueue_process(active_job.id)
                    _safe_send(wa, from_number, "Restarting your edit request...")
                save_message(active_job.id, MessageDirection.INBOUND, MessageType.TEXT, text_body, message_id)
                return

        _safe_send(
            wa,
            from_number,
            "Send me a video and tell me how you'd like it edited! "
            "For example: 'Remove blank parts, add Chinese subtitles, and make it flow smoothly.'",
        )
        return

    # --- Video messages ---
    if msg_type == "video":
        video_data = msg.get("video", {})
        media_id = video_data.get("id", "")
        caption = video_data.get("caption", "")

        if not media_id:
            return

        job = create_job(user_id=user.id, pipeline="talking-head", input_caption=caption)
        # 保存 edit_request 和 media_id 供 worker 使用
        update_job_fields(job.id, edit_request=caption, whatsapp_media_id=media_id)
        update_job_status(job.id, JobStatus.RECEIVED)

        save_message(job.id, MessageDirection.INBOUND, MessageType.VIDEO, caption, message_id)

        _enqueue_process(job.id)
        _safe_send(wa, from_number, "Video received! I'll analyze it and let you know my edit plan.")
        return

    _safe_send(wa, from_number, "Please send a video for editing.")


# ---------------------------------------------------------------------------
# Confirmation handlers
# ---------------------------------------------------------------------------

async def _handle_confirmation(job, wa: WhatsAppClient) -> None:
    update_job_status(job.id, JobStatus.RUNNING_PIPELINE)
    _safe_send(wa, job.user.whatsapp_id, "Starting video edit... this may take a few minutes.")
    _enqueue_pipeline(job.id)


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def _run_in_background(func, *args) -> None:
    """在后台线程运行长任务，让 HTTP 请求立即返回。

    Node 网关侧本来就是轮询 GET /jobs/{id} 获取结果，因此 /confirm、/render
    不应同步阻塞到管线跑完（否则会撞上 Node 的请求超时并触发重试 → 400）。
    """
    import threading

    threading.Thread(target=func, args=args, daemon=True).start()


def _enqueue_process(job_id: str) -> None:
    """入队任务。MVP阶段始终同步执行，生产环境配置 USE_RQ_WORKER=true 启用异步。"""
    import os
    from .worker import process_incoming_message

    if os.getenv("USE_RQ_WORKER", "").lower() == "true":
        from redis import Redis
        import rq
        config = get_config()
        try:
            redis_conn = Redis.from_url(config.redis_url, socket_connect_timeout=2, socket_timeout=2)
            q = rq.Queue("whatsapp_mvp", connection=redis_conn)
            q.enqueue(process_incoming_message, job_id, job_timeout=600)
            logger.info(f"Enqueued {job_id} to RQ")
            return
        except Exception as e:
            logger.warning(f"RQ enqueue failed, falling back to sync: {e}")
    process_incoming_message(job_id)


def _enqueue_pipeline(job_id: str) -> None:
    import os
    from .worker import run_pipeline

    if os.getenv("USE_RQ_WORKER", "").lower() == "true":
        from redis import Redis
        import rq
        config = get_config()
        try:
            redis_conn = Redis.from_url(config.redis_url, socket_connect_timeout=2, socket_timeout=2)
            q = rq.Queue("whatsapp_mvp", connection=redis_conn)
            q.enqueue(run_pipeline, job_id, job_timeout=1800)
            logger.info(f"Enqueued pipeline {job_id} to RQ")
            return
        except Exception as e:
            logger.warning(f"RQ enqueue failed: {e}")
    run_pipeline(job_id)


def _enqueue_final_render(job_id: str) -> None:
    import os
    from .worker import run_final_render

    if os.getenv("USE_RQ_WORKER", "").lower() == "true":
        from redis import Redis
        import rq
        config = get_config()
        try:
            redis_conn = Redis.from_url(config.redis_url, socket_connect_timeout=2, socket_timeout=2)
            q = rq.Queue("whatsapp_mvp", connection=redis_conn)
            q.enqueue(run_final_render, job_id, job_timeout=1800)
            logger.info(f"Enqueued final render {job_id} to RQ")
            return
        except Exception as e:
            logger.warning(f"RQ enqueue failed: {e}")
    run_final_render(job_id)


def _enqueue_revise(job_id: str, text: str) -> None:
    from .worker import revise_plan

    revise_plan(job_id, text)


# ---------------------------------------------------------------------------
# Job API (internal)
# ---------------------------------------------------------------------------

@app.post("/jobs")
async def create_job_endpoint(
    video: UploadFile = File(...),
    edit_request: str = Form(""),
    pipeline: str = Form("talking-head"),
):
    config = get_config()
    user = get_or_create_user("api_user")

    job = create_job(user_id=user.id, pipeline=pipeline, input_caption=edit_request)

    job_dir = job.job_dir
    job_dir.mkdir(parents=True, exist_ok=True)
    video_data = await video.read()
    (job_dir / "input.mp4").write_bytes(video_data)

    update_job_fields(job.id, edit_request=edit_request, input_video_path=str(job_dir / "input.mp4"))
    update_job_status(job.id, JobStatus.RECEIVED)

    # 后台跑（下载 + L2 规划耗时可达 1~2 分钟），立即返回；否则会阻塞
    # uvicorn 事件循环，导致同时到来的 /confirm 等请求撞上 Node 的 30s 超时。
    # Node 侧本就通过轮询 GET /jobs/{id} 等待 WAITING_CONFIRMATION。
    _run_in_background(_enqueue_process, job.id)

    return {"job_id": job.id, "status": job.status.value}


@app.get("/jobs/{job_id}")
async def get_job_endpoint(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.id,
        "status": job.status.value,
        "input_video_path": job.input_video_path,
        "preview_path": job.preview_path,
        "final_path": job.final_path,
        "planned_edit": json.loads(job.planned_edit) if job.planned_edit else None,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@app.post("/jobs/{job_id}/confirm")
async def confirm_job_endpoint(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # 幂等：任务已在处理或已完成，直接返回当前状态，避免 Node 重试打到 400
    if job.status in (
        JobStatus.RUNNING_PIPELINE,
        JobStatus.RENDERING,
        JobStatus.PREVIEW_READY,
        JobStatus.DONE,
    ):
        return {"job_id": job_id, "status": job.status.value}
    if job.status != JobStatus.WAITING_CONFIRMATION:
        raise HTTPException(
            status_code=400,
            detail=f"Job is in {job.status.value}, not WAITING_CONFIRMATION",
        )

    update_job_status(job_id, JobStatus.RUNNING_PIPELINE)
    # 后台跑管线，立即返回；Node 侧通过轮询 GET /jobs/{id} 等待 PREVIEW_READY
    _run_in_background(_enqueue_pipeline, job_id)
    return {"job_id": job_id, "status": "RUNNING_PIPELINE"}


@app.post("/jobs/{job_id}/render")
async def render_job_endpoint(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # 幂等：正在渲染或已完成，直接返回，避免重试重复触发
    if job.status in (JobStatus.RENDERING, JobStatus.DONE):
        return {"job_id": job_id, "status": job.status.value}

    update_job_status(job_id, JobStatus.RENDERING)
    # 后台跑最终导出，立即返回；Node 侧轮询等待 DONE
    _run_in_background(_enqueue_final_render, job_id)
    return {"job_id": job_id, "status": "RENDERING"}


@app.post("/jobs/{job_id}/revise")
async def revise_job_endpoint(job_id: str, text: str = Form("")):
    """就地修订：带用户反馈重新规划编辑方案（方案阶段或预览阶段都可用）。"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    update_job_status(job_id, JobStatus.PLANNING)
    # 后台带反馈重规划，立即返回；Node 轮询等待新方案（WAITING_CONFIRMATION）
    _run_in_background(_enqueue_revise, job_id, text)
    return {"job_id": job_id, "status": "PLANNING"}


# ---------------------------------------------------------------------------
# File serving
# ---------------------------------------------------------------------------

@app.get("/files/{job_id}/{filename}")
async def serve_file(job_id: str, filename: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    file_path = job.job_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    media_type = "video/mp4" if filename.endswith(".mp4") else "application/octet-stream"
    return FileResponse(str(file_path), media_type=media_type)





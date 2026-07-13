# WhatsApp MVP - FastAPI Webhook Service (Phase 2)

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, FileResponse

from .config import get_config
from .database import JobStatus, MessageDirection, MessageType
from .job_manager import (
    append_asset,
    create_job,
    finalize_target,
    get_active_job_for_user,
    get_assets,
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

            # b-roll 收集期：'go' 收尾开始，其余提示继续发素材
            if active_job.status == JobStatus.COLLECTING_ASSETS:
                if text_body in ("go", "start", "done", "开始", "完成", "好了"):
                    _finalize_collection(active_job, from_number, wa)
                else:
                    _safe_send(wa, from_number, "Send your clips, then reply 'go' to start.")
                save_message(active_job.id, MessageDirection.INBOUND, MessageType.TEXT, text_body, message_id)
                return

            # 2+ 视频时问哪个是主视频：解析用户回的编号
            if active_job.status == JobStatus.NEEDS_TARGET_CHOICE:
                videos = [a for a in get_assets(active_job) if a.get("kind") == "video"]
                try:
                    choice = int("".join(ch for ch in text_body if ch.isdigit()))
                except ValueError:
                    choice = 0
                if 1 <= choice <= len(videos):
                    target = videos[choice - 1]
                    finalize_target(active_job.id, target["media_id"])
                    update_job_fields(
                        active_job.id,
                        whatsapp_media_id=target["media_id"],
                        edit_request=active_job.edit_request or target.get("label", ""),
                    )
                    _safe_send(wa, from_number, "Got it. Starting the edit...")
                    _enqueue_process(active_job.id)
                else:
                    _safe_send(wa, from_number, f"Please reply with a number 1-{len(videos)}.")
                save_message(active_job.id, MessageDirection.INBOUND, MessageType.TEXT, text_body, message_id)
                return

        _safe_send(
            wa,
            from_number,
            "Send me a video and tell me how you'd like it edited! "
            "For example: 'Remove blank parts, add Chinese subtitles, and make it flow smoothly.'",
        )
        return

    # --- Media messages (video / image) — collect into one job, start on 'go' ---
    if msg_type in ("video", "image"):
        media = msg.get(msg_type, {})
        media_id = media.get("id", "")
        caption = media.get("caption", "")
        if not media_id:
            return
        _collect_media(user, from_number, msg_type, media_id, caption, message_id, wa)
        return

    _safe_send(wa, from_number, "Please send a video for editing.")


# ---------------------------------------------------------------------------
# b-roll / multi-asset collection
# ---------------------------------------------------------------------------

def _collect_media(user, from_number, kind, media_id, caption, message_id, wa) -> None:
    """把一条媒体收进 collecting 中的 job；没有就新建一个 COLLECTING_ASSETS job。"""
    mtype = MessageType.VIDEO if kind == "video" else MessageType.IMAGE
    active = get_active_job_for_user(user.id)
    # 收集态、或"问哪个是主视频"态都接着收：问角色期又来素材→视频集变了，
    # 退回收集态、重新等 'go'（旧的编号问题作废，防孤立 job）。
    if active and active.status in (JobStatus.COLLECTING_ASSETS, JobStatus.NEEDS_TARGET_CHOICE):
        job = append_asset(active.id, media_id, kind, caption)
        if active.status == JobStatus.NEEDS_TARGET_CHOICE:
            update_job_status(active.id, JobStatus.COLLECTING_ASSETS)
        save_message(active.id, MessageDirection.INBOUND, mtype, caption, message_id)
        _schedule_collect_timeout(active.id, len(get_assets(job)) if job else 0)
        return
    # 新起一个收集 job，这条作为第一个资产
    job = create_job(user_id=user.id, pipeline="talking-head", input_caption=caption)
    update_job_status(job.id, JobStatus.COLLECTING_ASSETS)
    job = append_asset(job.id, media_id, kind, caption)
    if kind == "video":
        # 暂定为 target（向后兼容：worker/pipeline 读 whatsapp_media_id）
        update_job_fields(job.id, edit_request=caption, whatsapp_media_id=media_id)
    save_message(job.id, MessageDirection.INBOUND, mtype, caption, message_id)
    _schedule_collect_timeout(job.id, len(get_assets(job)) if job else 0)
    _safe_send(
        wa, from_number,
        "Got it. Send your main video plus any b-roll clips/photos "
        "(add a short note on each saying where it goes). "
        "Reply 'go' when done — or 'go' now to edit without b-roll.",
    )


def _schedule_collect_timeout(job_id: str, seen_count: int) -> None:
    """收集态超时兜底：RQ 可用时延时入队一个 finalize 检查；此后无新素材则自动 'go'，
    防止用户发了视频却不打 'go' 而永久卡住。sync 模式（无后台调度）下 no-op——那时靠显式 'go'。"""
    import os
    if os.getenv("USE_RQ_WORKER", "").lower() != "true":
        return
    try:
        import datetime as _dt
        from redis import Redis
        import rq
        config = get_config()
        delay = int(os.getenv("WA_COLLECT_TIMEOUT_S", "45"))
        redis_conn = Redis.from_url(config.redis_url, socket_connect_timeout=2, socket_timeout=2)
        q = rq.Queue("whatsapp_mvp", connection=redis_conn)
        q.enqueue_in(_dt.timedelta(seconds=delay), finalize_collection_timeout, job_id, seen_count)
    except Exception as e:
        logger.warning(f"schedule collect timeout failed: {e}")


def finalize_collection_timeout(job_id: str, seen_count: int) -> None:
    """RQ 延时回调：仍是收集态、且此后无新素材（资产数没变）→ 自动 finalize（等价用户 'go'）。
    资产数变了说明期间又来了素材，交给更晚那次调度的超时处理，本次直接放行。"""
    job = get_job(job_id)
    if not job or job.status != JobStatus.COLLECTING_ASSETS:
        return
    if len(get_assets(job)) != seen_count:
        return
    config = get_config()
    wa = WhatsAppClient(config)
    from_number = job.user.whatsapp_id if job.user else None
    if from_number:
        _finalize_collection(job, from_number, wa)


def _finalize_collection(job, from_number, wa) -> None:
    """'go' 时定角色并开始：0 视频→提示；1 视频→直接开始；2+ 视频→问哪个是主视频。"""
    assets = get_assets(job)
    videos = [a for a in assets if a.get("kind") == "video"]
    if not videos:
        _safe_send(wa, from_number, "I need a main video to edit. Send one, then reply 'go'.")
        return
    if len(videos) == 1:
        target = videos[0]
        finalize_target(job.id, target["media_id"])
        update_job_fields(
            job.id,
            whatsapp_media_id=target["media_id"],
            edit_request=job.edit_request or target.get("label", ""),
        )
        n_broll = len(assets) - 1
        extra = f" and {n_broll} b-roll item(s)" if n_broll else ""
        _safe_send(wa, from_number, f"Got your video{extra}. Starting the edit...")
        _enqueue_process(job.id)
        return
    # 2+ 视频 → 问哪个是主视频
    update_job_status(job.id, JobStatus.NEEDS_TARGET_CHOICE)
    lines = ["Which one is your main talking-head video? Reply with the number:"]
    for i, v in enumerate(videos, 1):
        lbl = v.get("label") or f"clip {v.get('order', '?')}"
        lines.append(f"{i}. {lbl}")
    _safe_send(wa, from_number, "\n".join(lines))


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
            # 2700s (was 1800s) — apply_style's vision self-review can now retry
            # once (one extra plan_content call + one extra bounded QA-stills
            # pass) before falling through to graceful degradation; the old
            # budget was sized for a single render only.
            q.enqueue(run_pipeline, job_id, job_timeout=2700)
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

_ASSIGN_SYSTEM = (
    "你是视频剪辑助手的意图解析器。用户按顺序上传了若干视频（编号从 1 开始），"
    "并用一段或多段文字描述这些视频要怎么剪。请判断：\n"
    "1. 哪个视频是“主视频”（出镜/口播、要加字幕或剪辑的主体）——返回它的编号（1 开始）；"
    "文字里说不清就返回 null。\n"
    "2. 其余视频是 b-roll 补充素材，为每个 b-roll 提取插入说明（label，如“讲到 VS Code 时插入”）。\n"
    "3. 提取对主视频的编辑要求 edit_request（如“加字幕、剪掉空白和自我打断”）。\n"
    "只输出 JSON，不要多余文字：{\"main_index\": <int|null>, "
    "\"labels\": {\"<视频编号>\": \"<说明>\"}, \"edit_request\": \"<字符串>\"}。"
    "labels 只含 b-roll 视频（不含主视频），键是视频编号的字符串；某段找不到说明就给空字符串。"
)


@app.post("/assign")
async def assign_endpoint(video_count: int = Form(...), notes: str = Form("")):
    """把“N 个视频（按上传顺序）+ 用户描述文字”解析成 {main_index, labels, edit_request}。
    解析失败或说不清主视频时 main_index=null，由 Node 侧回退到“问编号”。"""
    result = {"main_index": None, "labels": {}, "edit_request": notes or ""}
    try:
        from .llm_client import call_llm_chat
        user_msg = (
            f"视频数量：{video_count}（编号 1..{video_count}，按上传顺序）。\n"
            f"用户描述：\n{notes.strip() or '(无)'}"
        )
        raw = call_llm_chat(_ASSIGN_SYSTEM, user_msg, temperature=0.0)
        if raw:
            import re as _re
            m = _re.search(r"\{.*\}", raw, _re.S)
            data = json.loads(m.group(0)) if m else {}
            mi = data.get("main_index")
            if isinstance(mi, bool):
                mi = None
            if isinstance(mi, (int, float)):
                result["main_index"] = int(mi)
            elif isinstance(mi, str) and mi.strip().isdigit():
                result["main_index"] = int(mi.strip())
            if isinstance(data.get("labels"), dict):
                result["labels"] = {str(k): str(v) for k, v in data["labels"].items()}
            if data.get("edit_request"):
                result["edit_request"] = str(data["edit_request"])
    except Exception as e:
        logger.warning(f"/assign 解析失败，回退问编号: {e}")
    return result


@app.post("/qa")
async def qa_endpoint(text: str = Form(...)):
    """自由文本问答——网关侧收到一条既不是命令、也不在任何活跃任务/收集态
    里的文字时打这里，取代之前"一律回写死帮助文案"的答非所问。"""
    from .qa_answer import answer_question

    answer = answer_question(text)
    return {"answer": answer}


@app.post("/jobs")
async def create_job_endpoint(
    video: UploadFile = File(...),
    edit_request: str = Form(""),
    pipeline: str = Form("talking-head"),
    broll: List[UploadFile] = File(default=[]),
    broll_labels: List[str] = Form(default=[]),
    broll_kinds: List[str] = Form(default=[]),
):
    config = get_config()
    user = get_or_create_user("api_user")

    job = create_job(user_id=user.id, pipeline=pipeline, input_caption=edit_request)

    job_dir = job.job_dir
    job_dir.mkdir(parents=True, exist_ok=True)
    video_data = await video.read()
    (job_dir / "input.mp4").write_bytes(video_data)

    update_job_fields(job.id, edit_request=edit_request, input_video_path=str(job_dir / "input.mp4"))

    # b-roll 素材：Node 网关已从 WhatsApp 下载并随表单上传。这里落盘到
    # assets/broll_<i>.<ext> 并登记进 job.assets（role=broll, order=i, label）。
    # 同时写入 local_path —— worker._download_broll_assets 见到 local_path 即跳过，
    # 不会重复去 WhatsApp 拉取；pipeline_runner.insert_broll 直接按 broll_<order>.* 取用。
    _IMAGE_EXTS = ("jpg", "jpeg", "png", "webp", "gif", "bmp")
    if broll:
        from .job_manager import append_asset, set_asset_local_path
        assets_dir = job_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        for i, up in enumerate(broll):
            data = await up.read()
            ext = (os.path.splitext(up.filename or "")[1].lstrip(".") or "mp4").lower()
            dest = assets_dir / f"broll_{i}.{ext}"
            dest.write_bytes(data)
            if i < len(broll_kinds) and broll_kinds[i]:
                kind = broll_kinds[i]
            else:
                kind = "image" if ext in _IMAGE_EXTS else "video"
            label = broll_labels[i] if i < len(broll_labels) else ""
            media_id = f"local_{i}"
            append_asset(job.id, media_id, kind, label)          # role=broll, order=i
            set_asset_local_path(job.id, media_id, str(dest))    # 标记已下载

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
        # Node 侧回复用户的文案要按这条任务的语言走（不能写死英文/中文），
        # edit_request 是用户自己敲的原话，是最可靠的语言信号——之前没往外
        # 暴露，Node 只能瞎猜或者写死一种语言。
        "edit_request": job.edit_request,
        # 非致命失败被跳过的操作（如 ["apply_style"]）。Node 侧预览消息靠它
        # 如实告知"哪步没成 + 可回复 retry"，不再静默交付半成品。
        "degraded_operations": json.loads(job.degraded_operations) if job.degraded_operations else [],
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@app.post("/jobs/{job_id}/retry")
async def retry_job_endpoint(job_id: str):
    """按原方案整单重跑管线。给两类场景用：预览里有降级步骤（用户回复 retry
    要完整效果），或整单 ERROR 后想再试一次。与 /revise 的区别：不改方案，
    只重执行。"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # 幂等：正在跑就直接返回，避免 Node 队列重试触发双跑
    if job.status in (JobStatus.RUNNING_PIPELINE, JobStatus.RENDERING):
        return {"job_id": job_id, "status": job.status.value}
    if job.status not in (JobStatus.PREVIEW_READY, JobStatus.ERROR, JobStatus.DONE):
        raise HTTPException(
            status_code=400,
            detail=f"Job is in {job.status.value}, cannot retry",
        )
    if not job.planned_edit:
        raise HTTPException(status_code=400, detail="Job has no edit plan to retry")
    update_job_fields(job_id, status=JobStatus.RUNNING_PIPELINE,
                      error_message=None, degraded_operations=None)
    _run_in_background(_enqueue_pipeline, job_id)
    return {"job_id": job_id, "status": "RUNNING_PIPELINE"}


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
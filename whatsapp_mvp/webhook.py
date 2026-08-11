# WhatsApp MVP - FastAPI Webhook Service (Phase 2)

from __future__ import annotations

import copy
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
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
    get_clips,
    get_job,
    get_jobs_by_status,
    get_or_create_user,
    message_exists,
    save_message,
    update_job_fields,
    update_job_status,
)
from .whatsapp_client import WhatsAppClient

logger = logging.getLogger(__name__)


def _recover_orphaned_jobs() -> None:
    """Jobs run as a daemon thread inside *this* process (`_run_in_background`
    in this file) unless `USE_RQ_WORKER=true` routes them to an independent RQ
    worker process instead. In the (default, no env var set) in-process mode,
    if this server restarts while a job's pipeline/render thread is running,
    that thread dies with the old process — the DB is left holding a stale
    RUNNING_PIPELINE/RENDERING status with nothing behind it, forever. Worse,
    `/jobs/{id}/retry`'s own idempotency guard refuses to touch a job in
    either of those states (reasonably assumes something's already handling
    it), so nothing short of manually editing the DB row could ever unstick
    it. Confirmed real: job_d9111d13d08b sat dead for ~30 minutes on 2026-07-23
    after exactly this restart, silently, until fixed by hand.

    At a fresh startup (in-process mode), any job already in one of those two
    states is provably orphaned — this process just started, so nothing here
    could have put it there. Mark it ERROR (with an explanatory message) so
    the user's next 'retry' actually restarts the pipeline, and the existing
    WhatsApp failure-notification path (server/worker.js) tells them so
    instead of leaving them waiting on a job that will never move again.
    """
    if os.getenv("USE_RQ_WORKER", "").lower() == "true":
        return  # separate RQ worker process — a webhook-server restart doesn't touch it
    orphaned = get_jobs_by_status([JobStatus.RUNNING_PIPELINE, JobStatus.RENDERING])
    for job in orphaned:
        logger.warning(f"[startup] recovering orphaned job {job.id} (was {job.status.value}) -> ERROR")
        update_job_status(job.id, JobStatus.ERROR,
            error_message="Orphaned by a server restart mid-pipeline; no process was left driving it. Reply 'retry' to try again.")
    if orphaned:
        logger.warning(f"[startup] recovered {len(orphaned)} orphaned job(s)")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _recover_orphaned_jobs()
    yield


app = FastAPI(title="OpenMontage WhatsApp MVP", lifespan=_lifespan)


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
            # clip-factory 渲染的是 N 条独立 clip（裁剪+转写+字幕+调色+降噪+配文
            # 各来一遍），管线内部自己的 wall-time 预算（config.clip_factory_wall_time_s，
            # 默认 1800s）到点会主动收尾，但 RQ 的 job_timeout 必须留够余量盖过它，
            # 否则 RQ 会在管线自己优雅收尾之前就先把整个 worker 进程杀掉。
            job = get_job(job_id)
            timeout = (max(2700, config.clip_factory_wall_time_s + 600)
                      if job and job.pipeline == "clip-factory" else 2700)
            q.enqueue(run_pipeline, job_id, job_timeout=timeout)
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


def _enqueue_editor_render(job_id: str) -> None:
    from .worker import editor_render

    editor_render(job_id)


def _enqueue_editor_render_authored(job_id: str) -> None:
    from .worker import editor_render_authored

    editor_render_authored(job_id)


# ---------------------------------------------------------------------------
# Job API (internal)
# ---------------------------------------------------------------------------

_ASSIGN_SYSTEM = (
    "你是视频剪辑助手的意图解析器。用户按顺序上传了若干视频（编号从 1 开始），"
    "并用一段或多段文字描述这些视频要怎么剪。请判断：\n"
    "1. 哪个视频是“主视频”（出镜/口播、要加字幕或剪辑的主体）——返回它的编号（1 开始）；"
    "文字里说不清就返回 null。\n"
    "2. 是否有某个视频是“参考风格视频”——用户想模仿它的剪辑风格/转场/节奏/特效/"
    "色彩/字幕样式（如“参照这个视频的风格剪”“照这个的转场来”），它本身既不是要剪的"
    "内容、也不是 b-roll 素材。有就返回它的编号（1 开始，且必须与主视频编号不同），没有返回 null。\n"
    "3. 其余视频是 b-roll 补充素材，为每个 b-roll 提取插入说明（label，如“讲到 VS Code 时插入”）。\n"
    "4. 提取对主视频的编辑要求 edit_request（如“加字幕、剪掉空白和自我打断”）。\n"
    "只输出 JSON，不要多余文字：{\"main_index\": <int|null>, \"reference_index\": <int|null>, "
    "\"labels\": {\"<视频编号>\": \"<说明>\"}, \"edit_request\": \"<字符串>\"}。"
    "labels 只含 b-roll 视频（不含主视频、不含参考风格视频），键是视频编号的字符串；某段找不到说明就给空字符串。"
)


@app.post("/assign")
async def assign_endpoint(video_count: int = Form(...), notes: str = Form("")):
    """把“N 个视频（按上传顺序）+ 用户描述文字”解析成 {main_index, labels, edit_request}。
    解析失败或说不清主视频时 main_index=null，由 Node 侧回退到“问编号”。"""
    result = {"main_index": None, "reference_index": None, "labels": {}, "edit_request": notes or ""}
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
            ri = data.get("reference_index")
            if isinstance(ri, bool):
                ri = None
            if isinstance(ri, (int, float)):
                result["reference_index"] = int(ri)
            elif isinstance(ri, str) and ri.strip().isdigit():
                result["reference_index"] = int(ri.strip())
            # 参考视频不能同时是主视频（模型偶尔混淆）——冲突则丢弃参考判断
            if (result["reference_index"] is not None
                    and result["reference_index"] == result["main_index"]):
                result["reference_index"] = None
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


@app.post("/transcribe")
async def transcribe_endpoint(audio: UploadFile = File(...)):
    """WhatsApp 语音消息转写成文字（架构复审后新增，2026-07-29）——设计上
    刻意不新建一整套"语音指令"路由：Node 网关下载语音、转写成文字之后，
    直接把它当成一条普通文字消息，原样喂给 handleMessage 里那一整套已经
    很成熟的上下文路由逻辑（收集态/等选臂/活跃任务确认/修改意见/问答……）
    ——同一句话不管是打字还是说出来，理解和路由方式完全一样，不用为语音
    另外维护一份行为可能悄悄分叉的平行逻辑。

    跟 job 生命周期无关的纯工具调用，不建 job、不落库——复用
    tools.analysis.transcriber.Transcriber（全项目统一的转写实现，语音消息
    通常几秒到几十秒，不需要 apply_style 那条链路的缓存/校准这些重量级
    机制）。转写失败（模型不可用/音频损坏等）返回空文本，Node 侧按"没听清"
    处理，不阻断整个 webhook 处理流程。
    """
    import tempfile

    from tools.analysis.transcriber import Transcriber

    data = await audio.read()
    ext = (os.path.splitext(audio.filename or "")[1].lstrip(".") or "ogg").lower()
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        result = Transcriber().execute({
            "input_path": tmp_path,
            "model_size": get_config().faster_whisper_model,
        })
    except Exception as e:
        logger.warning(f"/transcribe: 转写异常: {e}")
        return {"text": "", "error": str(e)}
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not result.success:
        logger.warning(f"/transcribe: 转写失败: {result.error}")
        return {"text": "", "error": result.error}

    segments = result.data.get("segments") or []
    text = " ".join(s.get("text", "").strip() for s in segments if s.get("text", "").strip())
    return {"text": text.strip(), "language": result.data.get("language")}


@app.post("/croll")
async def create_croll_endpoint(
    photo: UploadFile = File(...),
    hint: str = Form(""),
    lang: str = Form("zh"),
    pipeline: str = Form("talking-head"),
    broll: List[UploadFile] = File(default=[]),
    broll_labels: List[str] = Form(default=[]),
    broll_kinds: List[str] = Form(default=[]),
    wa_number: str = Form("api_user"),
):
    """C-roll：一张照片 -> AI 看图写文案 -> HeyGen 数字人说话视频 -> 接入常规
    剪辑管线（后续 confirm/export/retry 跟普通视频任务完全一样）。

    跟 POST /jobs 的形状故意保持一致（同样的 job 生命周期），区别只在于
    input.mp4 的来源——那边是用户直接传视频，这边是后台生成出来的。生成
    这几步（看图写文案 + HeyGen 上传/生成/轮询）合起来能到 1-2 分钟，全部
    放进后台线程，立即返回 job_id，Node 侧照旧轮询 GET /jobs/{id}。

    broll/broll_labels/broll_kinds：跟 POST /jobs 完全同一套参数形状、同一套
    登记逻辑（架构复审后新增，2026-07-29）——之前这里没有这三个参数，图生
    视频的 job 上永远不会有 role=="broll" 的资产，insert_broll 这一步因此永
    远不会被 L2 规划器 emit，不是管线跑不通，是这个入口没给它素材可用。
    HeyGen 生成完成后 generate_croll() 会把成品当 input.mp4 接入
    process_incoming_message，从那一步起跟普通视频任务走的是完全同一条路
    （转写/L2 规划/apply_style/insert_broll 一个都不用改）——L2 规划器读
    job 上的资产列表（job_manager.get_assets，按 role=="broll" 过滤）来决定
    要不要 emit insert_broll，这个判断跟 job 的主视频到底是用户直接传的还
    是这里生成出来的完全无关，只要资产已经登记好、下载到本地即可。这里的
    落盘/登记顺序特意跟 /jobs 的 broll 处理逐行对齐，避免两处各写一套、日后
    行为悄悄分叉（Rule 5/13 的教训）。

    注意：这个改动只打通了直接调用这个 API 的路径（比如脚本/未来的其它前
    端）——Node 网关目前收到照片触发 C-roll 生成时，只会带上这一张照片本
    身，还没有收集"这条消息之后用户还发了哪些 b-roll 素材"的逻辑（那是
    server/worker.js 的 crollGenerate，需要照着现有视频上传流程的多消息收
    集窗口另外接一遍，属于更大的一块改动，不在这次范围内）。WhatsApp 用户
    今天直接发照片触发 C-roll，还是拿不到 b-roll 合成——这条路目前只对直
    接打 API 的调用方生效。

    wa_number：真实 WhatsApp 号，不传就退回旧的共享 "api_user"（兼容还没升级
    的调用方）。2026-08 之前这里硬编码 "api_user"，导致所有艺人在 Python 侧
    共享同一个 User 行——上线 voice_clone.py 后这是真 bug 不只是"不精确"：
    User.elevenlabs_voice_id 存在共享行上，等于所有人共用同一个克隆音色，
    艺人 A 的声音会出现在艺人 B 的视频里。Node 网关这边已经把真实 waNumber
    传进来了，见 server/worker.js 的 createPythonCrollJob。
    """
    user = get_or_create_user(wa_number)
    job = create_job(user_id=user.id, pipeline=pipeline, input_caption=hint)

    job_dir = job.job_dir
    job_dir.mkdir(parents=True, exist_ok=True)
    photo_data = await photo.read()
    ext = (os.path.splitext(photo.filename or "")[1].lstrip(".") or "jpg").lower()
    photo_path = job_dir / f"source_photo.{ext}"
    photo_path.write_bytes(photo_data)

    # b-roll 素材：跟 POST /jobs 逐行对齐的同一套登记逻辑（见上面 docstring）。
    _IMAGE_EXTS = ("jpg", "jpeg", "png", "webp", "gif", "bmp")
    if broll:
        from .job_manager import set_asset_local_path
        assets_dir = job_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        for i, up in enumerate(broll):
            data = await up.read()
            broll_ext = (os.path.splitext(up.filename or "")[1].lstrip(".") or "mp4").lower()
            dest = assets_dir / f"broll_{i}.{broll_ext}"
            dest.write_bytes(data)
            if i < len(broll_kinds) and broll_kinds[i]:
                kind = broll_kinds[i]
            else:
                kind = "image" if broll_ext in _IMAGE_EXTS else "video"
            label = broll_labels[i] if i < len(broll_labels) else ""
            media_id = f"local_{i}"
            append_asset(job.id, media_id, kind, label)          # role=broll, order=i
            set_asset_local_path(job.id, media_id, str(dest))    # 标记已下载

    update_job_status(job.id, JobStatus.DOWNLOADING_MEDIA)

    from .worker import generate_croll
    _run_in_background(generate_croll, job.id, str(photo_path), lang, hint)

    return {"job_id": job.id, "status": job.status.value}


@app.post("/social-batch")
async def create_social_batch_endpoint(
    photo: UploadFile = File(...),
    hint: str = Form(""),
    lang: str = Form("zh"),
    wa_number: str = Form("api_user"),
):
    """一张照片 -> 一批多平台社媒内容（IG Feed/Reel·TikTok/Story），每个变体
    各自的文案+hashtag，见 social_batch.py 的设计说明。跟 /croll 的形状类似
    （立即返回、后台线程跑生成），区别是这里一次产出一批 job（共享 batch_id），
    不是单条——调用方轮询 GET /batches/{batch_id} 而不是 GET /jobs/{id}。

    wa_number：真实 WhatsApp 号——见 /croll 端点同一处注释，这里同理，是
    voice_clone.py 能"找对艺人本人的克隆音色"的前提，不是可选的精确化。
    """
    import uuid as _uuid

    user = get_or_create_user(wa_number)
    batch_id = f"batch_{_uuid.uuid4().hex[:12]}"

    # 照片先落到一个临时目录（不属于任何单条 job——这批要生成好几条 job，
    # 各自的 job_dir 要等 create_job 才存在）。social_batch.generate_batch
    # 内部会把这张原图复制/裁切进每个变体自己的 job_dir。
    tmp_dir = Path(get_config().jobs_dir) / f"_batch_upload_{batch_id}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    photo_data = await photo.read()
    ext = (os.path.splitext(photo.filename or "")[1].lstrip(".") or "jpg").lower()
    photo_path = tmp_dir / f"source_photo.{ext}"
    photo_path.write_bytes(photo_data)

    from .social_batch import generate_batch

    def _run():
        try:
            generate_batch(batch_id, user.id, str(photo_path), lang=lang, hint=hint)
        finally:
            import shutil as _shutil
            _shutil.rmtree(tmp_dir, ignore_errors=True)

    _run_in_background(_run)
    return {"batch_id": batch_id}


@app.get("/batches/{batch_id}")
def get_batch_endpoint(batch_id: str):
    """Studio 预览页读这个接口拿一批内容的全部变体状态。"""
    from .job_manager import get_jobs_by_batch

    jobs = get_jobs_by_batch(batch_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="Batch not found")

    variants = []
    for job in jobs:
        filename = Path(job.final_path).name if job.final_path else None
        try:
            hashtags = json.loads(job.social_hashtags) if job.social_hashtags else []
        except (ValueError, TypeError):
            hashtags = []
        variants.append({
            "job_id": job.id,
            "platform": job.platform,
            "status": job.status.value,
            "caption": job.social_caption or "",
            "hashtags": hashtags,
            "filename": filename,
            "asset_kind": "video" if (filename or "").endswith(".mp4") else "image",
        })
    return {"batch_id": batch_id, "variants": variants}


@app.post("/voice-clone")
async def create_voice_clone_endpoint(
    audio: UploadFile = File(...),
    wa_number: str = Form(...),
):
    """艺人一次性声音入驻：一段语音样本 -> ElevenLabs Instant Voice Clone ->
    voice_id 存到这个艺人（按真实 wa_number 识别）的 User 行上。之后每次
    generate_croll/social_batch.generate_batch 都会读这个字段，找到了就用
    真实克隆音色（走 HeyGen 的音频对口型模式），没有就照旧退回 HeyGen 库存声音。

    跟 /croll、/social-batch 不一样：这个同步做完再返回（IVC 本身就是秒级
    操作，不像 HeyGen 视频生成要 1-2 分钟，没必要为此再搭一套后台线程+轮询）。
    """
    from .voice_clone import create_instant_voice_clone

    user = get_or_create_user(wa_number)
    audio_data = await audio.read()
    tmp_path = Path(get_config().jobs_dir) / f"_voice_sample_{user.id}.mp3"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_bytes(audio_data)
    try:
        voice_id = create_instant_voice_clone(tmp_path, name=f"artist_{wa_number}")
    finally:
        tmp_path.unlink(missing_ok=True)

    if not voice_id:
        raise HTTPException(status_code=502, detail="voice clone creation failed")

    from .job_manager import set_user_voice_clone
    set_user_voice_clone(user.id, voice_id)
    return {"voice_id": voice_id}


@app.post("/jobs")
async def create_job_endpoint(
    video: UploadFile = File(...),
    edit_request: str = Form(""),
    pipeline: str = Form("talking-head"),
    broll: List[UploadFile] = File(default=[]),
    broll_labels: List[str] = Form(default=[]),
    broll_kinds: List[str] = Form(default=[]),
    arm: str = Form(""),
    reference: Optional[UploadFile] = File(default=None),
    reference_kind: str = Form(""),
):
    config = get_config()
    user = get_or_create_user("api_user")

    job = create_job(user_id=user.id, pipeline=pipeline, input_caption=edit_request)

    job_dir = job.job_dir
    job_dir.mkdir(parents=True, exist_ok=True)
    # 本 job 显式臂选择(WhatsApp 点选,Node 侧作为 arm 字段传来)→ 落 arm_choice.txt,
    # arm_router.resolve_arm 最优先读它(仅次于运维急停 force_arm)。空/不传=不写。
    if arm.strip():
        try:
            from .authored.arm_router import set_job_arm
            set_job_arm(job_dir, arm.strip().lower())
        except Exception as _e:  # noqa: BLE001 —— 写失败不拖垮建 job,退回默认路由
            logger.warning(f"写 arm_choice 失败(忽略,走默认路由): {_e}")
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

    # 参考风格视频（可选，模块4）：模型读它的剪辑风格（转场/节奏/特效/色彩/字幕样式
    # 等），落盘到 job_dir/style_ref.<ext>；authored 侧 _prepare 会 glob 到它并分析成
    # style_spec。它既不是要剪的主视频、也不是要合成的 b-roll，故不进 job.assets。
    if reference is not None:
        ref_data = await reference.read()
        if ref_data:
            ref_ext = (os.path.splitext(reference.filename or "")[1].lstrip(".")
                       or ("jpg" if (reference_kind or "").lower() == "image" else "mp4")).lower()
            (job_dir / f"style_ref.{ref_ext}").write_bytes(ref_data)

    update_job_status(job.id, JobStatus.RECEIVED)

    # 后台跑（下载 + L2 规划耗时可达 1~2 分钟），立即返回；否则会阻塞
    # uvicorn 事件循环，导致同时到来的 /confirm 等请求撞上 Node 的 30s 超时。
    # Node 侧本就通过轮询 GET /jobs/{id} 等待 WAITING_CONFIRMATION。
    _run_in_background(_enqueue_process, job.id)

    return {"job_id": job.id, "status": job.status.value}


def _clips_summary(job) -> list:
    """clip-factory 管线的 Clip 子表 -> API 响应用的扁平列表。status 不是
    READY 的（RENDERING/FAILED）也照样列出来，不静默丢弃——跟这个代码库
    其余地方"如实告知有哪些没成"的一贯做法一致，Node 侧靠 error_message
    判断要不要显示"第 N 条失败了"而不是假装那条从来没存在过。"""
    config = get_config()
    out = []
    for c in get_clips(job.id):
        out.append({
            "rank": c.rank,
            "clip_family": c.clip_family,
            "status": c.status.value,
            "hook_text": c.hook_text,
            "duration_seconds": c.duration_seconds,
            "url": (f"{config.public_base_url}/files/{job.id}/{c.output_filename}"
                   if c.status.value == "READY" and c.output_filename else None),
            "caption": json.loads(c.caption_json) if c.caption_json else None,
            "error_message": c.error_message,
        })
    return out


def _animations_summary(job) -> Optional[list]:
    """从 apply_style 的最终 props 里提取"这条视频实际包含哪些动画"的人话
    清单——确认过的真实用户反馈：预览消息只会念模板简介（"floating cards +
    karaoke subtitles..."，条条视频一模一样），从不说这条视频真正规划出了
    什么动画，用户要收到成片才发现是空的。props 是渲染的唯一事实来源，
    从它读就不会说谎。

    确认过的真实 bug（2026-07-23，job_fa4ee47e9676，用户直接指出"两句话自相
    矛盾"）：apply_style 整个降级（跳过失败的步骤、只交付上一步结果）时，
    _op_apply_style_props.json 依然是上一次失败/被放弃的重规划尝试留在磁盘
    上的内容（_build() 每次调用都无条件写盘，见 Rule 5/C9）——这份 props 从
    未真正用于渲染交付的视频，念出来的动画清单是假的。降级时必须返回 None，
    不能假装这些动画真的在成片里。"""
    if job.degraded_operations and "apply_style" in (json.loads(job.degraded_operations) or []):
        return None
    props_path = job.job_dir / "_op_apply_style_props.json"
    if not props_path.exists():
        return None
    try:
        props = json.loads(props_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    names: list[str] = []
    for c in props.get("countdowns") or []:
        names.append(f"Countdown ring — {c.get('headline') or str(c.get('value', ''))}")
    for c in props.get("calendarEvents") or []:
        names.append(f"Calendar reveal — {c.get('month')}/{c.get('targetDay')} {c.get('eventLabel', '')}".strip())
    for c in props.get("dataCards") or []:
        rows = ", ".join(str(r.get("label", "")) for r in (c.get("rows") or []))
        names.append(f"Count-up data card — {c.get('title') or rows}")
    for g in props.get("gauges") or []:
        names.append(f"Risk gauge — {g.get('title') or g.get('rightLabel', '')}")
    for b in props.get("beforeAfter") or []:
        names.append(f"Before/after comparison — {b.get('kicker', '')}")
    for s in props.get("stepLists") or []:
        names.append(f"{len(s.get('steps') or [])}-step process list — {s.get('title', '')}".strip(" —"))
    for tc in props.get("topicCards") or []:
        names.append(f"Topic card — {str(tc.get('headline', ''))[:30]}")
    for q in props.get("quotes") or []:
        names.append(f"Quote typography — {str(q.get('text', ''))[:30]}")
    for cc in props.get("cornerCards") or []:
        names.append(f"Corner app card — {cc.get('appName') or cc.get('variant', '')}")
    for cp in props.get("comparisons") or []:
        labels = " vs ".join(str(c.get("label", "")) for c in (cp.get("columns") or []))
        names.append(f"Side-by-side comparison — {cp.get('title') or labels}")
    for rl in props.get("rankedLists") or []:
        item_count = len(rl.get("items") or [])
        names.append(f"Ranked list — {rl.get('title') or f'{item_count} items'}")
    for cl in props.get("checklists") or []:
        item_count = len(cl.get("items") or [])
        names.append(f"Checklist — {cl.get('title') or f'{item_count} items'}")
    for lp in props.get("locationPins") or []:
        names.append(f"Location pin — {lp.get('place', '')}")
    for tm in props.get("testimonials") or []:
        names.append(f"Testimonial — {tm.get('name', '')}")
    for ic in props.get("iconClusters") or []:
        item_count = len(ic.get("items") or [])
        names.append(f"Icon cluster — {ic.get('title') or f'{item_count} items'}")
    for pb in props.get("progressBars") or []:
        names.append(f"Progress bar — {pb.get('label', '')}")
    for pcn in props.get("prosCons") or []:
        pc_fallback = f"{pcn.get('prosLabel', '')} vs {pcn.get('consLabel', '')}"
        names.append(f"Pros/cons — {pcn.get('title') or pc_fallback}")
    for mt in props.get("milestoneTracks") or []:
        stops = len(mt.get("milestones") or [])
        names.append(f"Milestone track — {mt.get('title') or f'{stops} stops'}")
    for tb in props.get("trustBadges") or []:
        badge_count = len(tb.get("badges") or [])
        names.append(f"Trust badge — {tb.get('title') or f'{badge_count} credentials'}")
    for bc in props.get("barCharts") or []:
        names.append(f"Bar chart — {bc.get('title', '')}")
    for mu in props.get("milestoneUnlocks") or []:
        names.append(f"Milestone unlock — {mu.get('label', '')}")
    for sec in props.get("sections") or []:
        if sec.get("timeline"):
            names.append(f"Multi-stage timeline — {sec['timeline'].get('heading', '')}")
        else:
            names.append(f"Full-canvas section — {sec.get('title', '')}")
    if props.get("intro"):
        names.append("Intro title card")
    if props.get("outro"):
        names.append("Outro CTA card")
    return names


@app.get("/jobs/{job_id}")
async def get_job_endpoint(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "animations": _animations_summary(job),
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
        # AI 生成累计花费（b-roll/背景音乐等）。Node 侧预览消息靠它如实告知
        # 用户/团队这单实际花了多少钱，不再是"哪儿都看不见"的隐性支出。
        "generation_cost_usd": job.generation_cost_usd or 0.0,
        # 发帖配文 + hashtag（social_caption.py 生成，可能为 None——没转写/
        # LLM 失败/超时都是正常的"没生成成功"，不是错误，Node 侧据此决定要不要
        # 发第二条"可直接复制粘贴"的消息）。命名为 talkinghead_social_caption
        # 而不是 social_caption——那个字段名已经被 social_batch.py（形状不同，
        # 纯字符串）占用了。
        "talkinghead_social_caption": json.loads(job.talkinghead_social_caption) if job.talkinghead_social_caption else None,
        # clip-factory 管线用——之前一直没往外暴露过，Node 侧要靠它判断
        # CLIPS_READY 时该不该走 clip 专属的展示逻辑。
        "pipeline": job.pipeline,
        "clips": _clips_summary(job) if job.pipeline == "clip-factory" else [],
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
                      error_message=None, degraded_operations=None, generation_cost_usd=None)
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
        JobStatus.CLIPS_READY,
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
    # 幂等：已经在重新规划中，直接返回，避免 Node 队列重试触发双跑
    if job.status == JobStatus.PLANNING:
        return {"job_id": job_id, "status": job.status.value}
    # 真实事故（2026-07-30，job_64f2d7dd56dd）：这个端点原来没有状态校验——
    # C-roll 生成期间（HeyGen 还没跑完，input.mp4 还不存在，job.status 还是
    # DOWNLOADING_MEDIA）用户又发来一条追加语音，被 Node 当成"对当前任务的
    # 修改意见"直接打到这里、触发重规划：读到的是根本不存在的视频，规划出
    # 一份"时长 0 秒"的假方案，还把 job 状态提前改成了 WAITING_CONFIRMATION。
    # 等 HeyGen 真正生成完成、process_incoming_message 跑出本该正确的方案时，
    # 已经被这次抢跑覆盖/污染，用户之后确认时 input_video_path 对不上真实
    # 文件，报"找不到输入视频"。跟 /retry、/confirm 一样补上状态校验：只有
    # 方案阶段或预览阶段才能修订，其余一律拒绝（Node 侧会在发起请求前用已
    # 经取到的 job 状态提前短路掉，不会真的打到这条 400）。
    if job.status not in (JobStatus.WAITING_CONFIRMATION, JobStatus.PREVIEW_READY):
        raise HTTPException(
            status_code=400,
            detail=f"Job is in {job.status.value}, cannot revise yet",
        )
    update_job_status(job_id, JobStatus.PLANNING)
    # 后台带反馈重规划，立即返回；Node 轮询等待新方案（WAITING_CONFIRMATION）
    _run_in_background(_enqueue_revise, job_id, text)
    return {"job_id": job_id, "status": "PLANNING"}


# ---------------------------------------------------------------------------
# Preview editor (Phase 2) — browser workspace for hand-editing render props.
#
# 跟 apply_style/revise_style 完全不同的一条渲染路径：不经过 plan_content、
# 不经过 _apply_deterministic_guarantees、不经过 props_lint/视觉复审——用户
# 编辑的内容就是最终结果（见 pipeline_runner.render_props_directly 的文档）。
# 这里只负责：token 鉴权、URL 改写（本机地址 <-> 公网地址）、并发合并
# （coalescing，同一 job 同时只跑一次渲染）、每小时保存次数上限。
# ---------------------------------------------------------------------------

_EDITOR_RENDER_MARKER_NAME = "_editor_render.json"
_EDITOR_SAVES_PER_HOUR = int(os.getenv("OM_EDITOR_SAVES_PER_HOUR", "12"))
_EDITOR_SAVE_WINDOW_S = 3600


def _require_editor_token(job_id: str, token: str):
    """校验预览编辑器链接的 token，失败一律 403（不区分"job 不存在"和"token
    不对"两种原因——job_id 本身也是要保密的信息，404 会向未认证的请求方
    泄露"这个 job 是否存在"）。"""
    from .editor_token import verify_token

    if not verify_token(job_id, token):
        raise HTTPException(status_code=403, detail="Invalid or expired editor token")
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=403, detail="Invalid or expired editor token")
    return job


def _rewrite_asset_url(url: Optional[str]) -> Optional[str]:
    """把 props 里指向 local_api_base（通常是 127.0.0.1，Remotion 渲染子进程
    取素材用）的资源 URL 换成 public_base_url（ngrok 隧道，浏览器/手机能
    解析）——不换的话 Player 会显示黑屏且不报任何错误，因为 <video> 加载
    失败在 UI 上就是"什么都没有"，不会抛异常。只做前缀替换：
    pipeline_runner.py 构造这些 URL 永远是 f"{local_api_base}/files/..."
    这个固定形状（videoSrc/presenter.src/qrContact.qrSrc 三处）。"""
    if not url:
        return url
    config = get_config()
    local_base = config.local_api_base.rstrip("/")
    if url.startswith(local_base):
        return config.public_base_url.rstrip("/") + url[len(local_base):]
    return url


def _rewrite_props_for_browser(props: dict, job=None) -> dict:
    """`job` optional only for backward-compat with any other caller; the
    editor `props` route always passes it so `videoSrc` can be swapped for a
    faststart copy — see `pipeline_runner.ensure_editor_preview_video`'s
    docstring for why every intermediate `.mp4` this pipeline produces is
    otherwise permanently black in a browser `<video>` element (moov atom at
    the end of the file, confirmed 70/70 on disk). This never touches the
    real `videoSrc` on disk: the returned dict is browser-facing only, and
    `pin_server_owned_props` re-pins the real value from disk on every save
    regardless of what the browser round-trips back."""
    props = dict(props)
    video_src = props.get("videoSrc")
    if job is not None:
        try:
            from .pipeline_runner import ensure_editor_preview_video
            preview_path = ensure_editor_preview_video(job)
            if preview_path is not None:
                base = get_config().local_api_base.rstrip("/")
                video_src = f"{base}/files/{job.id}/{preview_path.name}"
        except Exception:
            logger.warning("faststart 预览副本生成失败，退回原始 videoSrc", exc_info=True)
    props["videoSrc"] = _rewrite_asset_url(video_src)
    if props.get("presenter"):
        props["presenter"] = {**props["presenter"],
                              "src": _rewrite_asset_url(props["presenter"].get("src"))}
    if props.get("qrContact"):
        props["qrContact"] = {**props["qrContact"],
                              "qrSrc": _rewrite_asset_url(props["qrContact"].get("qrSrc"))}
    # Seed the editor's music-volume slider from the plan's own add_music op
    # — musicVolume isn't part of render_props on disk (it's an editor-only
    # override consumed by render_props_directly), so without this the
    # slider would start at some default instead of the actual level the
    # delivered video already has.
    if job is not None and "musicVolume" not in props:
        try:
            from .pipeline_runner import _load_plan
            plan = _load_plan(job)
            music_op = next((o for o in plan.get("edit_operations", []) if o.get("type") == "add_music"), None)
            if music_op is not None and isinstance(music_op.get("volume"), (int, float)):
                props["musicVolume"] = music_op["volume"]
        except Exception:
            logger.warning("musicVolume 预填失败，编辑器滑块回落到默认值", exc_info=True)
    return props


def _read_editor_marker(job_dir) -> dict:
    path = job_dir / _EDITOR_RENDER_MARKER_NAME
    default = {"state": "idle", "pending_props": None, "started_at": None,
               "error": None, "save_timestamps": [],
               # Phase 8 —— Arm B 的手动编辑（overrides）版本，跟 pending_props
               # 共用同一个标记文件/state machine（一个 job 同时只可能是 Arm A
               # 或 Arm B 中的一种，两个字段不会同时有值）。
               "pending_overrides": None}
    if not path.exists():
        return default
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
        return {**default, **marker}
    except Exception:
        return default


def _write_editor_marker(job_dir, marker: dict) -> None:
    (job_dir / _EDITOR_RENDER_MARKER_NAME).write_text(
        json.dumps(marker, ensure_ascii=False), encoding="utf-8")


@app.get("/editor/{job_id}/props")
def editor_get_props(job_id: str, token: str = Query("")):
    # Plain `def`, not `async def` — this now (indirectly, via
    # _rewrite_props_for_browser -> ensure_editor_preview_video) calls
    # ffmpeg through subprocess.run, which blocks. Declared async it would
    # freeze FastAPI's single event loop for every concurrent request behind
    # it, the same class of bug serve_file's own docstring documents as a
    # confirmed real incident. It was already doing blocking DB/file I/O
    # before this change too.
    job = _require_editor_token(job_id, token)
    props_path = job.job_dir / "_op_apply_style_props.json"
    if not props_path.exists():
        raise HTTPException(status_code=404, detail="This job has no styled render to edit yet")
    try:
        props = json.loads(props_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read props: {e}")

    marker = _read_editor_marker(job.job_dir)
    return {
        "props": _rewrite_props_for_browser(props, job),
        "job_status": job.status.value,
        "manually_edited": (job.job_dir / "_manual_edit.json").exists(),
        "editor_state": marker["state"],
    }


@app.post("/editor/{job_id}/props")
async def editor_post_props(job_id: str, request: Request, token: str = Query("")):
    job = _require_editor_token(job_id, token)
    user_props = await request.json()
    if not isinstance(user_props, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    marker = _read_editor_marker(job.job_dir)
    now = time.time()
    recent_saves = [t for t in marker["save_timestamps"] if now - t < _EDITOR_SAVE_WINDOW_S]
    if len(recent_saves) >= _EDITOR_SAVES_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=f"Editor save rate limit reached ({_EDITOR_SAVES_PER_HOUR}/hour) — try again later",
        )
    recent_saves.append(now)

    # 合并（coalesce），不排队：一个渲染已经在跑时，新的保存直接替换
    # pending_props 并返回，不再触发第二份并发渲染——RENDER_SLOTS 全局只有
    # 一个槽位，排队会让第二次保存等最多 OM_RENDER_TIMEOUT_S（默认 1800s）。
    # editor_render（worker.py）渲染完成后会检查 pending_props 有没有被
    # 刷新过，有就接着渲染最新这份，没有才真正结束。
    was_rendering = marker["state"] == "rendering"
    marker["pending_props"] = user_props
    marker["save_timestamps"] = recent_saves
    if not was_rendering:
        marker["state"] = "rendering"
        marker["started_at"] = now
        marker["error"] = None
    _write_editor_marker(job.job_dir, marker)

    wa_number = job.user.whatsapp_id if job.user else None

    if was_rendering:
        return {"job_id": job_id, "state": "coalesced", "wa_number": wa_number}

    # 跟 /revise_style 同一个race 修复：必须在 _run_in_background 之前同步
    # 落地状态，否则 Node 发完 POST 立刻开始轮询 GET /jobs/{id}，如果这里
    # 还是 PREVIEW_READY，会把旧预览误当成新预览投递出去。
    update_job_fields(job_id, status=JobStatus.RUNNING_PIPELINE, error_message=None,
                      progress_stage=None)
    _run_in_background(_enqueue_editor_render, job_id)
    return {"job_id": job_id, "state": "queued", "wa_number": wa_number}


# ---------------------------------------------------------------------------
# Phase 8 —— Arm B（AI 现写场景）的编辑器数据源，跟上面 Arm A 的 /props、
# /props 保存并列。一个 job 只会是 Arm A 或 Arm B 中的一种（arm_router 决定），
# 靠 job_dir/authored/scene.tsx 是否存在来判断走哪一条——跟 Arm A 自己靠
# _op_apply_style_props.json 是否存在判断"这个 job 有没有走过 apply_style"
# 完全同一个思路，不需要新的 DB 字段。
# ---------------------------------------------------------------------------

@app.get("/editor/{job_id}/authored")
def editor_get_authored(job_id: str, token: str = Query("")):
    # Plain `def`（跟 editor_get_props 同一个理由）——这里只做文件 I/O，
    # 没有 ffmpeg 子进程，但保持跟同类路由一致的风格。
    job = _require_editor_token(job_id, token)
    authored_dir = job.job_dir / "authored"
    scene_path = authored_dir / "scene.tsx"
    if not scene_path.exists():
        raise HTTPException(status_code=404, detail="This job has no AI-authored scene to edit")

    def _read_json_or(path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    try:
        tsx = scene_path.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read authored scene: {e}")
    manifest = _read_json_or(authored_dir / "manifest.json", [])
    overrides = _read_json_or(authored_dir / "overrides.json", {})
    base_props = _read_json_or(authored_dir / "props.json", {})

    # Phase 8 — the editor's in-browser live compile can fail (arbitrary
    # per-job generated code, however unlikely after tsx_validator.py's
    # gate). previewUrl lets it fall back to the already-rendered mp4
    # instead of a blank screen — the same file WhatsApp delivery itself
    # uses, so it's guaranteed to exist whenever this route doesn't 404.

    # base_props 里的 videoSrc/broll[].src 是**相对 job_dir** 的路径（见
    # whatsapp_mvp/authored/__init__.py 落 props.json 时的约定），这里拼成
    # 浏览器能 fetch 的 URL——跟 _rewrite_props_for_browser 对 Arm A 的
    # videoSrc/presenter.src/qrContact.qrSrc 做的事完全同一个理由（本机地址
    # 浏览器/手机解析不了，见该函数自己的注释）。serve_file（GET
    # /files/{job_id}/{filename}）本来就已经能读 job_dir 下任意路径，不需要
    # 新增文件服务路由。
    local_base = get_config().local_api_base.rstrip("/")
    video_src_rel = base_props.get("videoSrc") or ""
    base_props["videoSrc"] = (
        _rewrite_asset_url(f"{local_base}/files/{job_id}/{video_src_rel}") if video_src_rel else ""
    )
    base_props["broll"] = [
        {**b, "src": _rewrite_asset_url(f"{local_base}/files/{job_id}/{b['src']}") if b.get("src") else ""}
        for b in (base_props.get("broll") or [])
    ]
    preview_url = (
        _rewrite_asset_url(f"{local_base}/files/{job_id}/preview.mp4")
        if (job.job_dir / "preview.mp4").exists() else None
    )

    marker = _read_editor_marker(job.job_dir)
    return {
        "tsx": tsx,
        "manifest": manifest,
        "previewUrl": preview_url,
        "overrides": overrides,
        **base_props,
        "job_status": job.status.value,
        "editor_state": marker["state"],
    }


@app.post("/editor/{job_id}/overrides")
async def editor_post_overrides(job_id: str, request: Request, token: str = Query("")):
    """跟 editor_post_props 同一套合并（coalescing）/ 每小时限额机制，只是
    存的是 pending_overrides（Arm B 手动编辑层）而不是 pending_props（Arm A
    完整 render_props 树）——两者共用同一份标记文件，但 job 只会走其中一条。"""
    job = _require_editor_token(job_id, token)
    user_overrides = await request.json()
    if not isinstance(user_overrides, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    if not (job.job_dir / "authored" / "scene.tsx").exists():
        raise HTTPException(status_code=404, detail="This job has no AI-authored scene to edit")

    marker = _read_editor_marker(job.job_dir)
    now = time.time()
    recent_saves = [t for t in marker["save_timestamps"] if now - t < _EDITOR_SAVE_WINDOW_S]
    if len(recent_saves) >= _EDITOR_SAVES_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=f"Editor save rate limit reached ({_EDITOR_SAVES_PER_HOUR}/hour) — try again later",
        )
    recent_saves.append(now)

    was_rendering = marker["state"] == "rendering"
    marker["pending_overrides"] = user_overrides
    marker["save_timestamps"] = recent_saves
    if not was_rendering:
        marker["state"] = "rendering"
        marker["started_at"] = now
        marker["error"] = None
    _write_editor_marker(job.job_dir, marker)

    wa_number = job.user.whatsapp_id if job.user else None

    if was_rendering:
        return {"job_id": job_id, "state": "coalesced", "wa_number": wa_number}

    update_job_fields(job_id, status=JobStatus.RUNNING_PIPELINE, error_message=None,
                      progress_stage=None)
    _run_in_background(_enqueue_editor_render_authored, job_id)
    return {"job_id": job_id, "state": "queued", "wa_number": wa_number}


@app.post("/editor/{job_id}/relayout")
async def editor_relayout(job_id: str, request: Request, token: str = Query("")):
    """非破坏性的"重新走一遍自动排版"预览——用户在浏览器里看不出
    mode_schedule 冲突（哪张卡会被正在长大的说话人卡片挡住），这是唯一一个
    用户自己肉眼算不出来、需要引擎帮忙的保障。只计算、只返回，不写盘、不
    渲染、不影响 pending_props/render 状态。"""
    job = _require_editor_token(job_id, token)
    user_props = await request.json()
    if not isinstance(user_props, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    from .pipeline_runner import _recompute_scenes_from_content

    try:
        duration_frames = max(1, round(float(user_props.get("durationSeconds") or 0) * 30))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="durationSeconds must be a number")
    relaid = _recompute_scenes_from_content(copy.deepcopy(user_props), duration_frames)
    return {"props": relaid}


@app.get("/editor/{job_id}/status")
async def editor_status(job_id: str, token: str = Query("")):
    job = _require_editor_token(job_id, token)
    marker = _read_editor_marker(job.job_dir)
    now = time.time()
    recent_saves = [t for t in marker["save_timestamps"] if now - t < _EDITOR_SAVE_WINDOW_S]

    from .concurrency import RENDER_SLOTS

    return {
        "state": marker["state"],
        # threading.Semaphore 没有公开的"当前是否被占满"查询接口——_value 是
        # 内部实现细节，但这里只用于给用户体验提示（"机器正忙，可能要等一会
        # 儿"），不是任何正确性判断的依据，best-effort 探测可以接受。
        "slot_busy": getattr(RENDER_SLOTS, "_value", 1) <= 0,
        "error": marker["error"],
        "saves_this_hour": len(recent_saves),
        "job_status": job.status.value,
    }


@app.get("/editor/{job_id}/filmstrip")
def editor_filmstrip(job_id: str, token: str = Query("")):
    # Plain `def`, not `async def` — this calls ffmpeg via subprocess.run,
    # which blocks. Declared async it would freeze FastAPI's single event
    # loop for every concurrent request behind it, the same class of bug
    # `serve_file`'s own docstring documents as a confirmed real incident.
    job = _require_editor_token(job_id, token)
    from .pipeline_runner import ensure_editor_filmstrip

    paths = ensure_editor_filmstrip(job)
    base = get_config().local_api_base.rstrip("/")
    urls = [_rewrite_asset_url(f"{base}/files/{job_id}/{p.name}") for p in paths]
    return {"thumbnails": urls}


@app.get("/editor/{job_id}/waveform")
def editor_waveform(job_id: str, token: str = Query("")):
    job = _require_editor_token(job_id, token)
    from .pipeline_runner import ensure_editor_waveform

    path = ensure_editor_waveform(job)
    if path is None:
        return {"waveform": None}
    base = get_config().local_api_base.rstrip("/")
    return {"waveform": _rewrite_asset_url(f"{base}/files/{job_id}/{path.name}")}


@app.post("/jobs/{job_id}/editor_token")
async def create_editor_token_endpoint(job_id: str):
    """给 Node 网关用：预览就绪时调一次，拿一条可以直接发给用户的编辑器
    链接。刻意不放进 GET /jobs/{id}——那个端点没有鉴权，Node 每几秒轮询
    一次，token 会被写进每一行访问日志。"""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    from .editor_token import EditorTokenError, make_token

    try:
        token = make_token(job_id)
    except EditorTokenError as e:
        raise HTTPException(status_code=500, detail=str(e))
    config = get_config()
    editor_url = f"{config.public_base_url.rstrip('/')}/editor/{job_id}?token={token}"
    return {"editor_url": editor_url}


# ---------------------------------------------------------------------------
# File serving
# ---------------------------------------------------------------------------

@app.get("/files/{job_id}/{filename}")
def serve_file(job_id: str, filename: str):
    # Deliberately a plain `def`, not `async def`: get_job() is a blocking
    # SQLAlchemy query (a JOIN across user+messages), and this route is the
    # one Remotion's renderer hits repeatedly and CONCURRENTLY (6-way tab
    # concurrency) while seeking through the source video during a render.
    # As `async def` it shared FastAPI's single event-loop thread, so one
    # blocking DB call froze every concurrent fetch behind it — confirmed
    # live as the actual cause of Remotion's repeated "server sent no data
    # for 20 seconds" / proxy 500 failures across multiple real jobs, at many
    # different timestamps (not tied to any one frame). A plain `def` makes
    # FastAPI run each call in its own threadpool thread automatically, so
    # concurrent requests no longer serialize on one blocked thread.
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # Containment check — {filename} compiles to [^/]+ (no forward slash), but
    # that does NOT block a backslash, and on Windows pathlib treats \ as a
    # separator too. filename="..\\..\\..\\.env" resolves clean outside
    # job_dir and was confirmed live to read this repo's real .env (API keys)
    # and the sqlite DB (every user's WhatsApp number) through this route,
    # unauthenticated, reachable through the ngrok tunnel. resolve() collapses
    # ../, both separator styles, and 8.3 short names; the parents check is
    # the actual containment assertion (stays correct for a future nested
    # path like assets/broll_0.mp4, unlike a substring/startswith check).
    job_root = job.job_dir.resolve()
    try:
        file_path = (job_root / filename).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="File not found")
    if not file_path.is_file() or job_root not in file_path.parents:
        raise HTTPException(status_code=404, detail="File not found")

    if filename.endswith(".mp4"):
        media_type = "video/mp4"
    elif filename.endswith(".jpg") or filename.endswith(".jpeg"):
        media_type = "image/jpeg"  # social_batch.py 的 Feed 静态图变体
    elif filename.endswith(".png"):
        media_type = "image/png"
    elif filename.endswith(".mp3"):
        media_type = "audio/mpeg"  # voice_clone.py 合成的克隆音色音频，HeyGen 靠这个 URL 抓取
    else:
        media_type = "application/octet-stream"
    return FileResponse(str(file_path), media_type=media_type)
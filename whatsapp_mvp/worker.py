# WhatsApp MVP - Worker (RQ job functions)

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import get_config
from .database import JobStatus, MessageDirection, MessageType
from .job_manager import (
    get_job,
    save_message,
    update_job_fields,
    update_job_status,
)
from .whatsapp_client import WhatsAppClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 主入口：处理收到的 WhatsApp 消息
# ---------------------------------------------------------------------------

def process_incoming_message(job_id: str) -> None:
    """处理收到的 WhatsApp 消息任务的入口点。

    流程: 下载媒体 → LLM 规划 → 发送确认
    """
    logger.info(f"Worker: 开始处理任务 {job_id}")
    job = get_job(job_id)
    if job is None:
        logger.error(f"任务 {job_id} 不存在")
        return

    config = get_config()
    wa = WhatsAppClient(config)

    try:
        # ── 步骤1: 下载视频 ──
        if not job.input_video_path and job.whatsapp_media_id:
            _download_media(job, wa)
            job = get_job(job_id)  # 重新加载，获取最新状态

        # ── 步骤2: LLM 规划 ──
        # 零指令（视频不带文字）同样进 L2 规划：SYSTEM_BASE 里已定义默认方案
        # （remove_filler → apply_style 出模板成片），不再让无文字任务卡死在这一步。
        if not job.planned_edit:
            _run_llm_planner(job)
            job = get_job(job_id)  # 重新加载，获取最新状态

        # ── 步骤3: 发送确认消息 ──
        if job.planned_edit and job.status == JobStatus.PLANNING:
            _send_confirmation(job, wa)

    except Exception as e:
        logger.exception(f"处理任务 {job_id} 时出错: {e}")
        update_job_status(job_id, JobStatus.ERROR, str(e))
        _safe_send(
            wa,
            job.user.whatsapp_id,
            f"抱歉，处理您的视频时出现了问题，请重新上传。\n（错误: {str(e)[:100]}）",
        )


def run_pipeline(job_id: str) -> None:
    """运行 OpenMontage talking-head 管线（用户确认后）。"""
    logger.info(f"Worker: 运行管线 {job_id}")
    job = get_job(job_id)
    if job is None:
        return

    try:
        update_job_status(job_id, JobStatus.RUNNING_PIPELINE)

        from .pipeline_runner import run_talking_head_pipeline

        result = run_talking_head_pipeline(job)

        if result.get("preview_path"):
            update_job_fields(
                job_id,
                preview_path=result["preview_path"],
                status=JobStatus.PREVIEW_READY,
            )
            config = get_config()
            wa = WhatsAppClient(config)
            preview_url = f"{config.public_base_url}/files/{job_id}/preview.mp4"
            _safe_send(
                wa,
                job.user.whatsapp_id,
                f"预览已生成！\n{preview_url}\n\n"
                "回复 'export' 导出最终视频，或回复 'retry' 重新编辑。",
            )

    except Exception as e:
        logger.exception(f"管线运行出错 {job_id}: {e}")
        update_job_status(job_id, JobStatus.ERROR, str(e))


def run_final_render(job_id: str) -> None:
    """运行最终导出（用户确认预览后）。"""
    logger.info(f"Worker: 最终导出 {job_id}")
    job = get_job(job_id)
    if job is None:
        return

    try:
        update_job_status(job_id, JobStatus.RENDERING)

        from .pipeline_runner import run_final_export

        result = run_final_export(job)

        if result.get("final_path"):
            update_job_fields(
                job_id,
                final_path=result["final_path"],
                status=JobStatus.DONE,
            )
            config = get_config()
            wa = WhatsAppClient(config)
            final_url = f"{config.public_base_url}/files/{job_id}/final.mp4"
            _safe_send(
                wa,
                job.user.whatsapp_id,
                f"最终视频已生成！\n{final_url}",
            )

    except Exception as e:
        logger.exception(f"最终导出出错 {job_id}: {e}")
        update_job_status(job_id, JobStatus.ERROR, str(e))


def revise_plan(job_id: str, feedback: str) -> None:
    """就地修订：带用户反馈 + 上一版方案重新规划，回到 WAITING_CONFIRMATION。"""
    logger.info(f"就地修订 {job_id}: {feedback[:80]}")
    job = get_job(job_id)
    if job is None:
        return

    config = get_config()
    wa = WhatsAppClient(config)
    try:
        try:
            prev = json.loads(job.planned_edit) if job.planned_edit else {}
        except (json.JSONDecodeError, TypeError):
            prev = {}
        history = [(prev.get("edit_operations", []), prev.get("summary", ""), feedback)]

        input_path = job.job_dir / "input.mp4"
        video_path = str(input_path) if input_path.exists() else None
        transcript = []
        try:
            from .pipeline_runner import transcribe_segments

            if input_path.exists():
                transcript = transcribe_segments(str(input_path), job.job_dir)
        except Exception:
            transcript = []
        try:
            from .agent_editor import plan_video

            new_plan = plan_video(job.edit_request, video_path, history=history,
                                  transcript=transcript)
        except Exception as e:
            logger.warning(f"L2 修订规划失败，回退 L1.5: {e}")
            from .llm_planner import plan_edit

            new_plan = plan_edit(f"{job.edit_request}。补充：{feedback}")

        update_job_fields(
            job_id,
            edit_request=f"{job.edit_request}。补充：{feedback}",
            planned_edit=json.dumps(new_plan, ensure_ascii=False),
        )
        job = get_job(job_id)
        _send_confirmation(job, wa)  # 设 WAITING_CONFIRMATION 或 NEEDS_CLARIFICATION
    except Exception as e:
        logger.exception(f"修订出错 {job_id}: {e}")
        update_job_status(job_id, JobStatus.ERROR, str(e))


# ---------------------------------------------------------------------------
# Phase 3: 下载 WhatsApp 视频
# ---------------------------------------------------------------------------

def _download_media(job: Any, wa: WhatsAppClient) -> None:
    """从 WhatsApp 下载视频媒体到本地存储。"""
    media_id = job.whatsapp_media_id
    if not media_id:
        raise ValueError(f"任务 {job.id} 没有 media_id，无法下载")

    logger.info(f"下载媒体 {media_id} → {job.job_dir}")
    update_job_status(job.id, JobStatus.DOWNLOADING_MEDIA)

    # 确保目录存在
    job.job_dir.mkdir(parents=True, exist_ok=True)

    # 下载到 input.mp4
    dest = str(job.job_dir / "input.mp4")
    wa.download_media(media_id, dest)

    update_job_fields(job.id, input_video_path=dest)
    logger.info(f"媒体下载完成: {dest}")


# ---------------------------------------------------------------------------
# Phase 4: LLM 意图规划
# ---------------------------------------------------------------------------

def _script_stage(job: Any, input_path: Path) -> list:
    """Script 阶段：转录原始视频，产出并保存 script artifact，返回转录段供规划使用。

    失败不致命——返回空列表，规划照常进行（只是没有转录感知）。
    """
    if not input_path.exists():
        return []
    try:
        from .pipeline_runner import (
            _probe_duration,
            build_script_artifact,
            transcribe_segments,
            validate_artifact,
        )

        transcript = transcribe_segments(str(input_path), job.job_dir)
        if not transcript:
            return []
        duration = _probe_duration(input_path)
        script = build_script_artifact(transcript, duration)
        ok, err = validate_artifact(script, "script.schema.json")
        (job.job_dir / "script.json").write_text(
            json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(
            f"Script 阶段: {len(transcript)} 段, script.json 校验="
            + ("通过" if ok else ("跳过" if ok is None else f"未通过({err})"))
        )
        return transcript
    except Exception as e:
        logger.warning(f"Script 阶段失败（继续无转录规划）: {e}")
        return []


def _run_llm_planner(job: Any) -> None:
    """用 L2 agent 规划编辑方案（读 manifest/skill + tool-calling + 自审 + schema 校验）。

    agent 出错时回退到 L1.5 关键词/结构化规划器，保证任务不中断。
    """
    # 零指令：给 agent 一句明确的默认需求描述，而不是空串（SYSTEM_BASE 里
    # 定义了零指令的默认方案：remove_filler → apply_style）
    request = job.edit_request or "（用户没有文字指令）按默认方案出一条模板成片"
    logger.info(f"Agent(L2) 规划: {request[:100]}...")
    update_job_status(job.id, JobStatus.PLANNING)

    input_path = job.job_dir / "input.mp4"
    video_path = str(input_path) if input_path.exists() else None

    # Script 阶段：转录原始视频 + 产出 script artifact，转录喂给规划做转录感知剪辑
    transcript = _script_stage(job, input_path)

    try:
        from .agent_editor import plan_video

        plan = plan_video(request, video_path, transcript=transcript)
    except Exception as e:
        logger.warning(f"L2 agent 规划失败，回退 L1.5: {e}")
        from .llm_planner import plan_edit

        duration = None
        try:
            from .pipeline_runner import _probe_duration

            if input_path.exists():
                duration = _probe_duration(input_path) or None
        except Exception:
            duration = None
        plan = plan_edit(job.edit_request, video_duration=duration)

    update_job_fields(job.id, planned_edit=json.dumps(plan, ensure_ascii=False))
    logger.info(f"规划完成: {json.dumps(plan, ensure_ascii=False)[:200]}")


# ---------------------------------------------------------------------------
# Phase 5: 发送确认消息
# ---------------------------------------------------------------------------

def _send_confirmation(job: Any, wa: WhatsAppClient) -> None:
    """向用户发送编辑计划确认消息，等待批准。"""
    try:
        plan = json.loads(job.planned_edit)
    except (json.JSONDecodeError, TypeError):
        plan = {"summary": "编辑计划已生成", "edit_operations": []}

    # 检查是否需要澄清 → 进入 NEEDS_CLARIFICATION，由 Node 把问题发给用户、等用户回复
    if plan.get("clarification_needed"):
        question = plan.get("clarification_question", "请提供更多信息")
        _safe_send(wa, job.user.whatsapp_id, f"需要确认：{question}")
        update_job_status(job.id, JobStatus.NEEDS_CLARIFICATION)
        logger.info(f"任务 {job.id} 需要澄清: {question}")
        return

    # 构建确认消息
    summary = plan.get("summary", "编辑计划已生成")
    operations = plan.get("edit_operations", [])

    msg_lines = [
        f"*视频编辑计划* 📋",
        f"",
        f"{summary}",
        f"",
    ]
    if operations:
        msg_lines.append("*将执行以下操作：*")
        for i, op in enumerate(operations, 1):
            desc = op.get("description", op.get("type", "未知操作"))
            msg_lines.append(f"  {i}. {desc}")

    msg_lines.extend([
        "",
        "回复 *confirm* 开始编辑",
        "回复 *cancel* 取消",
    ])

    message = "\n".join(msg_lines)
    _safe_send(wa, job.user.whatsapp_id, message)

    # 保存确认消息记录
    save_message(job.id, MessageDirection.OUTBOUND, MessageType.TEXT, message)

    # 设置状态为等待确认
    update_job_status(job.id, JobStatus.WAITING_CONFIRMATION)
    logger.info(f"确认消息已发送，任务 {job.id} 进入 WAITING_CONFIRMATION")

    # 记录作业日志
    save_message(
        job.id,
        MessageDirection.OUTBOUND,
        MessageType.TEXT,
        f"[系统] 编辑计划: {json.dumps(plan, ensure_ascii=False)}",
    )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _safe_send(wa: WhatsAppClient, to: str, text: str) -> None:
    """安全发送 WhatsApp 消息，忽略网络错误。

    网关模式下真正的用户消息由 Node worker 发送；Python 侧的收件人是占位的
    'api_user'，发它必然失败，直接跳过以消除无害的 400 噪音。
    """
    if not to or to == "api_user":
        return
    try:
        wa.send_text_message(to, text)
    except Exception as e:
        logger.warning(f"发送 WhatsApp 消息失败: {e}")


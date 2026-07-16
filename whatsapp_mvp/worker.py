# WhatsApp MVP - Worker (RQ job functions)

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

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
            _download_broll_assets(job, wa)  # b-roll 资产（有就下，无则 no-op）
            job = get_job(job_id)  # 重新加载，获取最新状态

        # ── 步骤2: LLM 规划 ──
        # 零指令（视频不带文字）同样进 L2 规划：SYSTEM_BASE 定义了默认方案
        # （remove_filler → apply_style 出模板成片），无文字任务不再卡死在这一步。
        if not job.planned_edit:
            _run_llm_planner(job, wa)
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

        # 进度预览：确认后到预览生成之间可能较久（剪辑 + b-roll 合成 + 渲染），先回一条
        _safe_send(WhatsAppClient(get_config()), job.user.whatsapp_id,
                   "开始剪辑与合成，正在生成预览…（含 b-roll 合成时会稍久）")

        from .pipeline_runner import run_talking_head_pipeline

        result = run_talking_head_pipeline(job)

        if result.get("preview_path"):
            update_job_fields(
                job_id,
                preview_path=result["preview_path"],
                status=JobStatus.PREVIEW_READY,
                # 持久化降级信息（没降级也要写空列表：覆盖上一轮 retry 的旧值）。
                # Node 网关靠 GET /jobs 读它、在预览消息里如实告知用户——下面
                # _safe_send 的提醒在网关模式下是死代码，用户实际看不到。
                degraded_operations=json.dumps(result.get("degraded_operations") or []),
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

            # 优雅降级提示：某些非致命步骤失败被跳过（见 pipeline_runner 的
            # _DEGRADABLE_OPS），已交付上一步结果——把这件事显性告诉用户，
            # 不让"样式没成"变成沉默失败（对齐 AGENT_GUIDE：runtime 警告要 surface）。
            degraded = result.get("degraded_operations") or []
            if degraded:
                labels = {"apply_style": "品牌样式渲染"}
                names = "、".join(labels.get(op, op) for op in degraded)
                _safe_send(
                    wa,
                    job.user.whatsapp_id,
                    f"提醒：{names}这一步没成功，已先把剪辑好的版本发你（其余编辑已完成）。"
                    "可回复 'retry' 重试。",
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
# C-roll：照片 -> AI 文案 -> HeyGen 数字人说话视频 -> 接入常规剪辑管线
# ---------------------------------------------------------------------------

def generate_croll(job_id: str, photo_path: str, lang: str = "zh", hint: str = "") -> None:
    """POST /croll 的后台编排：看图写文案 -> HeyGen 上传/生成/轮询下载 ->
    把成品当 input.mp4 接入 process_incoming_message，从这一步起跟普通视频
    任务走的是完全同一条路（转写/L2 规划/confirm/apply_style/add_music 等
    一个都不用改）。任何一步失败都落 ERROR + 具体原因，不留在中间状态卡死。
    """
    logger.info(f"Worker: 开始生成 C-roll {job_id}")
    job = get_job(job_id)
    if job is None:
        return

    try:
        from . import heygen_croll
        from .croll_script import write_script

        if not heygen_croll.is_available():
            raise RuntimeError("HEYGEN_API_KEY 未配置，C-roll 功能不可用")

        script = write_script(photo_path, lang=lang, hint=hint)
        if not script:
            raise RuntimeError("看图写文案失败（视觉 LLM 不可用或未返回内容）")
        logger.info(f"  C-roll 文案（{job_id}）: {script[:80]}")

        talking_photo_id = heygen_croll.upload_talking_photo(Path(photo_path))
        if not talking_photo_id:
            raise RuntimeError("HeyGen 照片上传失败")

        video_id = heygen_croll.generate_talking_video(talking_photo_id, script, lang=lang)
        if not video_id:
            raise RuntimeError("HeyGen 视频生成提交失败")

        input_path = job.job_dir / "input.mp4"
        ok = heygen_croll.poll_and_download(video_id, input_path, timeout_s=300)
        if not ok:
            raise RuntimeError("HeyGen 视频生成超时或失败")

        duration = heygen_croll.probe_duration_seconds(input_path)
        cost = heygen_croll.estimate_cost(duration)
        if cost:
            from .pipeline_runner import _record_generation_cost
            _record_generation_cost(job.job_dir, "croll_heygen", cost)
        logger.info(f"  C-roll 视频生成完成（{job_id}）: {duration:.1f}s, ${cost}")

        # edit_request 是给 L2 剪辑规划器的"编辑指令"，跟 script（数字人要说的话）
        # 是两码事——之前这里直接把 script 塞进 edit_request 是个真实 bug：会让
        # 剪辑规划器把口播文案当成编辑指令去理解，而不是按零指令默认（remove_filler
        # + apply_style）处理。这里应该用用户当初给的方向提示（可能是空的）。
        update_job_fields(
            job_id,
            edit_request=hint or "",
            input_video_path=str(input_path),
        )

        # 从这里起完全复用普通视频任务的路径：转写 + L2 规划 + 发确认消息
        # （Node 网关模式下 _safe_send 是死代码，但 Node 本来就轮询 GET
        # /jobs/{id} 拿状态，不依赖这条直发路径）。
        process_incoming_message(job_id)

    except Exception as e:
        logger.exception(f"C-roll 生成出错 {job_id}: {e}")
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


def _download_broll_assets(job: Any, wa: WhatsAppClient) -> None:
    """下载所有 b-roll 资产到 job_dir/assets/；单个失败只跳过该资产（b-roll 非必需，不拖垮 job）。"""
    from .job_manager import get_assets, set_asset_local_path

    broll = [a for a in get_assets(job) if a.get("role") == "broll"]
    if not broll:
        return
    asset_dir = job.job_dir / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    for a in broll:
        media_id = a.get("media_id")
        if not media_id or a.get("local_path"):
            continue
        ext = "jpg" if a.get("kind") == "image" else "mp4"
        dest = str(asset_dir / f"broll_{a.get('order', 0)}.{ext}")
        try:
            wa.download_media(media_id, dest)
            set_asset_local_path(job.id, media_id, dest)
            logger.info(f"b-roll 资产下载完成: {dest}")
        except Exception as e:
            logger.warning(f"b-roll 资产 {media_id} 下载失败，跳过: {e}")


# ---------------------------------------------------------------------------
# Phase 4: LLM 意图规划
# ---------------------------------------------------------------------------

def _source_review_stage(job: Any, input_path: Path) -> Optional[dict]:
    """source_media_review(OpenMontage 标准件):审查用户上传素材,产出并保存 artifact。

    AGENT_GUIDE 契约:有用户上传素材时,创作性规划之前必须先做 source_media_review。
    直接复用 lib/source_media_review.py(technical probe + 代表帧 + 质量风险 + 转写),
    不自造。失败不致命——返回 None,规划照常进行。
    """
    if not input_path.exists():
        return None
    try:
        from lib.source_media_review import review_source_media
        from tools.tool_registry import registry

        registry.ensure_discovered()
        art = review_source_media(
            [input_path],
            {"pipeline_type": "talking-head", "project_dir": str(job.job_dir)},
            registry,
        )
        (job.job_dir / "source_media_review.json").write_text(
            json.dumps(art, ensure_ascii=False, indent=2), encoding="utf-8")

        from .pipeline_runner import validate_artifact

        ok, err = validate_artifact(art, "source_media_review.schema.json")
        logger.info(
            f"source_media_review: {art.get('summary', '')[:120]} | schema="
            + ("通过" if ok else ("跳过" if ok is None else f"未过({err})"))
        )
        return art
    except Exception as e:
        logger.warning(f"source_media_review 失败(继续无素材审查): {e}")
        return None


def _source_facts(art: Optional[dict]) -> str:
    """把 source_media_review 提炼成给 agent 的一段事实(分辨率/时长/音频/质量风险)。"""
    if not art:
        return ""
    lines = [art.get("summary", "")]
    for f in art.get("files", []):
        tp = f.get("technical_probe", {})
        if tp:
            lines.append(
                f"技术参数: {tp.get('resolution', '?')}, {tp.get('fps', '?')}fps, "
                f"{tp.get('duration_seconds', 0):.1f}s, 音频={tp.get('audio_codec') or '无'}"
            )
    impl = art.get("planning_implications", [])
    if impl:
        lines.append("素材提示: " + "；".join(impl[:4]))
    return "\n".join(x for x in lines if x)


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


def _run_llm_planner(job: Any, wa: Any = None) -> None:
    """用 L2 agent 规划编辑方案（读 manifest/skill + tool-calling + 自审 + schema 校验）。

    agent 出错时回退到 L1.5 关键词/结构化规划器，保证任务不中断。
    传入 wa 时会在转录/规划两个较慢阶段前回传进度消息（进度预览）。
    """
    # 零指令：给 agent 一句明确的默认需求描述而不是空串（SYSTEM_BASE 定义了
    # 零指令默认方案：remove_filler → apply_style）
    request = job.edit_request or "（用户没有文字指令）按默认方案出一条模板成片"
    logger.info(f"Agent(L2) 规划: {request[:100]}...")
    update_job_status(job.id, JobStatus.PLANNING)

    input_path = job.job_dir / "input.mp4"
    video_path = str(input_path) if input_path.exists() else None

    # source_media_review(AGENT_GUIDE 契约:有用户素材,创作性规划前必先做)
    review = _source_review_stage(job, input_path)
    source_facts = _source_facts(review)

    # b-roll 事实块：把收集到的 b-roll 素材(编号+类型+标签)喂给 L2，让它按"标签↔转录"匹配放置。
    # 没有 b-roll 就不加这块 —— L2 的 SYSTEM_BASE 规定只有事实里列了才 emit insert_broll。
    from .job_manager import get_assets
    broll = [a for a in get_assets(job) if a.get("role") == "broll"]
    if broll:
        lines = ["用户上传的 b-roll 素材（上传并附说明＝明确要求你按说明把它们插进成片，必须处理，不是可选项）。逐段 emit insert_broll，asset_ref 用下面的编号："]
        for a in broll:
            lines.append(f"[b{a.get('order')}] 类型={a.get('kind')} 说明=\"{a.get('label') or '(无说明)'}\"")
        block = "\n".join(lines)
        source_facts = (source_facts + "\n\n" + block) if source_facts else block

    # Script 阶段：转录原始视频 + 产出 script artifact，转录喂给规划做转录感知剪辑
    if wa:
        _safe_send(wa, job.user.whatsapp_id, "正在转录语音并识别可剪辑片段…（这一步通常最花时间）")
    transcript = _script_stage(job, input_path)

    if wa:
        _safe_send(wa, job.user.whatsapp_id, "转录完成，正在生成剪辑方案…")
    try:
        from .agent_editor import plan_video

        plan = plan_video(request, video_path, transcript=transcript,
                          source_facts=source_facts)
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
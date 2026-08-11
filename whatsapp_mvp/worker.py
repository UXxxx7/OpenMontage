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

        # clip-factory 走完全不同的执行函数和终态——candidates 早在规划阶段
        # 就选好了（见 _plan_clip_factory），这里只负责逐条渲染。跟下面
        # talking-head 分支共享同一层 try/except：clip-factory 整批失败
        # （run_clip_factory_pipeline 只在一条都没成功时才抛异常）一样会落进
        # 底部的 except，变成 JobStatus.ERROR，传播方式完全一致。
        if job.pipeline == "clip-factory":
            _safe_send(WhatsAppClient(get_config()), job.user.whatsapp_id,
                      "开始渲染选中的片段…（片数较多时会比较久）")
            from .pipeline_runner import run_clip_factory_pipeline

            cf_result = run_clip_factory_pipeline(job)
            update_job_fields(
                job_id,
                status=JobStatus.CLIPS_READY,
                generation_cost_usd=cf_result.get("generation_cost_usd") or 0.0,
            )
            logger.info(f"clip-factory 完成: job {job_id}, "
                       f"{cf_result.get('clip_count')}/{cf_result.get('clip_count_total')} 条成功")
            return

        # 进度预览：确认后到预览生成之间可能较久（剪辑 + b-roll 合成 + 渲染），先回一条
        _safe_send(WhatsAppClient(get_config()), job.user.whatsapp_id,
                   "开始剪辑与合成，正在生成预览…（含 b-roll 合成时会稍久）")

        from .pipeline_runner import run_talking_head_pipeline

        result = run_talking_head_pipeline(job)

        if result.get("preview_path"):
            # 发帖配文生成——必须在标记 PREVIEW_READY 的同一次 update_job_fields
            # 里一起落库（原子写），不能等这次写完再补一次。Node 网关的
            # waitForStatus() 一见到 PREVIEW_READY 就返回、不会再轮询第二次，
            # deliverStageResult() 只会拿那一次快照发消息——如果 talkinghead_social_caption
            # 是分两次写，轮询窗口刚好卡在两次写中间时，文案会在用户毫无感知的
            # 情况下永久丢失。这里内部已有 25s 硬性上限（social_caption.py），
            # 失败/超时都返回 None，外层再包一层 try/except 双保险——文案这个
            # 附加功能绝不能拖累或搞崩预览消息本身。
            social_caption_json = None
            if get_config().social_caption_enabled:
                try:
                    from .social_caption import generate_caption
                    caption_result = generate_caption(job.job_dir, job.edit_request)
                    if caption_result:
                        social_caption_json = json.dumps(caption_result, ensure_ascii=False)
                except Exception as e:
                    logger.warning(f"social_caption: 生成失败，不影响预览交付: {e}")

            update_job_fields(
                job_id,
                preview_path=result["preview_path"],
                status=JobStatus.PREVIEW_READY,
                # 持久化降级信息（没降级也要写空列表：覆盖上一轮 retry 的旧值）。
                # Node 网关靠 GET /jobs 读它、在预览消息里如实告知用户——下面
                # _safe_send 的提醒在网关模式下是死代码，用户实际看不到。
                degraded_operations=json.dumps(result.get("degraded_operations") or []),
                # AI 生成累计花费（同理覆盖旧值，没生成过就是 0）。
                generation_cost_usd=result.get("generation_cost_usd") or 0.0,
                # 同上：显式传，哪怕是 None——retry 会重新走到这里，漏传会让
                # 上一轮成功生成的旧文案在这一轮失败时静默留存下来。
                talkinghead_social_caption=social_caption_json,
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
        # ── Arm B:确认前改草稿(补丁点②)──
        # 命中 arm_b 且有 scene_draft.tsx → 按反馈改草稿(不渲染),回 WAITING_CONFIRMATION。
        # 返回 None(未命中 / 无草稿 / 修订失败)→ 落穿下面的 L2 就地修订(Arm A)。
        try:
            from .authored import arm_b_enabled, revise_authored_plan
            if arm_b_enabled(job):
                _pe = revise_authored_plan(job, feedback)
                if _pe is not None:
                    update_job_fields(
                        job_id,
                        edit_request=f"{job.edit_request}。补充：{feedback}",
                        planned_edit=json.dumps(_pe, ensure_ascii=False),
                    )
                    job = get_job(job_id)
                    _send_confirmation(job, wa)
                    logger.info("ArmB 就地修订完成(改草稿,未渲染,已回 WAITING_CONFIRMATION)")
                    return
                logger.info("ArmB revise_authored_plan 落穿 → 走 L2 就地修订(Arm A)")
        except Exception as _e:  # noqa: BLE001 —— 修订分支绝不拖垮主流程
            logger.warning(f"ArmB 修订分支异常,落穿 L2: {type(_e).__name__}: {_e}")

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


def _editor_marker_path(job) -> Path:
    return job.job_dir / "_editor_render.json"


def _read_editor_marker(job) -> dict:
    default = {"state": "idle", "pending_props": None, "started_at": None,
               "error": None, "save_timestamps": [],
               # Phase 8 — Arm B 的手动编辑(overrides)版本,跟 pending_props
               # 共用同一个标记文件/state machine(一个 job 同时只可能是 Arm A
               # 或 Arm B 中的一种,两个字段不会同时有值)。
               "pending_overrides": None}
    path = _editor_marker_path(job)
    if not path.exists():
        return default
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
        return {**default, **marker}
    except Exception:
        return default


def _write_editor_marker(job, marker: dict) -> None:
    _editor_marker_path(job).write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")


def _finish_editor_render_with_note(job_id: str, job, note: str) -> None:
    """预览编辑器保存失败时的收尾——从不设成 ERROR（理由跟 revise_style 的
    失败分支一样：旧 preview.mp4 从未被 render_props_directly 触碰，用户
    不该因为一次编辑失败就丢掉已经到手的预览），只把状态退回
    PREVIEW_READY，把原因如实追加进 quality_warnings。"""
    prev_warnings: list[str] = []
    try:
        prev_warnings = json.loads(job.quality_warnings) if job.quality_warnings else []
    except Exception:
        prev_warnings = []
    update_job_fields(
        job_id,
        status=JobStatus.PREVIEW_READY,
        progress_stage=None,
        quality_warnings=json.dumps(prev_warnings + [note]),
    )


def editor_render(job_id: str) -> None:
    """预览编辑器"保存"的后台渲染入口。从 `_editor_render.json`（跟
    `webhook.py` 的 `POST /editor/{id}/props` 共享同一份标记文件）里取出
    待渲染的 `pending_props`，渲染；如果渲染期间又有新的保存到达
    （`pending_props` 被那个端点刷新过），接着渲最新这份，直到没有新的
    待渲染内容为止——这就是合并（coalescing）：同一个 job 任何时刻只有一次
    渲染真正在跑，`RENDER_SLOTS` 全局只有一个槽位，排队会让用户等最多
    `OM_RENDER_TIMEOUT_S`（默认 1800s）。

    跟 `revise_style` 同样的原则：从不把失败变成 `ERROR`——旧 preview.mp4
    永远保留，状态退回 `PREVIEW_READY`，`quality_warnings` 里如实追加一句。
    """
    logger.info(f"预览编辑器渲染 {job_id}")
    job = get_job(job_id)
    if job is None:
        return

    from .pipeline_runner import _EditorPropsInvalid, _EditorRenderFailed, render_props_directly

    try:
        while True:
            marker = _read_editor_marker(job)
            props = marker["pending_props"]
            if props is None:
                break
            # 摘掉这份 pending_props 再开始渲染——如果不摘空，渲染完成时无法
            # 区分"pending_props 还是我刚取出的这份"和"渲染期间被新保存刷新
            # 过"，摘空后循环末尾能干净地通过"是否非 None"判断出后者。
            marker["pending_props"] = None
            _write_editor_marker(job, marker)

            job = get_job(job_id)  # 重新加载，取最新的 degraded_operations/quality_warnings
            if job is None:
                return

            def _report_progress(stage: str) -> None:
                update_job_fields(job_id, progress_stage=stage)

            started_at = time.time()
            try:
                result = render_props_directly(job, props, on_progress=_report_progress)
            except _EditorPropsInvalid as e:
                logger.warning(f"预览编辑器 {job_id}: props 校验失败: {e}")
                marker = _read_editor_marker(job)
                marker["error"] = str(e)[:500]
                _write_editor_marker(job, marker)
                _finish_editor_render_with_note(
                    job_id, job, f"Your edit didn't pass validation and wasn't applied: {e}")
                continue
            except _EditorRenderFailed as e:
                logger.exception(f"预览编辑器渲染出错 {job_id}: {e}")
                marker = _read_editor_marker(job)
                marker["error"] = str(e)[:500]
                _write_editor_marker(job, marker)
                _finish_editor_render_with_note(
                    job_id, job,
                    "Couldn't render your edit — your previous preview is unchanged.")
                continue
            except Exception as e:
                # 未预料到的异常（不是上面两个已知失败类型）——同样不设 ERROR，
                # 后台线程静默死掉比明确回退更糟：job 会卡在 RUNNING_PIPELINE
                # 里，Node 那边只能等到轮询超时才当失败处理，用户中途拿不到
                # 任何解释。
                logger.exception(f"预览编辑器渲染出现未预料异常 {job_id}: {e}")
                marker = _read_editor_marker(job)
                marker["error"] = str(e)[:500]
                _write_editor_marker(job, marker)
                _finish_editor_render_with_note(
                    job_id, job,
                    "Couldn't render your edit — your previous preview is unchanged.")
                continue
            elapsed_seconds = round(time.time() - started_at, 1)

            update_job_fields(
                job_id,
                preview_path=result["preview_path"],
                status=JobStatus.PREVIEW_READY,
                degraded_operations=json.dumps(result.get("degraded_operations") or []),
                generation_cost_usd=result.get("generation_cost_usd") or 0.0,
                llm_tokens_input=result.get("llm_tokens_input") or 0,
                llm_tokens_output=result.get("llm_tokens_output") or 0,
                llm_cost_usd=result.get("llm_cost_usd") or 0.0,
                elapsed_seconds=elapsed_seconds,
                quality_warnings=json.dumps(result.get("quality_warnings") or []),
                progress_stage=None,
            )
    finally:
        # 循环退出时（正常没有更多 pending_props，或提前 return）一律把状态
        # 收回 idle——POST /editor/{id}/props 靠 state=="rendering" 判断要不要
        # 合并而不是起新的后台任务，这里不收回的话下一次保存会永远走合并
        # 分支，实际上再也不会真正触发渲染。
        job2 = get_job(job_id)
        if job2 is not None:
            marker = _read_editor_marker(job2)
            marker["state"] = "idle"
            marker["pending_props"] = None
            _write_editor_marker(job2, marker)


# ---------------------------------------------------------------------------
# C-roll：照片 -> AI 文案 -> HeyGen 数字人说话视频 -> 接入常规剪辑管线
# ---------------------------------------------------------------------------

def editor_render_authored(job_id: str) -> None:
    """Phase 8 —— Arm B（AI 现写）版预览编辑器"保存"的后台渲染入口。跟
    editor_render 同一个标记文件/合并（coalescing）机制，只是取的是
    pending_overrides 而不是 pending_props，且渲染只需要 render_authored
    （直接吃 job_dir/authored/scene.tsx 这份已经现写好的代码 + 新的
    overrides），完全不调 LLM——手动编辑本来就该是这个响应速度，不需要
    重新现写。

    基础 props（videoSrc/broll/words/fps/durationInFrames）读自
    job_dir/authored/props.json——这是 compose_authored 成功时落的规范拷贝
    （详见 whatsapp_mvp/authored/__init__.py 自己的注释），不重新跑 _prepare
    （不需要重新转写/重新收 b-roll）。"""
    logger.info(f"Arm B 预览编辑器渲染 {job_id}")
    job = get_job(job_id)
    if job is None:
        return

    from .authored.authored_renderer import render_authored

    authored_dir = job.job_dir / "authored"
    scene_path = authored_dir / "scene.tsx"
    props_path = authored_dir / "props.json"

    try:
        while True:
            marker = _read_editor_marker(job)
            overrides = marker["pending_overrides"]
            if overrides is None:
                break
            # 同样先摘掉再渲染（理由同 editor_render）：渲染完成时才能干净
            # 区分"刚取出的这份"和"渲染期间又被新保存刷新过"。
            marker["pending_overrides"] = None
            _write_editor_marker(job, marker)

            job = get_job(job_id)
            if job is None:
                return

            if not scene_path.exists() or not props_path.exists():
                logger.warning(f"Arm B 预览编辑器 {job_id}: authored/scene.tsx 或 props.json 缺失")
                _finish_editor_render_with_note(
                    job_id, job, "Couldn't apply your edit \u2014 this job's AI-authored scene is missing.")
                continue

            try:
                tsx = scene_path.read_text(encoding="utf-8")
                base_props = json.loads(props_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.exception(f"Arm B 预览编辑器 {job_id}: 读取 scene.tsx/props.json 失败: {e}")
                _finish_editor_render_with_note(
                    job_id, job, "Couldn't apply your edit \u2014 your previous preview is unchanged.")
                continue

            input_video = job.job_dir / (base_props.get("videoSrc") or "input.mp4")
            broll_abs = [
                {**b, "src": str(job.job_dir / b["src"])}
                for b in (base_props.get("broll") or []) if b.get("src")
            ]
            words = base_props.get("words") or []
            fps = int(base_props.get("fps") or 30)
            duration_s = float(base_props.get("durationInFrames") or fps) / fps

            started_at = time.time()
            out_path = authored_dir / "render_manual.mp4"
            config = get_config()
            rc_dir = Path(config.openmontage_root) / "remotion-composer"
            import os
            timeout_s = int(os.getenv("ARM_B_RENDER_TIMEOUT_S", "600"))

            try:
                from .concurrency import RENDER_SLOTS
                with RENDER_SLOTS:
                    result = render_authored(tsx, input_video, broll_abs, words, duration_s,
                                             out_path, rc_dir=rc_dir, timeout_s=timeout_s,
                                             overrides=overrides)
            except ImportError:
                result = render_authored(tsx, input_video, broll_abs, words, duration_s,
                                         out_path, rc_dir=rc_dir, timeout_s=timeout_s,
                                         overrides=overrides)

            if not result.ok:
                logger.warning(f"Arm B 预览编辑器 {job_id}: 渲染失败: {result.log_tail[-500:]}")
                marker = _read_editor_marker(job)
                marker["error"] = (result.log_tail or "")[:500]
                _write_editor_marker(job, marker)
                _finish_editor_render_with_note(
                    job_id, job, "Couldn't render your edit \u2014 your previous preview is unchanged.")
                continue

            elapsed_seconds = round(time.time() - started_at, 1)
            preview = job.job_dir / "preview.mp4"
            import shutil as _shutil
            _shutil.copyfile(result.out_path, preview)
            (authored_dir / "overrides.json").write_text(
                json.dumps(overrides, ensure_ascii=False), encoding="utf-8")

            update_job_fields(
                job_id,
                preview_path=str(preview),
                status=JobStatus.PREVIEW_READY,
                generation_cost_usd=0.0,   # 纯重渲，不调 LLM，没有新增成本
                elapsed_seconds=elapsed_seconds,
                progress_stage=None,
            )
    finally:
        job2 = get_job(job_id)
        if job2 is not None:
            marker = _read_editor_marker(job2)
            marker["state"] = "idle"
            marker["pending_overrides"] = None
            _write_editor_marker(job2, marker)

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

    from . import heygen_croll

    # 标准档账号只给 3 个 photo avatar 名额，用完必须清理——2026-07-17 实测撞过：
    # 连续测 3 次直接把配额堵死，第 4 次上传直接 400。放 finally 里保证无论后面
    # 生成成功/超时/异常都会清理，因为上传这一步本身就已经占了名额，不清理的话
    # 即使后续步骤失败，这个名额也白白浪费掉。（合并自 PR #39，2026-07-20）
    talking_photo_id: Optional[str] = None
    try:
        from .croll_script import write_script

        if not heygen_croll.is_available():
            raise RuntimeError("HEYGEN_API_KEY 未配置，C-roll 功能不可用")

        script = write_script(photo_path, lang=lang, hint=hint)
        if not script:
            raise RuntimeError("看图写文案失败（视觉 LLM 不可用或未返回内容）")
        logger.info(f"  C-roll 文案（{job_id}）: {script[:80]}")
        # HeyGen 数字人念的就是这段文字，逐字保证准确——存下来给后面的
        # transcribe_segments 用来纠正 ASR 听错的同音字/含糊音（而不是让
        # ASR 重新"猜"一遍这段本来就 100% 已知的文本，见 pipeline_runner.py
        # _correct_transcript_against_script 的说明）。
        (job.job_dir / "croll_script.txt").write_text(script, encoding="utf-8")

        talking_photo_id = heygen_croll.upload_talking_photo(Path(photo_path))
        if not talking_photo_id:
            raise RuntimeError("HeyGen 照片上传失败")

        # 艺人注册过克隆音色（voice_clone.py）就用真实声音合成、走 HeyGen 的
        # 音频对口型模式；没注册过，或合成这一步失败，退回 HeyGen 库存声音——
        # 这是"锦上添花"不是"必需依赖"，克隆环节挂了不该拖垮整条 C-roll。
        from .voice_clone import synthesize_for_heygen
        audio_url = synthesize_for_heygen(script, job.user.elevenlabs_voice_id, job.job_dir)
        if audio_url:
            logger.info(f"  C-roll（{job_id}）使用克隆音色语音: {audio_url}")
            video_id = heygen_croll.generate_talking_video(talking_photo_id, audio_url=audio_url)
        else:
            video_id = heygen_croll.generate_talking_video(talking_photo_id, script, lang=lang)
        if not video_id:
            raise RuntimeError("HeyGen 视频生成提交失败")

        input_path = job.job_dir / "input.mp4"
        # 60 秒成片的渲染明显比 20 秒的慢，300s 不够用；给到 15 分钟。
        ok = heygen_croll.poll_and_download(video_id, input_path, timeout_s=900)
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
    finally:
        if talking_photo_id:
            heygen_croll.delete_talking_photo(talking_photo_id)


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


def _plan_clip_factory(job: Any, wa: Any = None) -> None:
    """clip-factory 管线的规划阶段：转录 -> 选片排序 -> 写 planned_edit。

    故意不新写一套确认消息逻辑——把 planned_edit 写成跟 talking-head 规划
    兼容的 {summary, edit_operations} 形状，直接复用现成的 _send_confirmation
    （Python 侧）/ formatPlanMessage（Node 侧，真正展示给用户的那条），
    confirm/cancel 整条会话流程一行都不用改。candidates 的完整结构化数据
    另外存在 planned_edit.candidates 里，供确认后 run_clip_factory_pipeline
    真正渲染时使用。

    选不出候选（源太短/没有转写内容/LLM 不可用）就退回 talking-head 规划，
    不让用户卡在一个"选片选不出来"的死胡同里。
    """
    update_job_status(job.id, JobStatus.PLANNING)
    input_path = job.job_dir / "input.mp4"

    if wa:
        _safe_send(wa, job.user.whatsapp_id, "正在转录并挑选最佳片段…（长视频这一步会比较久）")

    from .pipeline_runner import _probe_duration, transcribe_segments
    duration = _probe_duration(input_path) or 0.0
    segments = transcribe_segments(str(input_path), job.job_dir)

    from .clip_factory import select_clips, rank_and_trim
    selection = select_clips(segments, duration, workdir=job.job_dir)
    candidates = rank_and_trim(selection)

    if not candidates:
        logger.info(f"clip-factory: job {job.id} 没有选出合格候选片段，退回 talking-head 规划")
        update_job_fields(job.id, pipeline="talking-head")
        _run_llm_planner(job, wa)
        return

    min_c = selection.get("min_clips", 0)
    max_c = selection.get("max_clips", 0)
    summary = (f"这段视频大约 {duration/60:.0f} 分钟，我按质量挑了 {len(candidates)} 条独立片段"
              f"（目标区间 {min_c}-{max_c} 条），排名如下：")
    operations = [
        {"description": f"#{c['rank']} [{c.get('clip_family', 'clip')}] "
                        f"{(c.get('hook_text') or '')[:60]} (~{c['duration_seconds']:.0f}s)"}
        for c in candidates
    ]
    plan = {
        "pipeline": "clip-factory",
        "summary": summary,
        "edit_operations": operations,
        "candidates": candidates,
    }
    update_job_fields(job.id, pipeline="clip-factory",
                      planned_edit=json.dumps(plan, ensure_ascii=False))
    logger.info(f"clip-factory 规划完成: job {job.id}, {len(candidates)} 条候选")


def _run_llm_planner(job: Any, wa: Any = None) -> None:
    """用 L2 agent 规划编辑方案（读 manifest/skill + tool-calling + 自审 + schema 校验）。

    agent 出错时回退到 L1.5 关键词/结构化规划器，保证任务不中断。
    传入 wa 时会在转录/规划两个较慢阶段前回传进度消息（进度预览）。
    """
    # clip-factory 意图分类——放在最前面，比 Arm B/L2 都早：命中就整条短路，
    # 走完全不同的规划路径（选片而不是单条编辑方案）。分类失败关闭到
    # "talking-head"（见 classify_pipeline_intent 自己的注释），不会卡在这里。
    # CLIP_FACTORY_ENABLED=false 时整段跳过分类调用，等价于这个功能不存在——
    # 出问题能立刻关掉，不用等代码回滚（同 SOCIAL_CAPTION_ENABLED 的用法）。
    if get_config().clip_factory_enabled:
        from .content_planner import classify_pipeline_intent
        pipeline_kind = classify_pipeline_intent(job.edit_request or "")
        if pipeline_kind == "clip-factory":
            _plan_clip_factory(job, wa)
            return

    # ── Arm B:author 先行(补丁点①)──
    # 路由命中 arm_b 时,规划阶段就现写 tsx + 出分镜当方案,短路 L2。
    # plan_authored 返回 None(未命中 / author 失败)→ 落穿下面的 L2 规划(Arm A)。
    try:
        from .authored import arm_b_enabled, plan_authored
        if arm_b_enabled(job):
            update_job_status(job.id, JobStatus.PLANNING)
            _pe = plan_authored(job)
            if _pe is not None:
                update_job_fields(job.id, planned_edit=json.dumps(_pe, ensure_ascii=False))
                logger.info("ArmB 规划完成(author 先行,已短路 L2)")
                return
            logger.info("ArmB plan_authored 落穿 → 走 L2 规划(Arm A)")
    except Exception as _e:  # noqa: BLE001 —— 规划分支绝不拖垮主流程
        logger.warning(f"ArmB 规划分支异常,落穿 L2: {type(_e).__name__}: {_e}")

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

def _plan_needs_broll_generation(operations: list) -> bool:
    """规划里有没有要求 AI 现生成 b-roll（gen_prompt，不是用户上传的
    asset_ref）——只有这类才用得上 gemini_broll 的额度状态检测。"""
    for op in operations or []:
        if op.get("type") != "insert_broll":
            continue
        for item in op.get("items") or []:
            if isinstance(item, dict) and item.get("gen_prompt") and not item.get("asset_ref"):
                return True
    return False


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

    # 架构复审后新增（2026-07-29，真实事故驱动，job_9c671249eb76）：规划里
    # 要求 AI 生成 b-roll，但账号当时在 Gemini 免费档、对应模型配额是
    # 0——用户直到确认、等生成失败了才知道这段不会有。这里在发确认消息
    # *之前*就检查一次（不产生真实调用，读的是最近一次真实失败留下的本地
    # 缓存），命中就把警示写进 plan 本身并重新持久化——Node 网关模式下这条
    # 消息真正的文案是 Node 读 GET /jobs/{id} 的 planned_edit 自己拼的（这里
    # 走 _safe_send 那份是直连模式用的，网关模式下是死代码），所以警示必须
    # 落进 plan 字段本身，不能只加进下面这条本地拼的 msg_lines 里。
    if _plan_needs_broll_generation(operations):
        from .gemini_broll import check_broll_generation_availability
        available, reason = check_broll_generation_availability()
        if not available:
            plan["broll_generation_warning"] = reason
            update_job_fields(job.id, planned_edit=json.dumps(plan, ensure_ascii=False))
            logger.warning(
                f"任务 {job.id}: 规划要求 AI 生成 b-roll，但当前不可用（{reason}）"
                "，已在确认消息里提前警示"
            )

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

    if plan.get("broll_generation_warning"):
        msg_lines.extend(["", f"⚠️ AI 生成 b-roll 当前不可用：{plan['broll_generation_warning']}"])

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
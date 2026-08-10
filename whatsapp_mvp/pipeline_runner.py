# WhatsApp MVP - Pipeline Runner
# L1.5：通过 OpenMontage 正式工具执行 talking-head 编辑。
# 结构为 "op -> handler 注册表"：每个 handler 是对一个正式工具的薄封装，
# 这一层在将来升级到 L2（agent 编排）时可原样复用，只需换掉上面的编排头。

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .config import get_config
from .database import Job
from . import authored as _armb  # Arm B(模型现写 composition)接线层;flag 关时零行为差异

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 重型阶段并发闸门。node 侧 WA_WORKER_CONCURRENCY>1 后，多任务的"规划"可以
# 重叠（主要在等 LLM 响应，占不了多少 CPU），但转写(Whisper)/Remotion 渲染/
# 画质增强是 CPU/内存大户——单机上两个同时跑会互相拖慢到集体超时（实测事故，
# 2026-07-08）。给每类重活一个信号量各自排队：多用户的感受是"并行推进"，
# 机器的现实是"重活永远只有 N 个在跑"。槽位数可用环境变量按机器调。
from .concurrency import (
    ENHANCE_SLOTS as _ENHANCE_SLOTS,
    RENDER_SLOTS as _RENDER_SLOTS,
    RENDER_TIMEOUT_S as _RENDER_TIMEOUT_S,
    TRANSCRIBE_SLOTS as _TRANSCRIBE_SLOTS,
)


# ============================================================================
# 主入口
# ============================================================================

# add_music 重试前的等待：Pixabay 检索失败常是 Cloudflare 反爬挑战（非官方 API，
# 爬公开搜索页），这类拦截一般数十秒内解除，立即重试大概率还在同一个挑战窗口里。
# （合并自 PR #39，2026-07-20）
_MUSIC_RETRY_DELAY_S = 30


def run_talking_head_pipeline(job: Job) -> dict[str, Any]:
    """按编辑计划用 OpenMontage 正式工具执行编辑，输出 preview.mp4。

    顺序：先应用所有视频类操作，字幕留到最后（转写才对得上剪过的时间轴）。
    """
    job_dir = job.job_dir
    input_video = job_dir / "input.mp4"
    preview_path = job_dir / "preview.mp4"

    if not input_video.exists():
        raise FileNotFoundError(f"找不到输入视频: {input_video}")

    # ── Arm B 灰度门:开关/百分比走环境变量(ARM_B_ENABLED / ARM_B_PERCENT)。
    # compose_authored 永不抛;返回 None 即"没出片",落穿进下面的 Arm A 原路径(兜底)。
    # 命中并出片则在此 return,不会走到下面的 Arm A 预算标记清理(那只服务 apply_style)。
    if _armb.arm_b_enabled(job):
        _armb_result = _armb.compose_authored(job)
        if _armb_result is not None:
            return _armb_result
        logger.warning("Arm B 未出片,落回 Arm A 继续")

    # apply_style 的内容规划总预算标记（见 _op_apply_style 里的说明）只应该在
    # *这一次*管线运行内、跨"原始尝试 + 外层自动重试一次"共享；每次重新跑
    # 整条管线（无论是首次 confirm 还是用户显式 retry）都要清掉上一次留下的
    # 标记，否则会一直沿用一个早就过期的截止时间，新的一次尝试会被误判成
    # "预算已用完"，直接跳过所有重试。
    (job_dir / "_apply_style_deadline.txt").unlink(missing_ok=True)

    plan = _load_plan(job)
    operations = plan.get("edit_operations", [])
    logger.info(f"=== 运行管线: {job.id}，{len(operations)} 个操作 ===")

    src = str(input_video)
    applied: list[str] = []
    degraded: list[str] = []  # 非致命失败：跳过后仍交付上一步结果的操作

    # 优雅降级：这些操作失败时不许拖垮整个 job——保留上一步剪好的视频继续交付。
    # apply_style 是重量级 Remotion 渲染（失败面多：模板/字体/依赖/props）；零指令默认
    # 是 [remove_filler, apply_style]，渲染挂了也必须把剪好的视频还给用户，而不是整单报错。
    # 后续 compose 段算子（color_grade / audio_enhance 等）落地时按需加进来。
    # add_music 不在这个集合里——它被摘出 ordered_ops 单独在最后执行（见下方
    # music_op 分支），有自己手写的一份对称重试+降级逻辑，不走这里的通用循环。
    _DEGRADABLE_OPS = {"apply_style", "insert_broll"}

    # 执行顺序：多个 remove_segment 按 start 降序“从后往前”切（转录给的是原始时间轴
    # 坐标；从后往前切，前面的刀就不会移动后面那刀之前的坐标）。其余视频操作保持原序，
    # 字幕永远最后（要对剪过的时间轴转写）。背景音乐更要放最后一步单独混——apply_style
    # 内部有 audio_enhance（清人声降噪）子步骤，字幕烧录也要转写音频，音乐这时候要是
    # 已经混进去了，会被降噪链路误伤、也会干扰转写准确度，必须晚于两者。
    subtitle_ops = [op for op in operations if op.get("type") == "add_subtitles"]
    music_ops = [op for op in operations if op.get("type") == "add_music"]
    video_ops = [op for op in operations if op.get("type") not in ("add_subtitles", "add_music")]
    removes = sorted(
        [op for op in video_ops if op.get("type") == "remove_segment"],
        key=lambda o: _num(o.get("start_seconds")) or 0.0, reverse=True,
    )
    others = [op for op in video_ops if op.get("type") != "remove_segment"]
    ordered_ops = removes + others
    # presenter 模式：同一方案里 insert_broll 与 apply_style 并存时，让 b-roll 用
    # cutaway 铺满卡片、人物交给模板在下方渲染（见 _op_insert_broll / _op_apply_style）。
    # 先清掉上一轮残留的 _presenter.json，避免改方案后放出"幽灵人物小窗"。
    (job_dir / "_presenter.json").unlink(missing_ok=True)
    if any(o.get("type") == "apply_style" for o in ordered_ops):
        for _o in ordered_ops:
            if _o.get("type") == "insert_broll":
                _o["_presenter"] = True
    if len(removes) > 1:
        logger.info(f"  {len(removes)} 段 remove_segment 将按起点降序执行（防时间轴错位）")

    subtitle_op: Optional[dict] = subtitle_ops[0] if subtitle_ops else None
    music_op: Optional[dict] = music_ops[0] if music_ops else None

    for op in ordered_ops:
        op_type = op.get("type", "")
        handler = _OP_HANDLERS.get(op_type)
        if handler is None:
            logger.warning(f"  跳过不支持的操作: {op_type}")
            continue
        logger.info(f"  执行操作: {op_type}")
        before = _probe_duration(Path(src))
        try:
            new_src = handler(src, op, job_dir)
        except Exception as e:
            # 先自动重试一次再谈降级：渲染类失败里有一部分是瞬时的（资源争抢、
            # 子进程偶发），一次重试能白捡回来；确定性失败则重试也快（在同一
            # 个错误上再挂一次），代价可控。
            logger.warning(f"    {op_type}: 执行失败，自动重试一次。原因: {e}")
            try:
                new_src = handler(src, op, job_dir)
            except Exception as e2:
                if op_type in _DEGRADABLE_OPS:
                    logger.warning(
                        f"    {op_type}: 重试仍失败，降级——保留上一步结果继续交付"
                        f"（会显性告知用户，非静默）。原因: {e2}"
                    )
                    degraded.append(op_type)
                    continue
                raise
        if new_src and Path(new_src).exists() and str(Path(new_src).resolve()) != str(Path(src).resolve()):
            after = _probe_duration(Path(new_src))
            logger.info(f"    {op_type}: 时长 {before:.1f}s → {after:.1f}s"
                        + ("  (无变化/未生效)" if abs(after - before) < 0.05 and op_type != "reframe" else ""))
            src = str(new_src)
            applied.append(op_type)
        else:
            logger.info(f"    {op_type}: 无输出/未改变视频 (no-op)")

    if subtitle_op is not None:
        logger.info("  执行操作: add_subtitles")
        new_src = _op_add_subtitles(src, subtitle_op, job_dir)
        if new_src and Path(new_src).exists():
            src = str(new_src)
            applied.append("add_subtitles")

    if music_op is not None:
        logger.info("  执行操作: add_music")
        try:
            new_src = _op_add_music(src, music_op, job_dir)
        except Exception as e:
            # Pixabay 检索走的是公开搜索页爬取（没有官方 API），失败常是 Cloudflare
            # 的人机验证挑战（cf-mitigated: challenge）——这类拦截通常几十秒内自行
            # 放行，立即重试大概率撞在同一个挑战窗口里、白重试一次。等一段再重试，
            # 成功率明显更高（2026-07-17 实测复现过：403 立即重试仍 403，等待后
            # 用完全相同的请求参数直接成功）。
            logger.warning(f"    add_music: 执行失败，{_MUSIC_RETRY_DELAY_S}s 后重试一次。原因: {e}")
            time.sleep(_MUSIC_RETRY_DELAY_S)
            try:
                new_src = _op_add_music(src, music_op, job_dir)
            except Exception as e2:
                logger.warning(
                    f"    add_music: 重试仍失败，降级——保留无背景音乐的版本继续交付"
                    f"（会显性告知用户，非静默）。原因: {e2}"
                )
                new_src = None
                degraded.append("add_music")
        if new_src and Path(new_src).exists():
            src = str(new_src)
            applied.append("add_music")

    # 定稿为 preview.mp4
    if Path(src).resolve() != preview_path.resolve():
        shutil.copyfile(src, preview_path)

    duration = _probe_duration(preview_path)
    if degraded:
        logger.warning(f"=== 降级交付: {job.id} 跳过失败的 {degraded}，交付上一步结果 ===")
    generation_cost = _read_generation_cost(job_dir)
    logger.info(f"=== 管线完成: {job.id} → {preview_path} ({duration:.1f}s), 应用: {applied} ===")
    return {
        "preview_path": str(preview_path),
        "duration": duration,
        "applied_operations": applied,
        "degraded_operations": degraded,
        "generation_cost_usd": generation_cost,
    }


def run_final_export(job: Job) -> dict[str, Any]:
    """最终导出：基于预览重新编码为 final.mp4（+faststart 便于流式播放）。"""
    job_dir = job.job_dir
    preview_path = job_dir / "preview.mp4"
    final_path = job_dir / "final.mp4"

    if not preview_path.exists():
        logger.warning("预览不存在，重新运行管线生成最终版本")
        result = run_talking_head_pipeline(job)
        preview_path = Path(result["preview_path"])

    logger.info(f"最终导出: {preview_path} → {final_path}")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(preview_path),
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
         str(final_path)],
        capture_output=True, check=True,
    )
    logger.info(f"最终导出完成: {final_path}")
    return {"final_path": str(final_path)}


# ============================================================================
# Clip Factory：一条长视频 -> N 条独立排名短片
#
# 跟 run_talking_head_pipeline 的通用 op-handler 派发不是一回事——candidate
# 列表已经在规划阶段（worker._plan_clip_factory -> clip_factory.select_clips）
# 定好了，这里只负责把每条候选真正渲染出来。每条 clip 独立 try/except、
# 独立落库，一条失败不连累其它——这是 clip-factory.yaml 的 compose-director
# 阶段"fail softly, continue the rest of the batch"要求的具体实现，不只是
# 写在注释里的美好愿望。
# ============================================================================

def run_clip_factory_pipeline(job: Job) -> dict[str, Any]:
    """按 job.planned_edit 里的 candidates 列表逐条渲染，每条独立成败。

    只有全批次一条都没成功时才抛异常（外层 worker.run_pipeline 的 try/except
    会把它变成 JobStatus.ERROR，跟 run_talking_head_pipeline 失败时的传播方式
    一致）——只要有一条成功，整批就按"部分交付"处理，不整体报错。
    """
    from .job_manager import create_clips, update_clip_fields, update_clip_status
    from .database import ClipStatus

    job_dir = job.job_dir
    input_video = job_dir / "input.mp4"
    if not input_video.exists():
        raise FileNotFoundError(f"找不到输入视频: {input_video}")

    plan = _load_plan(job)
    candidates = plan.get("candidates") or []
    if not candidates:
        raise RuntimeError("planned_edit 里没有 candidates，clip-factory 无法渲染")

    clip_rows = create_clips(job.id, candidates)
    config = get_config()
    deadline = time.time() + config.clip_factory_wall_time_s

    ready_count = 0
    total_generation_cost = 0.0
    for i, (cand, row) in enumerate(zip(candidates, clip_rows), 1):
        if time.time() > deadline:
            logger.warning(f"clip-factory: job {job.id} 达到 wall-time 预算，"
                           f"跳过剩余 {len(candidates) - i + 1} 条")
            update_clip_status(row.id, ClipStatus.FAILED,
                              "skipped: batch wall-time budget exhausted")
            continue

        update_clip_status(row.id, ClipStatus.RENDERING)
        clip_workdir = job_dir / f"clip_{row.id}"
        clip_workdir.mkdir(parents=True, exist_ok=True)
        out_filename = f"clip_{row.rank:02d}.mp4"
        try:
            result = _render_one_clip(input_video, clip_workdir, cand, job_dir, out_filename)
        except Exception as e:
            logger.exception(f"clip-factory: 第 {i} 条渲染失败: {e}")
            update_clip_status(row.id, ClipStatus.FAILED, str(e)[:500])
            continue

        caption_result = result.get("caption")
        update_clip_fields(
            row.id,
            status=ClipStatus.READY,
            output_filename=out_filename,
            duration_seconds=result.get("duration"),
            caption_json=json.dumps(caption_result, ensure_ascii=False) if caption_result else None,
            degraded_operations=json.dumps(result.get("degraded") or []),
        )
        ready_count += 1

    if ready_count == 0:
        raise RuntimeError("clip-factory: 全部候选片段渲染失败，没有任何一条成功")

    return {
        "clip_count": ready_count,
        "clip_count_total": len(candidates),
        "generation_cost_usd": total_generation_cost,
    }


def _render_one_clip(input_video: Path, clip_workdir: Path, cand: dict,
                     job_dir: Path, out_filename: str) -> dict:
    """单条候选的渲染：裁剪 -> 转写(clip 自己独立时间轴) -> 字幕烧录(可降级) ->
    音频降噪+调色(可降级) -> 平铺拷到 job_dir 根目录 -> 生成配文(可降级)。

    裁剪失败是致命的（没有视频可用），其余步骤失败都只记录到 degraded 列表、
    继续用上一步的产物往后走——跟 run_talking_head_pipeline 对 apply_style/
    insert_broll 的降级哲学一致。
    """
    from tools.video.video_trimmer import VideoTrimmer
    from tools.video.remotion_caption_burn import RemotionCaptionBurn
    from tools.enhancement.color_grade import ColorGrade
    from tools.audio.audio_enhance import AudioEnhance

    degraded: list[str] = []
    config = get_config()

    # 1. 裁剪——致命，没有片段就没有这条 clip。
    start = float(cand["start_seconds"])
    end = float(cand["end_seconds"])
    trimmed = clip_workdir / "trimmed.mp4"
    r = VideoTrimmer().execute({
        "operation": "cut", "input_path": str(input_video),
        "start_seconds": start, "end_seconds": end, "codec": "libx264",
        "output_path": str(trimmed),
    })
    if not r.success:
        raise RuntimeError(f"clip 裁剪失败: {r.error}")
    src = r.artifacts[0] if r.artifacts else str(trimmed)

    # 2. 独立转写——clip 有自己的 0 基时间轴，不是从源视频时间戳平移过来的
    # （跟 _op_add_subtitles 对剪过的视频重新转写是同一个道理）。这次转写
    # 拿到的是 word-level 原始 segments（给 RemotionCaptionBurn 用），额外
    # 精简一份 {id,start,end,text} 存成 script_transcript.json（跟
    # pipeline_runner.transcribe_segments() 的缓存形状一致），让
    # social_caption.generate_caption() 能直接复用，不用再转写一次。
    t = _safe_transcribe(src, clip_workdir, config.faster_whisper_model)
    word_segments = (t.data.get("segments") if t and t.success else None) or []
    if word_segments:
        slim = [
            {"id": s.get("id"), "start": round(_num(s.get("start")) or 0.0, 2),
             "end": round(_num(s.get("end")) or 0.0, 2), "text": (s.get("text") or "").strip()}
            for s in word_segments
        ]
        (clip_workdir / "script_transcript.json").write_text(
            json.dumps({"segments": slim, "language": t.data.get("language")},
                      ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # 3. 字幕烧录——可降级：没转写出内容就不烧字幕，不整条失败。
    if word_segments:
        try:
            captioned = clip_workdir / "captioned.mp4"
            r = RemotionCaptionBurn().execute({
                "input_path": src, "output_path": str(captioned), "segments": word_segments,
            })
            if r.success and r.artifacts:
                src = r.artifacts[0]
            else:
                degraded.append("add_subtitles")
                logger.warning(f"clip 字幕烧录失败，交付无字幕版本: {getattr(r, 'error', None)}")
        except Exception as e:
            degraded.append("add_subtitles")
            logger.warning(f"clip 字幕烧录异常，交付无字幕版本: {e}")
    else:
        degraded.append("add_subtitles")

    # 4. 调色 + 降噪——best-effort：单步失败就跳过、继续用上一步的产物，不让
    # 收尾步骤拖垮整条 clip。
    for name, tool_cls, extra in (
        ("color_grade", ColorGrade, {"profile": "cinematic_warm", "intensity": 0.85}),
        ("audio_enhance", AudioEnhance, {"preset": "clean_speech"}),
    ):
        try:
            out = clip_workdir / f"_{name}.mp4"
            r = tool_cls().execute({"input_path": src, "output_path": str(out), **extra})
            if r.success:
                new_src = r.data.get("output") or (r.artifacts[0] if r.artifacts else None)
                if new_src and Path(new_src).exists():
                    src = new_src
                else:
                    degraded.append(name)
            else:
                degraded.append(name)
        except Exception as e:
            degraded.append(name)
            logger.warning(f"clip {name} 出错，跳过: {e}")

    # 5. 平铺拷到 job_dir 根目录——/files/{job_id}/{filename} 这条路由（FastAPI
    # 和 Express 两边都一样）不支持嵌套路径，clip_workdir 只是中间产物暂存地。
    final_path = job_dir / out_filename
    shutil.copy(src, final_path)
    duration = _probe_duration(final_path)

    # 6. 配文——复用今天已经建好、验证过的 social_caption.py，原样调用，不
    # 重新发明第三套文案系统。hook_text 当 edit_request 传进去，只是用来给
    # 语言判定 (_resolve_lang) 一个信号，不是真正意义上的"用户指令"。
    caption_result = None
    try:
        from .social_caption import generate_caption
        caption_result = generate_caption(clip_workdir, cand.get("hook_text"))
    except Exception as e:
        degraded.append("social_caption")
        logger.warning(f"clip 配文生成失败，不影响 clip 本身交付: {e}")

    return {"duration": duration, "degraded": degraded, "caption": caption_result}


# ============================================================================
# 操作处理器（op -> 正式工具）
# 每个 handler: (src_path, op_dict, workdir) -> 新文件路径 或 None（无变化则跳过）
# 工具都是惰性 import，缺依赖只影响对应操作，不会拖垮整个 worker。
# ============================================================================

def _op_trim_start(src: str, op: dict, workdir: Path) -> Optional[str]:
    """剪掉开头 N 秒 -> VideoTrimmer cut，保留 [N, 结尾]。"""
    seconds = _num(op.get("seconds"))
    if not seconds or seconds <= 0:
        return None
    from tools.video.video_trimmer import VideoTrimmer
    out = workdir / "_op_trim_start.mp4"
    r = VideoTrimmer().execute({
        "operation": "cut", "input_path": src,
        "start_seconds": seconds, "codec": "libx264",
        "output_path": str(out),
    })
    if not r.success:
        raise RuntimeError(f"trim_start 失败: {r.error}")
    return r.artifacts[0] if r.artifacts else str(out)


def _op_trim_end(src: str, op: dict, workdir: Path) -> Optional[str]:
    """剪掉结尾 N 秒 -> VideoTrimmer cut，保留 [0, 时长-N]。"""
    seconds = _num(op.get("seconds"))
    if not seconds or seconds <= 0:
        return None
    dur = _probe_duration(Path(src))
    end = dur - seconds
    if end <= 0:
        return None
    from tools.video.video_trimmer import VideoTrimmer
    out = workdir / "_op_trim_end.mp4"
    r = VideoTrimmer().execute({
        "operation": "cut", "input_path": src,
        "start_seconds": 0, "end_seconds": end, "codec": "libx264",
        "output_path": str(out),
    })
    if not r.success:
        raise RuntimeError(f"trim_end 失败: {r.error}")
    return r.artifacts[0] if r.artifacts else str(out)


def _op_keep_range(src: str, op: dict, workdir: Path) -> Optional[str]:
    """只保留 [start, end] -> VideoTrimmer cut。"""
    start = _num(op.get("start_seconds")) or 0.0
    end = _num(op.get("end_seconds"))
    from tools.video.video_trimmer import VideoTrimmer
    out = workdir / "_op_keep_range.mp4"
    inputs = {
        "operation": "cut", "input_path": src,
        "start_seconds": start, "codec": "libx264", "output_path": str(out),
    }
    if end and end > start:
        inputs["end_seconds"] = end
    r = VideoTrimmer().execute(inputs)
    if not r.success:
        raise RuntimeError(f"keep_range 失败: {r.error}")
    return r.artifacts[0] if r.artifacts else str(out)


def _op_remove_segment(src: str, op: dict, workdir: Path) -> Optional[str]:
    """删除中间某段 [start, end] -> 保留两侧再 concat（只剩一侧则直接 cut）。"""
    from tools.video.video_trimmer import VideoTrimmer
    a = _num(op.get("start_seconds"))
    b = _num(op.get("end_seconds"))
    if a is None or b is None or b <= a:
        return None
    dur = _probe_duration(Path(src))
    keep: list[dict] = []
    if a > 0.1:
        keep.append({"input_path": src, "start_seconds": 0, "end_seconds": a})
    if dur <= 0 or b < dur - 0.1:
        keep.append({"input_path": src, "start_seconds": b})
    if not keep:
        return None
    out = workdir / "_op_remove_seg.mp4"
    if len(keep) == 1:
        seg = keep[0]
        inputs = {
            "operation": "cut", "input_path": src,
            "start_seconds": seg.get("start_seconds", 0),
            "codec": "libx264", "output_path": str(out),
        }
        if "end_seconds" in seg:
            inputs["end_seconds"] = seg["end_seconds"]
        r = VideoTrimmer().execute(inputs)
    else:
        r = VideoTrimmer().execute({
            "operation": "concat", "segments": keep, "output_path": str(out),
        })
    if not r.success:
        raise RuntimeError(f"remove_segment 失败: {r.error}")
    return r.data.get("output") or (r.artifacts[0] if r.artifacts else str(out))


def _op_remove_silences(src: str, op: dict, workdir: Path) -> Optional[str]:
    """去掉静音/停顿使更紧凑 -> SilenceCutter mode=remove。

    min_silence_duration 可由上层（agent）传入：默认 0.35s 几乎剪掉所有停顿；
    调大（如 1.0~1.5s）则只剪明显偏长的静音、保留自然的句间停顿，说话更自然。
    """
    from tools.video.silence_cutter import SilenceCutter
    min_dur = _num(op.get("min_silence_duration"))
    if not min_dur or min_dur <= 0:
        min_dur = 0.35
    thr = _num(op.get("silence_threshold_db"))
    if thr is None:
        thr = -30
    out = workdir / "_op_nosilence.mp4"
    r = SilenceCutter().execute({
        "input_path": src, "mode": "remove", "output_path": str(out),
        "silence_threshold_db": thr, "min_silence_duration": min_dur,
    })
    if not r.success:
        raise RuntimeError(f"remove_silences 失败: {r.error}")
    seg = r.data.get("silence_segments", 0)
    logger.info(f"    去静音(min_silence={min_dur}s): 检测到 {seg} 段静音，"
                f"移除 {r.data.get('silence_removed_seconds', 0)}s")
    # 无静音时工具会把 output 设为原文件路径
    return r.data.get("output") or (r.artifacts[0] if r.artifacts else None)


# ---------------------------------------------------------------------------
# P2 加固件（合并 feat/pipeline-remove-filler-apply-style 后重新嫁接）：
# 转写异常兜底 / 人脸校准取景 / 短语级字幕。均已在本机 e2e 验证过。
# ---------------------------------------------------------------------------

def _transcribe_elevenlabs(src: str, api_key: str):
    """ElevenLabs Scribe transcription — same API video-use (video-studio's
    own trim pipeline) uses, and for the same reason: retake-detection needs
    consistent word-level text between two near-identical takes to tell them
    apart, which local faster-whisper is meaningfully weaker at (confirmed
    root cause of a real production bug — a retake survived filler-removal).

    timestamps_granularity="word" is mandatory, not optional (video-studio's
    edit-director.md, confirmed by direct testing there): omitting it makes
    Scribe silently return degenerate word timing (multiple consecutive words
    sharing one start==end timestamp), which would corrupt every downstream
    frame calculation silently rather than erroring.

    Returns an object shaped like the local Transcriber's ToolResult
    (.success / .data / .error) so callers don't need to know which
    provider ran.
    """
    import requests

    from tools.base_tool import ToolResult

    try:
        with open(src, "rb") as f:
            resp = requests.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": api_key},
                files={"file": (Path(src).name, f, "video/mp4")},
                data={"model_id": "scribe_v1", "timestamps_granularity": "word"},
                timeout=300,
            )
        resp.raise_for_status()
    except requests.HTTPError as e:
        # Fix C11（2026-07-17，真实生产复现）：ElevenLabs 对"配额用完"和"key 无效/
        # 无权限"都回同一个 401，resp.raise_for_status() 抛出的异常字符串只有
        # "401 Client Error: Unauthorized for url: ..."，完全看不出是哪一种——
        # 逼着上一次调试花了几个小时才靠直接 curl 打 /v1/user 才挖出真正原因
        # (免费档 10000 字符/月配额，body 里其实一直带着
        # {"detail":{"code":"quota_exceeded","message":"...You have N credits
        # remaining..."}})。这里改成优先读 body 里的 code/message，让日志一次
        # 到位区分"配额用完"（等重置或升级套餐，换 key 没用——新账号一样只有
        # 10000/月）和"key 真的无效/无权限"（换 key 才有用）。
        detail = None
        try:
            detail = e.response.json().get("detail") if e.response is not None else None
        except (ValueError, AttributeError):
            pass
        if isinstance(detail, dict) and detail.get("code") == "quota_exceeded":
            msg = f"quota_exceeded: {detail.get('message', '')}（免费档配额用完——等月度重置或升级套餐，换 key 无效）"
        elif isinstance(detail, dict) and detail.get("message"):
            msg = f"{detail.get('code', 'error')}: {detail['message']}"
        else:
            msg = str(e)
        logger.warning(f"  ElevenLabs Scribe 转写调用异常: {msg}")
        return ToolResult(success=False, error=msg)
    except Exception as e:
        logger.warning(f"  ElevenLabs Scribe 转写调用异常: {e}")
        return ToolResult(success=False, error=str(e))

    data = resp.json()
    # ElevenLabs returns "word" and "spacing" as separate token types (the
    # space between two words is its own token) — faster-whisper instead
    # bakes a leading space into each word's own text (e.g. " hello", " world",
    # confirmed in tools/analysis/transcriber.py's direct `w.word` usage with
    # no separate join-with-space step anywhere downstream). Dropping
    # "spacing" tokens outright (as an earlier version of this function did)
    # loses that leading space, and downstream caption-text concatenation —
    # built assuming each word already carries it, like faster-whisper —
    # then mashes every word together with no spaces at all (confirmed real
    # bug: a rendered caption read "I'veeputthefullbreakdowninthis"). Fix:
    # carry each preceding spacing token's text forward as this word's prefix.
    raw_tokens = data.get("words", [])
    word_timestamps = []
    pending_prefix = ""
    for tok in raw_tokens:
        if tok.get("type") == "spacing":
            pending_prefix += tok.get("text", "")
            continue
        if tok.get("type") != "word":
            continue
        word_timestamps.append({
            "word": pending_prefix + tok["text"],
            "start": round(tok["start"], 3),
            "end": round(tok["end"], 3),
        })
        pending_prefix = ""

    # Phrase-level segments too (id/start/end/text) — transcribe_segments()
    # (the L2 agent's own planning-stage transcript, used specifically to
    # spot retakes/repeated sentences before any op runs) needs this shape,
    # not word_timestamps. Same GAP_THRESHOLD_MS=400 phrase-grouping video-use
    # itself uses (tools/directors/edit-director.md Step 3) — new phrase
    # whenever the gap since the last word exceeds 400ms.
    GAP_THRESHOLD_MS = 400
    segments: list[dict] = []
    cur_words: list[str] = []
    cur_start = cur_end = None
    for w in word_timestamps:
        start_ms, end_ms = w["start"] * 1000, w["end"] * 1000
        if cur_words and (start_ms - cur_end) > GAP_THRESHOLD_MS:
            # words already carry their own leading space (see word_timestamps
            # above) — join with "" not " ", or every segment gets double
            # spaces between words.
            segments.append({"id": len(segments), "start": cur_start / 1000, "end": cur_end / 1000,
                              "text": "".join(cur_words).strip()})
            cur_words = []
        if not cur_words:
            cur_start = start_ms
        cur_words.append(w["word"])
        cur_end = end_ms
    if cur_words:
        segments.append({"id": len(segments), "start": cur_start / 1000, "end": cur_end / 1000,
                          "text": " ".join(cur_words)})

    return ToolResult(
        success=True,
        data={
            "word_timestamps": word_timestamps,
            "segments": segments,
            "language": data.get("language_code"),
            "duration_seconds": word_timestamps[-1]["end"] if word_timestamps else 0.0,
        },
    )


# 真实事故（2026-07-23，job_08b94c0922ce）：faster-whisper "small" 模型把
# "重疾险"听成同音字"重极险"，把整句"保额、价格、购买条款也要看清楚"听成
# "保额紧张买购,条款也要看清楚"——这些错字被 content_planner 原样（甚至
# 进一步）抄进了卡片文案，最终渲染出明显的错别字。faster-whisper 的
# hotwords 参数就是为这种"领域术语容易被听成同音字"设计的解码期偏置，
# 不依赖 condition_on_previous_text（该参数已被上面的 Fix 关闭），给出正确
# 写法能显著降低同音字误听。这里给的是保险这个业务域的常见术语——不是
# 这条视频专属，所有转写都会带上，对非保险内容基本无副作用。
_HOTWORDS_INSURANCE = (
    "重疾险 定期寿险 终身寿险 医疗险 意外险 年金险 车险 家财险 "
    "保额 保费 理赔 保单 投保 承保 保险公司 保障"
)


def _correct_transcript_against_script(segments: list[dict], script: str) -> list[dict]:
    """C-roll 场景下，数字人念的就是 write_script() 生成的这段文字，逐字
    100% 已知——用户原话："这个文案可以直接送给他进行判断吧，文案是百分之
    百准确的啊"，说得对：与其指望 ASR 把这段本来就已知的话再听一遍还可能
    听错（真实事故：'重疾险'->'重极险'、'出岔子'->'出差子'），不如直接用
    已知原文纠正 ASR 输出的文字。

    只换文字，不碰时间戳——ASR 的 start/end 依然是基于真实音频对齐出来的，
    照样可信；script 只提供"这段音频对应的文字应该是什么"。用字符级 diff
    把 ASR 全文和 script 对齐，再按每个 segment 原来的字符长度切回去。如果
    某个 segment 对齐出来的文字长度跟原文字长度差太多（说明这段对不上，比如
    ASR 漏听/多听了一整块），保留 ASR 原文，不强行覆盖成可能文不对时的内容
    ——宁可保留一个听错的字，也不要引入一段跟时间戳对不上的文字。
    """
    script_norm = script.replace("\n", "").replace(" ", "")
    asr_full = "".join(seg["text"] for seg in segments)
    if not asr_full.strip() or not script_norm.strip():
        return segments

    opcodes = difflib.SequenceMatcher(None, asr_full, script_norm, autojunk=False).get_opcodes()

    def script_slice(i1: int, i2: int) -> Optional[str]:
        out = []
        for _tag, a1, a2, b1, b2 in opcodes:
            if a2 <= i1 or a1 >= i2 or a2 == a1:
                continue
            lo, hi = max(i1, a1), min(i2, a2)
            frac_lo = (lo - a1) / (a2 - a1)
            frac_hi = (hi - a1) / (a2 - a1)
            out.append(script_norm[b1 + round(frac_lo * (b2 - b1)):b1 + round(frac_hi * (b2 - b1))])
        return "".join(out) if out else None

    corrected = []
    pos = 0
    changed = 0
    for seg in segments:
        seg_len = len(seg["text"])
        piece = script_slice(pos, pos + seg_len)
        new_seg = dict(seg)
        if piece and piece.strip() and abs(len(piece) - seg_len) <= max(2, seg_len // 3):
            if piece != seg["text"]:
                changed += 1
            new_seg["text"] = piece
        corrected.append(new_seg)
        pos += seg_len
    if changed:
        logger.info(f"  转写文本按已知口播文案（croll_script.txt）纠正了 {changed}/{len(segments)} 段")
    return corrected


def _safe_transcribe(src: str, workdir: Path, model_size: str, hotwords: str = _HOTWORDS_INSURANCE):
    """跑转写，把"工具报告失败"和"工具本身抛异常"统一收敛成返回 None。

    config.transcribe_provider == "elevenlabs"（默认，见该字段注释）时优先走
    _transcribe_elevenlabs；ElevenLabs 没配密钥、或配了但调用失败（401/限流/
    网络异常等，任何原因）都会回退到本地 faster-whisper，而不是直接放弃——
    只有本地 faster-whisper 也失败时才真正返回 None。

    faster-whisper/PyAV 对损坏/非视频输入会直接抛 av.error.InvalidDataError
    之类的异常（实测），不会走 ToolResult(success=False)；调用方拿到 None 再
    决定降级还是报错，而不是被底层异常炸穿。
    """
    import os as _os

    from tools.base_tool import ToolResult

    # Fix C18（2026-07-17，video-use 的 SKILL.md 明确写过这条教训——"Cache
    # transcripts per source. Never re-transcribe unless the source file
    # itself changed"——whatsapp_mvp 一直没有这层缓存）：同一个 job 里
    # _safe_transcribe 最多被调用 4 次（remove_filler / add_subtitles /
    # apply_style / transcribe_segments），每次的 src 是流水线上不同阶段的
    # 产物（原始输入 / 剪过口误的 / 过完 face+color+audio 增强的……），字节
    # 内容互不相同，天真按文件内容/路径做缓存 key 完全不会命中。但只要
    # remove_filler 没有真的剪任何东西（没有口误/复述需要去掉——真实视频里
    # 相当常见），从它到 apply_style 之间讲的话、每个词的时间戳全都没变，
    # 中间的增强步骤全部用 -fps_mode cfr（Rule 11 已经确认过）保时长不变，
    # 只是重新编码了画质/音质——这种情况下重新转写一次纯粹是浪费配额。
    # 用时长做安全的等价判断：只有当前 src 的时长跟缓存里记录的时长几乎
    # 相等（<0.05s 误差）才命中——这基本等价于"帧数完全一致"，比路径/mtime
    # 更能反映"内容真的没变"，且一旦剪过东西时长必然不同，缓存不会被误用
    # 到那种情况（那种情况本来就应该、也确实会重新转写）。缓存范围只到
    # workdir（即单个 job），不跨 job，不会有内容混淆的风险。
    cache_path = workdir / "_transcript_cache.json"
    try:
        src_duration = _probe_duration(Path(src))
    except Exception:
        src_duration = -1.0
    if cache_path.exists() and src_duration > 0:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if abs(cached.get("duration_seconds", -999) - src_duration) < 0.05:
                logger.info(
                    f"  转写命中缓存（时长 {src_duration:.2f}s 与缓存一致，"
                    "视为同一段语音内容的另一次重新编码，跳过重新转写）"
                )
                return ToolResult(success=True, data=cached["data"])
        except (json.JSONDecodeError, OSError, KeyError):
            pass  # 缓存损坏/格式不对就当没有，走下面正常转写，不阻断流程

    def _save_cache(data: dict) -> None:
        if src_duration <= 0:
            return
        try:
            cache_path.write_text(
                json.dumps({"duration_seconds": src_duration, "data": data}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # 写缓存失败不影响本次转写结果，只是下次少一次命中机会

    def _apply_croll_correction(data: dict) -> None:
        """workdir 下有 croll_script.txt（generate_croll 写的已知口播原文）
        时，用它纠正这次转写结果的文字——就地修改 data["segments"]。"""
        script_path = workdir / "croll_script.txt"
        if not script_path.exists():
            return
        try:
            script = script_path.read_text(encoding="utf-8")
            data["segments"] = _correct_transcript_against_script(data["segments"], script)
        except OSError:
            pass

    config = get_config()
    if config.transcribe_provider == "elevenlabs" and config.elevenlabs_api_key:
        t = _transcribe_elevenlabs(src, config.elevenlabs_api_key)
        if t.success:
            _apply_croll_correction(t.data)
            _save_cache(t.data)
            return t
        # 确认过的真实生产 bug：ElevenLabs 密钥"配了但被拒绝"(401/过期/限流/
        # 网络异常)时，以前直接在这里 return None——调用方把这当成"完全没有
        # 转写"，整条视频降级成无字幕、无任何图形，即使转写本来是可以靠本地
        # faster-whisper 顶上的。只有"没配密钥"这一种情况以前会走到下面的
        # 本地回退分支；"配了但用不了"反而是更常见、更该有回退的那种失败。
        # ElevenLabs 调用失败时也一样回退到本地，而不是直接放弃整条视频的
        # 字幕/图形——质量略降(faster-whisper 在识别复述片段上确实弱一些，
        # 见 _transcribe_elevenlabs 的文档注释)，但远好于完全没有。
        logger.warning(f"  ElevenLabs 转写失败({t.error})，回退到本地 faster-whisper")
    elif config.transcribe_provider == "elevenlabs":
        logger.warning("  transcribe_provider=elevenlabs 但没配 ELEVENLABS_API_KEY，回退到本地 faster-whisper")

    from tools.analysis.transcriber import Transcriber

    _hf = _os.environ.pop("HF_TOKEN", None)
    try:
        with _TRANSCRIBE_SLOTS:  # Whisper 是 CPU 大户，跨任务串行
            t = Transcriber().execute({
                "input_path": src,
                "output_dir": str(workdir),
                "model_size": model_size,
                "hotwords": hotwords,
                "realign": os.getenv("OM_FORCED_ALIGNMENT", "true").lower() == "true",
            })
    except Exception as e:
        logger.warning(f"  转写调用异常: {e}")
        return None
    finally:
        if _hf is not None:
            _os.environ["HF_TOKEN"] = _hf

    if not t.success:
        logger.warning(f"  转写失败: {t.error}")
        return None
    _apply_croll_correction(t.data)
    _save_cache(t.data)
    return t


_DEFAULT_SPEAKER_OBJECT_POSITION = "50% 35%"

# 架构复审后新增（2026-07-28）：统计过本机全部 33 次人脸校准记录，23 次
# （70%）落在 Y=51-52% 附近——这个区间就是这一晚反复触发"脸部被裁切"降级
# 的那个区间（job_cb04960d9a48/job_5b0ec0b914ee/job_7a33f9a80af8 三个 job
# 都是这个校准值）。根因：compose-director.md 规定的公式（下面 docstring
# 里那行）直接把人脸在**原始视频**里的位置占比，原样套用成卡片里的取景
# 位置——这个换算只在"原视频里人脸的相对位置刚好也适合卡片"时凑巧成立，
# 对着摄像头略低于视线拍摄（很常见的笔记本摄像头角度）的视频，人脸中心
# 天然落在画面偏下的位置，直接套用就会让卡片的裁剪窗口偏下、把头顶切掉。
# 极端情况更明显：还观测到两次 92%/79-80% 的校准结果，明显是误检测（背景
# 里的物体/画面边角），不加边界的话这类误检测会直接产出完全不能用的取景。
#
# 钳制边界不是拍脑袋定的：qa_stills._VISION_CHECKLIST 第一条本来就明确写了
# "脸应在其卡片顶部 20-40% 位置"——这是视觉复审自己拿来判断取景好坏的标准，
# 直接拿来当校准结果的钳制区间，跟下游判断口径完全一致。X 方向的钳制只是
# 防止误检测产生的极端值，观测到的正常范围都在 42-53% 附近，30-70% 给了
# 足够宽的余量，不会影响任何正常校准结果。
_CALIBRATION_Y_MIN, _CALIBRATION_Y_MAX = 20.0, 40.0
_CALIBRATION_X_MIN, _CALIBRATION_X_MAX = 30.0, 70.0


def _clamp_calibrated_object_position(raw_x_pct: float, raw_y_pct: float) -> tuple[str, bool]:
    """纯函数，方便直接单测，不用 mock FaceTracker。返回 (钳制后的 CSS
    object-position 字符串, 这次是否真的被钳制过)。"""
    clamped_x = max(_CALIBRATION_X_MIN, min(raw_x_pct, _CALIBRATION_X_MAX))
    clamped_y = max(_CALIBRATION_Y_MIN, min(raw_y_pct, _CALIBRATION_Y_MAX))
    was_clamped = abs(clamped_x - raw_x_pct) > 0.5 or abs(clamped_y - raw_y_pct) > 0.5
    return f"{round(clamped_x)}% {round(clamped_y)}%", was_clamped


def calibrate_speaker_object_position(src: str, workdir: Path) -> str:
    """对源视频跑 face_tracker，取人脸中心中位数 -> CSS object-position。

    compose-director.md 的强制校准项：objPos ≈ face_center_y/source_height*100，
    不同源视频没有通用值。检测不到人脸/缺 opencv 时退回静态默认值。
    （注意本机 opencv-python 必须 <5：5.0 wheel 不带 Haar cascade。）

    计算完原始公式后钳制到 [_CALIBRATION_X_MIN, _CALIBRATION_X_MAX] x
    [_CALIBRATION_Y_MIN, _CALIBRATION_Y_MAX]——见上面模块级注释，这个区间
    直接取自视觉复审自己的取景判断标准，不是新发明的口径。
    """
    try:
        from tools.analysis.face_tracker import FaceTracker

        out_json = workdir / "_op_apply_style_faces.json"
        with _ENHANCE_SLOTS:  # opencv 人脸检测也是 CPU 大户，跨任务串行
            r = FaceTracker().execute({
                "input_path": src, "output_path": str(out_json), "sample_fps": 3,
            })
        if not r.success:
            logger.warning(f"  apply_style: face_tracker 失败，用默认取景: {r.error}")
            return _DEFAULT_SPEAKER_OBJECT_POSITION
        data = json.loads(Path(r.data["output"]).read_text(encoding="utf-8"))
        faces = data.get("faces", [])
        if not faces:
            logger.warning("  apply_style: 没检测到人脸，用默认取景")
            return _DEFAULT_SPEAKER_OBJECT_POSITION
        centers_x = sorted(f["bbox"]["x"] + f["bbox"]["width"] / 2 for f in faces)
        centers_y = sorted(f["bbox"]["y"] + f["bbox"]["height"] / 2 for f in faces)
        mid = len(faces) // 2
        raw_x, raw_y = centers_x[mid] * 100, centers_y[mid] * 100
        obj_pos, was_clamped = _clamp_calibrated_object_position(raw_x, raw_y)
        if was_clamped:
            logger.info(
                f"  apply_style: 人脸校准取景原始值 {raw_x:.0f}% {raw_y:.0f}% 超出合理区间，"
                f"钳制为 {obj_pos}（{len(faces)}帧检出，取中位数）"
            )
        else:
            logger.info(f"  apply_style: 人脸校准取景 -> {obj_pos}（{len(faces)}帧检出，取中位数）")
        return obj_pos
    except Exception as e:
        logger.warning(f"  apply_style: face_tracker 调用异常，用默认取景: {e}")
        return _DEFAULT_SPEAKER_OBJECT_POSITION


_MAX_CAPTION_WORDS = 7
_MAX_CAPTION_CHARS = 42

# 确认过的真实 bug（2026-07-27，job_f1eec580e3c7 真实渲染出的成片）：整段字幕
# 连成一坨（"Hithere,it'sDavidfromPacificLife."），逐词卡拉OK高亮完全消失。
# 根因是拼接约定被打破，不是新 bug 的新写法——faster-whisper 原生词表每个
# 词自带前导空格（" Hi"/" there,"），下面原来一直用 "".join(...) 直接拼接、
# 靠这个前导空格分隔词与词；ElevenLabs 那条路径也刻意把 spacing token 的
# 文本搬到下一个词头上维持同一约定（见上面 elevenlabs 转写函数的注释）。
# 但今天默认开启的 WhisperX 强制对齐（forced_alignment.py，2026-07-24 加的
# 精度优化）重新计算词级时间戳时，产出的词表是 whisperx 自己的干净分词，
# **不带**前导空格——"".join(...) 因此把整句焊死成一个无空格字符串。
# Captions.tsx 的逐词高亮完全靠 text.indexOf(" ", ...) 找词边界，没有空格
# 就永远找不到，只能在整句结尾突然一次性点亮——用户看到的正是这个症状。
#
# 与其要求"以后任何词级时间戳的产出者都必须记得嵌入前导空格"这种容易被
# 破坏的隐性约定（forced_alignment 这次已经证明了它会被破坏——而且破坏得
# 很安静，没有任何测试或类型检查能拦住），不如让消费方自己彻底不依赖这个
# 约定：先 strip 掉每个词 token 自带的任何空白，再按字符集自行判断该不该
# 加空格——中日韩文字之间原生不加空格，其余按正常西文词间距处理。不管未来
# 换成哪个转写/对齐后端、输出词表带不带前导空格，这里都能拼出正确文本。
_CJK_CHAR_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿぀-ヿ가-힯]")


def _append_word_token(parts: list[str], token: str) -> None:
    token = str(token).strip()
    if not token:
        return
    if not parts:
        parts.append(token)
        return
    prev_char = parts[-1][-1:]
    cur_char = token[:1]
    if _CJK_CHAR_RE.match(prev_char) or _CJK_CHAR_RE.match(cur_char):
        parts.append(token)
    else:
        parts.append(" " + token)


def _words_to_caption_text(ws: list[dict]) -> str:
    parts: list[str] = []
    for w in ws:
        _append_word_token(parts, w.get("word", ""))
    return "".join(parts).strip()


def build_caption_phrases(words: list[dict], segments: list[dict]) -> list[dict]:
    """词级时间戳 -> 短语级字幕（≤7词/42字符或句读断句）。

    直接用转写 segment 当字幕一条能到 200+ 字符、屏上 5-6 行——codex 要求
    phrase-level。没有词级数据时退回 segment 级。
    """
    if not words:
        return [
            {"text": seg["text"].strip(), "startMs": round(seg["start"] * 1000), "endMs": round(seg["end"] * 1000)}
            for seg in segments
            if seg.get("text", "").strip()
        ]
    phrases: list[dict] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        text = _words_to_caption_text(cur)
        if text:
            phrases.append({
                "text": text,
                "startMs": round(cur[0]["start"] * 1000),
                "endMs": round(cur[-1]["end"] * 1000),
            })
        cur.clear()

    for w in words:
        cur.append(w)
        text = _words_to_caption_text(cur)
        ends_sentence = text.endswith((".", "?", "!", "。", "？", "！", ",", "，"))
        if len(cur) >= _MAX_CAPTION_WORDS or len(text) >= _MAX_CAPTION_CHARS or ends_sentence:
            flush()
    flush()
    return phrases


def _op_remove_filler(src: str, op: dict, workdir: Path) -> Optional[str]:
    """转写(词级) -> LLM 判断口误/重录 -> VideoTrimmer concat 只保留干净片段。

    这是 video-use / edit-director.md 的思路："不用打分公式，让 LLM 读转写稿
    自己判断哪里该剪"——跟 remove_silences 的纯静音检测是互补的两件事，静音
    检测测不到有声的语气词、也测不到中间没停顿的重录。LLM 不可用、判断没有
    需要剪的地方、或转写本身失败时，都原样返回，不影响后续步骤。
    """
    from tools.video.video_trimmer import VideoTrimmer

    from .content_planner import plan_filler_removal

    config = get_config()

    t = _safe_transcribe(src, workdir, config.faster_whisper_model)
    if t is None:
        logger.info("  remove_filler: 转写不可用，跳过口误检测（视频不变）")
        return None

    words = t.data.get("word_timestamps") or []
    duration = _probe_duration(Path(src))
    keep_ranges = plan_filler_removal(words, duration)
    if not keep_ranges:
        logger.info("  remove_filler: 没有判断出需要剪的口误/重录，跳过（视频不变）")
        return None

    segments = [{"input_path": src, **r} for r in keep_ranges]
    out = workdir / "_op_nofiller.mp4"
    r = VideoTrimmer().execute({"operation": "concat", "segments": segments, "output_path": str(out)})
    if not r.success:
        raise RuntimeError(f"remove_filler 剪辑失败: {r.error}")
    return r.data.get("output") or (r.artifacts[0] if r.artifacts else str(out))


def _op_speed_up_silence(src: str, op: dict, workdir: Path) -> Optional[str]:
    """把静音段加速而非删除 -> SilenceCutter mode=speed_up。"""
    from tools.video.silence_cutter import SilenceCutter
    out = workdir / "_op_speedsilence.mp4"
    inputs = {"input_path": src, "mode": "speed_up", "output_path": str(out)}
    factor = _num(op.get("factor"))
    if factor and factor > 1:
        inputs["silence_speed_factor"] = factor
    r = SilenceCutter().execute(inputs)
    if not r.success:
        raise RuntimeError(f"speed_up_silence 失败: {r.error}")
    return r.data.get("output") or (r.artifacts[0] if r.artifacts else None)


def _op_trim_leading_silence(src: str, op: dict, workdir: Path) -> Optional[str]:
    """只去掉开头静音 -> SilenceCutter mark 定位开头静音段，再 VideoTrimmer cut。

    直接用检测到的第一段静音 silences[0]：若它就在开头（起点 <0.5s）且够长，
    就从它结尾附近下刀。**不要**用 speech_segments[0].start —— 工具算 speech 时
    带 0.08s padding，即便开头是静音，speech_segments 也会有一个 [0, padding] 的
    微小段，导致起点恒为 ~0、永远被判成“开头无静音”（这正是之前恒 no-op 的原因）。
    """
    from tools.video.silence_cutter import SilenceCutter
    from tools.video.video_trimmer import VideoTrimmer
    mark_json = workdir / "_op_silence_mark.json"
    r = SilenceCutter().execute({
        "input_path": src, "mode": "mark", "output_path": str(mark_json),
        "silence_threshold_db": -30, "min_silence_duration": 0.35,
    })
    if not r.success:
        logger.warning(f"  trim_leading_silence: mark 失败 {r.error}")
        return None
    # 无静音时工具返回的是原 mp4 路径（非 JSON），直接跳过，避免把二进制当文本读崩溃
    if r.data.get("silence_segments", 0) == 0:
        logger.info("    trim_leading_silence: 未检测到静音，跳过")
        return None
    mark_out = str(r.data.get("output", ""))
    if not mark_out.endswith(".json"):
        return None
    try:
        data = json.loads(Path(mark_out).read_text(encoding="utf-8"))
        silences = data.get("silences", [])
    except Exception as e:
        logger.warning(f"  trim_leading_silence: 读取 mark 结果失败 {e}")
        return None
    if not silences:
        return None
    first = silences[0]
    lead_start = _num(first.get("start")) or 0.0
    lead_end = _num(first.get("end")) or 0.0
    # 第一段静音必须就在开头（起点 <0.5s）且时长 >=0.3s，才算“开头空白”
    if lead_start > 0.5 or (lead_end - lead_start) < 0.3:
        logger.info(f"    trim_leading_silence: 开头无明显静音"
                    f"（首段静音 {lead_start:.2f}~{lead_end:.2f}s），跳过")
        return None
    # 留 0.15s 缓冲，避免切掉第一个字的起音
    cut_at = max(0.0, lead_end - 0.15)
    if cut_at < 0.3:
        return None
    logger.info(f"    trim_leading_silence: 剪掉开头 0~{cut_at:.2f}s 的静音")
    out = workdir / "_op_trim_lead.mp4"
    tr = VideoTrimmer().execute({
        "operation": "cut", "input_path": src,
        "start_seconds": cut_at, "codec": "libx264", "output_path": str(out),
    })
    if not tr.success:
        raise RuntimeError(f"trim_leading_silence 裁剪失败: {tr.error}")
    return tr.artifacts[0] if tr.artifacts else str(out)


def _op_reframe(src: str, op: dict, workdir: Path) -> Optional[str]:
    """转换画幅（默认竖屏 9:16）-> AutoReframe，自带人脸跟踪居中。"""
    from tools.video.auto_reframe import AutoReframe
    aspect = str(op.get("aspect") or "portrait").lower()
    alias = {
        "9:16": "portrait", "vertical": "portrait", "竖屏": "portrait",
        "shorts": "portrait", "reels": "portrait", "tiktok": "portrait",
        "1:1": "square", "方形": "square",
        "16:9": "landscape", "横屏": "landscape",
        "21:9": "cinematic", "4:5": "vertical_4_5",
    }
    aspect = alias.get(aspect, aspect)
    if aspect not in ("portrait", "square", "landscape", "cinematic", "vertical_4_5"):
        aspect = "portrait"
    out = workdir / "_op_reframe.mp4"
    r = AutoReframe().execute({
        "input_path": src, "target_aspect": aspect, "output_path": str(out),
    })
    if not r.success:
        raise RuntimeError(f"reframe 失败: {r.error}")
    # 源画幅已匹配时工具会返回原文件路径
    return r.data.get("output") or (r.artifacts[0] if r.artifacts else None)


def _op_color_grade(src: str, op: dict, workdir: Path) -> Optional[str]:
    """整片调色 -> OpenMontage ColorGrade（ffmpeg profile/LUT，薄封装）。
    收尾类整片变换（像 reframe，不改时长），排在剪辑类操作之后。"""
    from tools.enhancement.color_grade import ColorGrade
    profile = str(op.get("profile") or "cinematic_warm").lower()
    valid = {"cinematic_warm", "cinematic_cool", "moody_dark",
             "bright_clean", "vintage_film", "high_contrast", "neutral"}
    if profile not in valid:
        profile = "cinematic_warm"
    # 默认略低于 1.0：ColorGrade 自己的 review-focus 提醒防止肤色过饱和
    intensity = op.get("intensity", 0.85)
    out = workdir / "_op_color_grade.mp4"
    r = ColorGrade().execute({
        "input_path": src, "output_path": str(out),
        "profile": profile, "intensity": intensity,
    })
    if not r.success:
        raise RuntimeError(f"color_grade 失败: {r.error}")
    return r.data.get("output") or (r.artifacts[0] if r.artifacts else None)


_BROLL_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def _op_insert_broll(src: str, op: dict, workdir: Path) -> Optional[str]:
    """把用户上传的 b-roll 叠进成片（单次 ffmpeg 多层合成，保留说话人原声）。
    资产文件在 workdir/assets/broll_<asset_ref>.*（Phase 1 已下载）。

    输出方向自动跟随素材：主视频或任一 b-roll 为横屏 → 横屏画布，否则跟随主视频；
    op.orientation ∈ {portrait,landscape} 可强制覆盖（用户说“横屏/竖屏”时规划器给）。
    每段 mode：
      - broll_main（默认）：b-roll 铺满画布，人物缩成右下小窗。
      - cutaway：b-roll 铺满画布，不叠人物。
      - pip：人物打底铺满，b-roll 缩成右下小窗（旧版式）。
    讲话（无 b-roll）的时段：人物按画布方向居中，方向不符则两边/上下留黑。
    整片收尾类，排在剪辑之后。"""
    items = op.get("items") or []
    if not items:
        return None
    assets_dir = workdir / "assets"
    base_w, base_h = _probe_dimensions(Path(src))
    if not base_w or not base_h:
        raise RuntimeError("insert_broll: 无法读取主视频画幅")

    resolved: list[dict] = []
    any_landscape = base_w > base_h
    for it in items:
        ref = it.get("asset_ref")
        start = _num(it.get("start_seconds"))
        end = _num(it.get("end_seconds"))
        has_gen = bool(it.get("gen_prompt"))
        if start is None or end is None or end <= start:
            continue
        if ref is None and not has_gen:
            continue
        ref_key = ref if ref is not None else f"gen{int(round(start))}s"
        # gen_force：用户明确要重新生成时，先删掉本 job 已缓存的生成片再重生成。
        # 仅对纯生成项（ref is None）生效——绝不删用户上传的素材；并按真值语义判断，
        # 避免字符串 "false" 被当成真而误触发重生成。
        _fv = it.get("gen_force")
        _force = (_fv.strip().lower() in ("true", "1", "yes")) if isinstance(_fv, str) else bool(_fv)
        if has_gen and ref is None and _force:
            for _stale in assets_dir.glob(f"broll_{ref_key}.*"):
                try:
                    _stale.unlink()
                except OSError:
                    pass
        matches = sorted(assets_dir.glob(f"broll_{ref_key}.*"))
        if matches and has_gen:
            logger.info(f"  insert_broll: 复用已生成的 b-roll {matches[0].name}（同一 job 不重复生成，省成本）")
        # gen_prompt: 没上传素材但给了文字 prompt 且无缓存 → 用所选 provider 生成一段再合成
        if not matches and has_gen:
            from .broll_providers import generate_broll_via
            assets_dir.mkdir(parents=True, exist_ok=True)
            _gen_out = assets_dir / f"broll_{ref_key}.mp4"
            _gen_result = generate_broll_via(it.get("gen_provider", "omni"), it["gen_prompt"], _gen_out,
                                             aspect=("9:16" if base_h >= base_w else "16:9"))
            if _gen_result:
                matches = [_gen_out]
                _record_generation_cost(workdir, f"insert_broll[{ref_key}]", _gen_result.get("cost_usd") or 0.0)
            else:
                logger.warning(f"  insert_broll: gen_prompt 生成失败（provider={it.get('gen_provider','omni')}），跳过该段")
        if not matches:
            logger.warning(f"  insert_broll: 找不到资产 broll_{ref_key}.*，跳过")
            continue
        asset = matches[0]
        is_image = asset.suffix.lower() in _BROLL_IMG_EXTS
        if not is_image:
            # 视频素材：窗口收到不超过素材本身时长，避免叠加末尾冻帧
            clip_dur = _probe_duration(asset)
            if clip_dur and (end - start) > clip_dur:
                end = start + clip_dur
        bw, bh = _probe_dimensions(asset)
        if bw and bh and bw > bh:
            any_landscape = True
        mode = str(it.get("mode") or "broll_main").lower()
        if mode not in ("broll_main", "cutaway", "pip"):
            mode = "broll_main"
        resolved.append({"path": str(asset), "start": start, "end": end,
                         "is_img": is_image, "mode": mode})
    if not resolved:
        return None

    # presenter 模式：把 broll_main 项转成 cutaway（卡片只放 b-roll、不烧人物），
    # 并记下人物视频与各 b-roll 时间窗，交给 apply_style 在模板下方渲染人物小窗。
    if op.get("_presenter"):
        _pwins = []
        for _r in resolved:
            if _r["mode"] == "broll_main":
                _r["mode"] = "cutaway"
                _pwins.append({"start": _r["start"], "end": _r["end"]})
        if _pwins:
            (workdir / "_presenter.json").write_text(
                json.dumps({"person_src": Path(src).name, "windows": _pwins}, ensure_ascii=False),
                encoding="utf-8")

    orientation = str(op.get("orientation") or "auto").lower()
    if orientation not in ("portrait", "landscape"):
        orientation = "landscape" if any_landscape else "portrait"
    out_w, out_h = (1920, 1080) if orientation == "landscape" else (1080, 1920)

    out = workdir / "_op_insert_broll.mp4"
    _composite_broll(src, resolved, out, out_w, out_h,
                     ins_h=round(out_h * 0.28), margin=round(out_w * 0.03))
    return str(out)


def _has_audio_stream(path: str) -> bool:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True,
        )
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def _op_add_music(src: str, op: dict, workdir: Path) -> Optional[str]:
    """配一段背景音乐，压低音量混进原声底下（保留说话人原声不变响）。

    **仅当用户明确要求背景音乐/BGM/配乐时才会被规划器放进方案**（见
    agent_editor.py 的触发说明）——不是默认行为，音乐会喧宾夺主也可能不合
    用户口味，不能替用户做这个决定。

    失败（provider 找不到匹配曲目、ffmpeg 混音出错）一律 raise，交给上面
    已有的 _DEGRADABLE_OPS 降级交付逻辑处理——不静默丢失这一步，用户会
    在预览消息里被明确告知"背景音乐没配成，可回复 retry 重试"。
    """
    query = (op.get("query") or "").strip()
    if not query:
        return None

    from .music_providers import fetch_music_via
    music_path = workdir / "_bgm_source.mp3"
    result = fetch_music_via(op.get("provider", "pixabay"), query, music_path)
    if not result:
        raise RuntimeError(f"add_music: 没找到匹配「{query}」的背景音乐")

    duration = _probe_duration(Path(src))
    if duration <= 0:
        raise RuntimeError("add_music: 无法读取主视频时长")

    # 音量：默认压到约 -15dB，明显是"底下垫着"的分量，不盖过说话人原声。
    # 用户如果具体说了"再小声点/再大声点"，规划器可以给 op.volume 覆盖，
    # 夹在合理区间内防止误设成 0（听不见）或 1（跟原声一样响、糊成一团）。
    vol = _num(op.get("volume"))
    vol = min(max(vol, 0.05), 0.4) if vol else 0.18

    fade_in = min(1.5, duration / 6)
    fade_out = min(2.0, duration / 4)
    fade_out_start = max(0.0, duration - fade_out)

    out = workdir / "_op_add_music.mp4"
    music_chain = (
        f"[1:a]volume={vol}[mv];"
        f"[mv]atrim=0:{duration:.3f}[mt];"
        f"[mt]afade=t=in:st=0:d={fade_in:.2f}[mfi];"
        f"[mfi]afade=t=out:st={fade_out_start:.2f}:d={fade_out:.2f}[bgm]"
    )
    if _has_audio_stream(src):
        filter_complex = f"{music_chain};[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        audio_map = "[aout]"
    else:
        # 主视频没有音轨（罕见，防御性分支）：背景音乐就是唯一音轨，不用混音
        filter_complex = music_chain
        audio_map = "[bgm]"

    cmd = [
        "ffmpeg", "-y", "-i", src, "-stream_loop", "-1", "-i", str(music_path),
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", audio_map,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"add_music: ffmpeg 混音失败: {(r.stderr or '')[-300:]}")

    _record_generation_cost(workdir, "add_music", result.get("cost_usd") or 0.0)
    return str(out)


def _composite_broll(src: str, resolved: list, out: Path,
                     out_w: int, out_h: int, ins_h: int, margin: int) -> None:
    """单次 ffmpeg filter_complex：人物 pillarbox 打底 + 每段 b-roll 按 mode 叠加。
    输入 0=人物（含原声/已烧字幕）；1..N=各 b-roll（图片用 -loop 输入）。"""
    inputs: list[str] = ["-i", str(src)]
    for r in resolved:
        if r["is_img"]:
            inputs += ["-loop", "1", "-t", f'{max(0.1, r["end"] - r["start"]):.3f}', "-i", r["path"]]
        else:
            inputs += ["-i", r["path"]]

    main_idx = [i for i, r in enumerate(resolved) if r["mode"] == "broll_main"]
    ins_map = {idx: k for k, idx in enumerate(main_idx)}
    # 归一化：把每一路都统一成 yuv420p / SAR=1 / 30fps。真实手机/录屏素材的像素格式、
    # 采样宽高比、帧率各不相同，overlay 混合异质流会在部分 ffmpeg 上报 "-22 Invalid
    # argument / no packets"。统一后即可稳定合成。
    _norm = ",format=yuv420p,setsar=1,fps=30"
    fc: list[str] = []
    # 人物拆流：1 路打底 + 每个 broll_main 一路小窗（未用的 split 输出会导致 ffmpeg 报错，故精确计数）
    split_outs = "[spk_base]" + "".join(f"[ins{k}]" for k in range(len(main_idx)))
    fc.append(f"[0:v]split={1 + len(main_idx)}{split_outs}")
    fc.append(f"[spk_base]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
              f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black{_norm}[base]")
    for i, r in enumerate(resolved):
        vin = f"{i + 1}:v"
        offset = "" if r["is_img"] else f",setpts=PTS-STARTPTS+{r['start']}/TB"
        if r["mode"] == "pip":
            boxw = int(out_w * 0.38)
            fc.append(f"[{vin}]scale={boxw}:-2{offset}{_norm}[bro{i}]")
        else:  # broll_main / cutaway：铺满画布（等比放大后居中裁切）
            fc.append(f"[{vin}]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                      f"crop={out_w}:{out_h}{offset}{_norm}[bro{i}]")
        if r["mode"] == "broll_main":
            fc.append(f"[ins{ins_map[i]}]scale=-2:{ins_h}{_norm}[insv{i}]")
    cur = "base"
    for i, r in enumerate(resolved):
        s, e = r["start"], r["end"]
        if r["mode"] == "pip":
            fc.append(f"[{cur}][bro{i}]overlay=W-w-{margin}:H-h-{margin}:enable='between(t,{s},{e})'[c{i}]")
        elif r["mode"] == "cutaway":
            fc.append(f"[{cur}][bro{i}]overlay=0:0:enable='between(t,{s},{e})'[c{i}]")
        else:  # broll_main：先铺满，再叠人物小窗
            fc.append(f"[{cur}][bro{i}]overlay=0:0:enable='between(t,{s},{e})'[m{i}]")
            fc.append(f"[m{i}][insv{i}]overlay=W-w-{margin}:H*0.70-h:enable='between(t,{s},{e})'[c{i}]")
        cur = f"c{i}"
    fc.append(f"[{cur}]null[outv]")
    # 输出时长钉在主视频长度：b-roll 用 setpts 偏移后其流可能比主视频长（overlay 默认跟
    # 最长流），不钉住会把成片拉长、末尾是无人物的残留 b-roll。
    base_dur = _probe_duration(Path(src))
    cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", ";".join(fc),
           "-map", "[outv]", "-map", "0:a?",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    if base_dur and base_dur > 0:
        cmd += ["-t", f"{base_dur:.3f}"]
    cmd += [str(out), "-loglevel", "error"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"insert_broll 合成失败: {proc.stderr[-500:]}")


def _op_add_subtitles(src: str, op: dict, workdir: Path) -> Optional[str]:
    """转写 -> 烧录字幕（原语言）。翻译成其他语言暂不支持（见 planner 的 unsupported）。

    字幕是用户显式要的东西，转写失败没有"降级但有意义"的输出可给，所以仍然
    raise——但经 _safe_transcribe 收敛，报错干净而不是底层库异常炸穿。
    """
    from tools.video.remotion_caption_burn import RemotionCaptionBurn

    config = get_config()
    t = _safe_transcribe(src, workdir, config.faster_whisper_model)
    if t is None:
        raise RuntimeError("转写失败，无法烧录字幕")
    segments = t.data.get("segments")
    if not segments:
        logger.warning("  转写无结果，跳过字幕")
        return None

    out = workdir / "_op_subtitled.mp4"
    # 不 force_ffmpeg：本机 ffmpeg 没编 libass（subtitles 滤镜不存在，实测
    # exit 234/Filter not found），Remotion 烧录路径已验证可用，让工具自选。
    r = RemotionCaptionBurn().execute({
        "input_path": src, "output_path": str(out),
        "segments": segments,
    })
    if not r.success:
        raise RuntimeError(f"字幕烧录失败: {r.error}")
    return r.artifacts[0] if r.artifacts else str(out)


# Dominant/Workflow floating-card geometry — matches the numbers already used
# across video-studio's own compose-director.md-driven builds and
# XiaojinEditorial's own demo defaultProps (Root.tsx). content_planner only
# reasons about WHEN to be in which mode (dominant vs workflow), and (P3) how
# WIDE the on-screen content is at that moment; the actual pixel box a mode
# maps to is a rendering-layer concern, not a planning one.
_DOMINANT_BOX = {"x": 60, "y": 104, "w": 960, "h": 1100}
# Docked/side-pip geometry (content_width-dependent x/w narrowing) is gone —
# confirmed real user complaint against that whole model: shrinking WIDTH and
# docking the card to a side column leaves the entire opposite side and the
# whole lower half of the canvas empty, with only faint atmosphere text to
# fill it. video-studio's own validated reference (motion/vell-renewal-fresh's
# RenewalFresh/SpeakerCard.tsx) does the opposite: the card stays FULL WIDTH,
# anchored top-left at the exact same x/w as Dominant, and only HEIGHT shrinks
# — freeing up a full-width band BELOW the card (RenewalFresh's own
# CoverageSection.tsx: `CONTENT_TOP = 1040`, `left:40, right:40`) for content
# to stack into, rather than a narrow side column beside it. Adopting that
# model verbatim: Workflow keeps Dominant's x/w unchanged, only h differs, so
# the card doesn't even move horizontally on the Dominant<->Workflow
# transition — a pure vertical squeeze.
# h=900, straight from the reference (RenewalFresh WORKFLOW = 1000x900): at
# 960px wide the objectFit:cover crop is WIDTH-bound, so a shorter box shows
# LESS of the speaker vertically, not a smaller card — h=700 was a confirmed
# real bug that cropped the speaker to head-only. 900 shows face + chest.
_WORKFLOW_BOX = {"x": 60, "y": 104, "w": 960, "h": 900}
# Content zone directly below the Workflow card — full card width, starting
# just under its bottom edge (104+900=1004, +36px gap=1040 — the reference's
# own CONTENT_TOP). content_planner.py's data-display defaults must match
# these exactly — see that file's own copy of these same numbers.
_CONTENT_ZONE_X = 60
_CONTENT_ZONE_Y = 1040
_CONTENT_ZONE_WIDTH = 960
# Caller-supplied contentWidth of 920+ (full InfoCard/before_after/section
# width) is now only used to detect the SECTION_PIP_SENTINEL case (full-canvas
# takeover) -- it no longer drives any card-width narrowing (see above), so
# any ordinary value works identically. Kept as the default so a
# hand-authored op["mode_schedule"] entry that omits contentWidth still
# resolves to "regular workflow", not a section pip.
_WORKFLOW_DEFAULT_CONTENT_WIDTH = 920


# 全画布章节接管（sections）期间 SpeakerCard 直接淡出隐藏，不再缩成小 pip——
# 确认过的用户反馈：哪怕真小 pip（350x420 右下角）也逼着接管图形整体偏到左半
# 边去躲它，warning 图标显得"很偏"，右侧和下方大片留白。参考成片的接管章节
# 本来就是"图形拥有整个画布"；说话人这几秒消失完全可接受（音频还在继续）。
_TAKEOVER_FADE_FRAMES = 15


_TRANSITION_HOLD_FRAMES = 20  # 一次真实卡片变形动画的合理时长——见 _insert_transition_holds


def _insert_transition_holds(scenes: list[dict]) -> list[dict]:
    """Fix C21（2026-07-17，真实生产复现，同一天内两次——job_1b7254abcd66 的
    (180,592)、job_ac00838adea9 的 (180,298)/(758,1193)）：`interpolate()`
    只拿到两个尺寸不同的关键帧时，会在它们之间的*整段*间隔里连续插值——如果
    下一次真正的几何变化要再过几百帧才发生（例如一个全画布接管在很久之后
    才结束，卡片才收回 workflow 尺寸），卡片就被 Remotion 判定成"一直在缓慢
    变形"长达十几秒，其间任何真实内容挂上去都会被 element_mounts_during_
    card_transition 拦下来——即使卡片早就视觉上稳定在目标尺寸，只是数据里
    没有一个"提前到达"的关键帧去停住插值。

    今天早些时候试过反过来改 props_lint._transition_windows（把检测窗口
    强行缩短），但那是被三个当时已经验证过的用例依赖的公共函数，一动就
    连带破坏了 Fix C13b/C19 依赖的、真正需要"整段间隔都算过渡"这个语义的
    intro 场景（0→180 那种紧邻的两帧，是真的在整段内连续变形）——检测函数
    本身没错，错的是喂给它的数据在几何变化之后没有一个"到达并停住"的关键帧。

    这里改成从数据源头修：两个几何不同、且间隔超过 _TRANSITION_HOLD_FRAMES
    的相邻 scene 关键帧之间，插入一个"提前到达"的关键帧——跟后一个关键帧
    尺寸相同，落在前一个关键帧之后 _TRANSITION_HOLD_FRAMES 帧的位置。插值
    在这个新关键帧之前是真过渡（跟原来一样，短且合理），之后到下一个真实
    关键帧因为两端尺寸相同、不再被判定为过渡——不用改 _transition_windows
    这个已经验证过、被多处依赖的检测函数一个字，只是让它看到的数据更准确。

    Fix C39（2026-07-21，通过 replica harness 在 job_452ef6c48100 上反复复现，
    root-caused 后发现这才是 C31/C33/C38 追的那个"内容区图形撞上 Dominant"
    问题真正的最初来源）：上面这段插入逻辑对"缩小"（Dominant->Workflow）和
    "长大"（Workflow->Dominant）一视同仁，永远在 prev 之后 _TRANSITION_HOLD_
    FRAMES 帧提前到达 cur 的尺寸——这对缩小方向是对的（卡片碍事就该尽快让开），
    但对长大方向恰恰相反：workflow 状态之所以在 mode_schedule 里出现，就是
    因为这段时间*确实*有内容要占那块地方，长大回 Dominant 只应该发生在 cur
    自己的帧（内容真正结束的时刻），提前 20 帧就长回满屏，等于卡片在内容还
    显示着的时候就已经压上去了——这正是 element_over_card 反复抓到的那个 bug，
    而不是巧合。改成按方向区分：缩小方向保持原逻辑（提前到达+尽快让开）；
    长大方向反过来——插入的提前到达关键帧用 prev 的（更小的）尺寸，落在 cur
    帧*之前* _TRANSITION_HOLD_FRAMES 帧的位置，让卡片在小尺寸多停留到接近
    cur 自己的时刻才真正长大，而不是一进 workflow 没多久就被拉回满屏。
    """
    if not scenes:
        return scenes
    out = [scenes[0]]
    for cur in scenes[1:]:
        prev = out[-1]
        box_changed = prev.get("w") != cur.get("w") or prev.get("h") != cur.get("h")
        gap = cur["frame"] - prev["frame"]
        if box_changed and gap > _TRANSITION_HOLD_FRAMES:
            shrinking = cur["h"] < prev["h"]
            if shrinking:
                # 卡片变小：尽快让开，提前到达 cur 的（更小）尺寸并一直停在那，
                # 跟原逻辑一致。
                hold_frame = prev["frame"] + _TRANSITION_HOLD_FRAMES
                out.append({"frame": hold_frame, "x": cur["x"], "y": cur["y"], "w": cur["w"], "h": cur["h"]})
            else:
                # 卡片变大：workflow 期间内容还在占用这块地方，尽量保持 prev
                # 的（更小）尺寸，只在紧贴 cur 自己那一刻之前才真正长大——
                # 不能提前，提前就是直接压在还在显示的内容上面。
                hold_frame = cur["frame"] - _TRANSITION_HOLD_FRAMES
                if hold_frame > prev["frame"]:
                    out.append({"frame": hold_frame, "x": prev["x"], "y": prev["y"], "w": prev["w"], "h": prev["h"]})
        out.append(cur)
    return out


def _mode_schedule_to_scenes(mode_schedule: list[dict]) -> tuple[list[dict], list[dict]]:
    """content_planner 的 dominant/workflow 模式时间表 -> contract② 的
    (scenes, opacityKeyframes)。contentWidth>=SECTION_PIP_SENTINEL 的 workflow
    段是全画布章节接管：卡片几何保持 _WORKFLOW_BOX 不动（反正看不见，避免
    淡回来时从奇怪的位置飞入），透明度在段首淡出、在下一段开始时淡回。
    """
    from .content_planner import SECTION_PIP_SENTINEL  # lazy import, matches this file's existing pattern

    scenes: list[dict] = []
    opacity: list[dict] = []
    hidden = False
    for entry in mode_schedule:
        f = entry["frame"]
        is_workflow = entry.get("mode") == "workflow"
        is_takeover = is_workflow and entry.get(
            "contentWidth", _WORKFLOW_DEFAULT_CONTENT_WIDTH) >= SECTION_PIP_SENTINEL
        scenes.append({"frame": f, **(_WORKFLOW_BOX if is_workflow else _DOMINANT_BOX)})
        if is_takeover and not hidden:
            opacity += [{"frame": max(0, f - 1), "opacity": 1.0},
                        {"frame": f + _TAKEOVER_FADE_FRAMES, "opacity": 0.0}]
            hidden = True
        elif hidden and not is_takeover:
            opacity += [{"frame": f, "opacity": 0.0},
                        {"frame": f + _TAKEOVER_FADE_FRAMES, "opacity": 1.0}]
            hidden = False
    scenes = _insert_transition_holds(scenes)
    # interpolate() needs strictly increasing frames — drop any keyframe that
    # would violate that (e.g. two takeovers closer together than the fades).
    monotonic: list[dict] = []
    for k in opacity:
        if monotonic and k["frame"] <= monotonic[-1]["frame"]:
            continue
        monotonic.append(k)
    return scenes, monotonic


# props 字段名 -> _CONTENT_ZONE_WIDTH 的普通内容区图形类型（quotes/sections 单独
# 处理，它们是 SECTION_PIP_SENTINEL 隐藏语义，不是普通 workflow 内容）。
_WORKFLOW_CONTENT_PROP_KEYS = (
    "dataCards", "gauges", "countdowns", "calendarEvents", "beforeAfter",
    "pills", "stepLists", "topicCards",
    "comparisons", "rankedLists", "checklists", "locationPins", "testimonials", "iconClusters",
    "progressBars", "prosCons", "milestoneTracks", "trustBadges", "barCharts", "milestoneUnlocks",
)


def _recompute_scenes_from_content(props: dict, duration_frames: int) -> dict:
    """Fix C33（2026-07-20，真实生产复现——job_51f154a80f9b 交付版本的 vision QA
    直接抓到：一张 stepList 自己的存活区间(349-954)中途撞上一段从 551 帧开始的
    Dominant(满尺寸卡片)窗口，说话人卡片长回全尺寸，直接压在还在显示的步骤 1/2
    上面）；跟 content_planner.py 的 Fix C31 是同一类桥——props["scenes"]/
    opacityKeyframes 一旦从 mode_schedule 定型，后面任何再挪动/插入内容区图形
    的代码都必须重新调用这个函数，否则冻结的时间点跟图形最终真正落地的时间点
    不再是同一份真相。

    Fix C38（2026-07-21，通过 replica harness 在 job_452ef6c48100 上反复复现——
    C33 当初只在 `_build()` 内部调用一次，但 `_apply_deterministic_guarantees`
    调用的 C13(`_fill_intro_lead_dead_space`)/C15/C37 全部发生在 `_build()`
    *之后*：C13 插入的新 topicCard 用真实数据验证过会直接落在一段 Dominant
    窗口的开头——因为它插入时 scenes 早就是 C33 recompute 过的旧版本，而这张
    新卡片当然不在那次 recompute 的输入里。跟 C33 同一个教训，只是这次连
    "只调用一次"这个假设本身都是错的：任何插入/挪动内容区图形的步骤之后都要
    重算，不是只在 `_build()` 收尾时算一次就一劳永逸。

    从 props 的公开字段（dataCards/gauges/.../topicCards/quotes/sections，
    JSON key 名而非 Python 局部变量名，好让 `_build()` 内部和
    `_apply_deterministic_guarantees` 都能调用同一份实现）重新扫一遍
    workflow_ranges，跟 sections/quote 的隐藏(SECTION_PIP_SENTINEL)区间取并集
    再重算一次 mode_schedule -> scenes/opacityKeyframes。
    """
    from .content_planner import SECTION_PIP_SENTINEL, _workflow_mode_schedule

    ranges: list[tuple[int, int, int]] = []
    for key in _WORKFLOW_CONTENT_PROP_KEYS:
        for it in props.get(key) or []:
            if "mountFrame" in it and "endFrame" in it:
                ranges.append((it["mountFrame"], it["endFrame"], _CONTENT_ZONE_WIDTH))
    for q in props.get("quotes") or []:
        if "mountFrame" in q and "endFrame" in q:
            ranges.append((q["mountFrame"], q["endFrame"], SECTION_PIP_SENTINEL))
    for sec in props.get("sections") or []:
        ranges.append((sec["fromFrame"], sec["toFrame"], SECTION_PIP_SENTINEL))

    mode_schedule = _workflow_mode_schedule(ranges, duration_frames)

    # Fix C50（2026-07-21，同一支 job_452ef6c48100，验证 C47/C49 的下一次
    # 真实渲染里复现）：C47/C49 的避让检查在 `_build()` 里只跑了一次，用的
    # 是那一刻的 mode_schedule——但 `_recompute_scenes_from_content` 自己
    # 的文档（Fix C33/C38）说得很清楚："scenes/opacityKeyframes 一旦从
    # mode_schedule 定型，后面任何再挪动/插入内容区图形的代码都必须重新
    # 调用这个函数"。C47/C49 的避让调用本身就是"挪动内容区图形的代码"，
    # 但它们发生在 `_build()` 里最后一次 `_recompute_scenes_from_content`
    # 之前，而不是之后——真正定型的 mode_schedule（这里重算出来的这份）
    # 从未被拿去重新检查过 zoneHeaders/dataCards 会不会撞上它。这是
    # Rule 15 那条教训的又一次重演："最终保证"只有跑在真正最后一次改动
    # 之后才算数。修复：既然这个函数已经是 scenes 定型的唯一权威入口，
    # 避让检查也搬进来，用这里刚算出的、真正最终的 mode_schedule 重新跑
    # 一遍——跑完可能又轻微挪动了内容区图形的边界，所以再重算一次
    # ranges/mode_schedule/scenes，保证两者互相一致，而不是留下一份对不上
    # 号的 scenes。
    for key in _WORKFLOW_CONTENT_PROP_KEYS:
        _shift_off_dominant_windows(props.get(key), mode_schedule)
    _shift_off_dominant_windows_headers(props.get("zoneHeaders"), mode_schedule)

    ranges = []
    for key in _WORKFLOW_CONTENT_PROP_KEYS:
        for it in props.get(key) or []:
            if "mountFrame" in it and "endFrame" in it:
                ranges.append((it["mountFrame"], it["endFrame"], _CONTENT_ZONE_WIDTH))
    for q in props.get("quotes") or []:
        if "mountFrame" in q and "endFrame" in q:
            ranges.append((q["mountFrame"], q["endFrame"], SECTION_PIP_SENTINEL))
    for sec in props.get("sections") or []:
        ranges.append((sec["fromFrame"], sec["toFrame"], SECTION_PIP_SENTINEL))
    mode_schedule = _workflow_mode_schedule(ranges, duration_frames)

    props = dict(props)
    props["scenes"], opacity = _mode_schedule_to_scenes(mode_schedule)
    if opacity:
        props["opacityKeyframes"] = opacity
    else:
        props.pop("opacityKeyframes", None)
    return props


# QuoteCard 是唯一"solo"(占满整个画布)的图形类型——用户明确反馈过：不能
# 让它在视频刚开始、观众还没看到/听到说话人开口的这段时间内就上场盖脸。
# 7s 留出足够时间让片头标题卡（如果有）播完 + 说话人至少露脸说上一两句话。
_QUOTE_MIN_START_FRAMES = 210  # 7s @ 30fps


def _floor_shift_graphics(items: Optional[list[dict]], floor: int) -> None:
    """把 items 里每个图形的挂载时间整体钳到 floor 之后（原地修改）——整体
    平移 mountFrame/endFrame（以及 beforeAfter 自己的 secondRevealFrame），
    保留原有停留时长，而不是只把起点拉后却让终点留在原地压缩甚至压成负
    时长。count_up 的 rows[].mountOffset、step_list 的 steps[].activateOffset
    都是相对卡片自己 mountFrame 的相对值，卡片整体平移后自动保持正确，不用
    额外处理。

    Fix C2：确认过的真实 bug——intro 期间卡片保持 Dominant（未收起），但图形
    自己的 mountFrame 没有跟着 intro 的 mode_schedule 延迟一起往后挪，
    countdown 直接画在了还没让开位置的大卡上面。
    """
    for g in items or []:
        if not isinstance(g, dict) or "mountFrame" not in g:
            continue
        delta = floor - g["mountFrame"]
        if delta <= 0:
            continue
        g["mountFrame"] += delta
        if "endFrame" in g:
            g["endFrame"] += delta
        if "secondRevealFrame" in g:  # beforeAfter's own second-value beat
            g["secondRevealFrame"] += delta


def _floor_shift_zone_headers(headers: Optional[list[dict]], floor: int) -> None:
    """跟 _floor_shift_graphics 同样的整体平移，但 ZoneHeader 用的字段名是
    fromFrame/toFrame，不是 mountFrame/endFrame。"""
    for h in headers or []:
        if not isinstance(h, dict) or "fromFrame" not in h:
            continue
        delta = floor - h["fromFrame"]
        if delta <= 0:
            continue
        h["fromFrame"] += delta
        h["toFrame"] += delta


_CARD_TRANSITION_FRAMES = 20  # SpeakerCard.tsx 自己的收缩转场时长，跟 Fix C2 的 +20 同一个常量


def _mode_at(mode_schedule: list[dict], frame: int) -> str:
    """mode_schedule（按 frame 升序的状态变化点列表）在给定帧生效的模式。"""
    mode = "dominant"
    for entry in mode_schedule:
        if entry["frame"] > frame:
            break
        mode = entry.get("mode", mode)
    return mode


def _next_docked_frame(mode_schedule: list[dict], after_frame: int) -> Optional[int]:
    """after_frame（含）之后，卡片真正收缩完成（workflow 模式生效 +
    转场动画播完）的第一帧；后面再没有 workflow 窗口就返回 None。"""
    for entry in mode_schedule:
        if entry["frame"] >= after_frame and entry.get("mode") == "workflow":
            return entry["frame"] + _CARD_TRANSITION_FRAMES
    return None


def _next_dominant_grow_start(mode_schedule: list[dict], after_frame: int) -> Optional[int]:
    """after_frame（不含）之后，卡片下一次开始从 docked 长回 dominant 的第一
    帧——即真正开始变形的时刻，比 mode_schedule 自己的 dominant 时间戳早
    _CARD_TRANSITION_FRAMES（Rule 15/Fix C39：growing 转场提前那么多帧起
    步，只在真正的下一次转场前那 _CARD_TRANSITION_FRAMES 帧才开始长大）。
    后面再没有 dominant 窗口就返回 None。"""
    for entry in mode_schedule:
        if entry["frame"] > after_frame and entry.get("mode") == "dominant":
            return max(after_frame, entry["frame"] - _CARD_TRANSITION_FRAMES)
    return None


def _shift_off_dominant_windows(items: Optional[list[dict]], mode_schedule: list[dict]) -> None:
    """Fix C24（2026-07-20，真实生产复现——job_452ef6c48100，用户在渲染出的
    截图里直接抓到）：content-zone 元素（zoneHeaders/dataCards/gauges/...）
    的固定 Y 坐标（_CONTENT_ZONE_Y，见 content_planner.py）假设卡片此刻是
    docked（收起，矮）状态。Fix C2 只处理了片头这一次 dominant 窗口——但
    dominant/workflow 会在视频中段按内容反复交替（Fix C14 的注释已经说明
    这是"内容本身决定的真实时间点，不该跟着挪"这条设计本身没错），每一次
    卡片重新变回 Dominant（未收起，更高）都会重现同一个 bug：真实复现里
    COVERAGE、RISK 两个 zoneHeader 各自的整个显示区间都恰好落在了这样一次
    Dominant 窗口内，标题文字在真实渲染出的截图里直接叠在说话人身上，不是
    卡片下方的奶油区。

    这里不改 mode_schedule 本身（dominant/workflow 交替是内容驱动的真实
    时间点，改了就是在动 Fix C14 特意保留不动的东西）——而是反过来让
    content-zone 元素避让：任何 mountFrame 落在 dominant/hidden 窗口内的
    元素，顺延到卡片真正收缩完成的那一帧，跟 Fix C2 一样只平移不压缩
    （保留原有停留时长）。已经落在 workflow 窗口内的元素是 no-op。后面
    再没有 workflow 窗口了（最后一段一直是 Dominant 到片尾）就保持原状，
    没有更好的位置可躲。

    Fix C49（2026-07-21，同一支 job_452ef6c48100，验证 C47/C48 的同一次
    渲染里复现——跟 Fix C47 一模一样的形状，只是这次撞上的是普通的
    dataCard/countdown/gauge 而不是 zoneHeader）：这里原本也只检查
    mountFrame 那一刻的模式，没检查 endFrame 之前模式会不会变回
    dominant。跟 C47 用同一套修复：起点判定之后，再检查会不会撞上下一次
    dominant 增长，会的话把 endFrame 截短到长大真正开始之前。截短一段
    数字/图形的展示时间比让它跟长大中的卡片肉眼可见地叠在一起要好——跟
    C47 的取舍一致，只是这次代价从"装饰性标题少露 20 帧"变成"数据卡少
    露 20 帧"，仍然远好于真的叠在说话人脸上。
    """
    for g in items or []:
        if not isinstance(g, dict) or "mountFrame" not in g:
            continue
        # Fix C51（2026-07-22，真实生产复现 job_2729b2e0a795，用户直接在 WhatsApp
        # 上收到"品牌样式渲染没成功"，排查发现跟 Redis/ngrok/网关完全无关，是这里
        # 纯 Python 的 UnboundLocalError）：C49 把原来"提前 continue"的写法改成了
        # 嵌套 if，但 `delta` 只在"需要挪动"这个分支里赋值——mountFrame 已经在
        # workflow 窗口内（最常见、完全正常的情况）时整个分支被跳过，`delta` 从未
        # 赋值，一旦这个元素恰好带 secondRevealFrame 字段，下面就直接崩溃。这不是
        # 偶发的基础设施问题，是每次遇到"已经在 workflow 内 + 带 secondRevealFrame"
        # 这个组合就必现的代码 bug。修复：delta 提前初始化为 0——没发生挪动时，
        # secondRevealFrame 也不该被挪动，语义上正确，不只是消除崩溃。
        delta = 0
        if _mode_at(mode_schedule, g["mountFrame"]) != "workflow":
            target = _next_docked_frame(mode_schedule, g["mountFrame"])
            if target is None:
                continue
            delta = target - g["mountFrame"]
            if delta > 0:
                g["mountFrame"] += delta
                if "endFrame" in g:
                    g["endFrame"] += delta
        if "endFrame" in g:
            grow_start = _next_dominant_grow_start(mode_schedule, g["mountFrame"])
            if grow_start is not None and grow_start < g["endFrame"]:
                g["endFrame"] = max(g["mountFrame"], grow_start)
        if "secondRevealFrame" in g:
            g["secondRevealFrame"] += delta


def _shift_off_dominant_windows_headers(headers: Optional[list[dict]], mode_schedule: list[dict]) -> None:
    """跟 _shift_off_dominant_windows 同样的避让逻辑，ZoneHeader 的字段名是
    fromFrame/toFrame。

    Fix C47（2026-07-21，同一支 job_452ef6c48100 真实复现，就在 C46 验证
    的同一次渲染里）：C24 的原始版本只检查 fromFrame 那一刻的模式，判定
    "从 workflow 开始就没事"——但没检查 toFrame 之前模式会不会再变回
    dominant。真实案例：COVERAGE 的 zoneHeader fromFrame=381 时确实是
    workflow（判定 continue，跳过），但卡片在 toFrame=592 之前的 572 帧就
    已经开始长回 dominant（下一次真实转场提前 _CARD_TRANSITION_FRAMES 帧
    起步，Rule 15/Fix C39 的既有设计），于是 header 还在显示的最后 20 帧
    (572-592) 跟正在长大的卡片撞在一起——vision QA 真实抓到了这一帧，标题
    文字叠在了说话人身上。C24 的"只看起点"假设只对"进场后一直保持
    workflow 到 toFrame"这一种形状成立，这支视频的内容形状（COVERAGE 章节
    结束后紧接着又要变回 Dominant）恰好不满足。修复：起点判定之后，再单独
    检查 toFrame 之前会不会撞上下一次 dominant 增长——会的话把 toFrame
    （连同 EXIT_FRAMES 的退场空间由组件自己处理）截短到长大真正开始之前，
    只平移/截短，不影响 fromFrame 的正常入场时机。"""
    for h in headers or []:
        if not isinstance(h, dict) or "fromFrame" not in h:
            continue
        if _mode_at(mode_schedule, h["fromFrame"]) != "workflow":
            target = _next_docked_frame(mode_schedule, h["fromFrame"])
            if target is None:
                continue
            delta = target - h["fromFrame"]
            if delta > 0:
                h["fromFrame"] += delta
                h["toFrame"] += delta
        grow_start = _next_dominant_grow_start(mode_schedule, h["fromFrame"])
        if grow_start is not None and grow_start < h["toFrame"]:
            h["toFrame"] = max(h["fromFrame"], grow_start)


def _run_enhancement_chain(src: str, workdir: Path) -> str:
    with _ENHANCE_SLOTS:  # face/color/audio 增强都是 ffmpeg/模型重活，跨任务串行
        return _run_enhancement_chain_inner(src, workdir)


def _run_enhancement_chain_inner(src: str, workdir: Path) -> str:
    """face_enhance -> color_grade -> audio_enhance。

    架构复审发现（2026-07-24）：这三步原本是三次完全独立的 ffmpeg 调用，每次
    都对整段视频重新解码->滤镜->编码——但 face_enhance/color_grade 都只是
    单纯的 -vf 滤镜串，audio_enhance 是单纯的 -af 滤镜串，三者分别只碰视频流
    /音频流，天然可以合并成一次 ffmpeg 调用（-vf "人脸滤镜,调色滤镜" -af
    音频滤镜），把三次解码+编码压成一次。优先走合并路径；合并失败（任何原因：
    滤镜取不到、ffmpeg 报错、超时……）时退回原来久经考验的三步串行版本，
    单步失败互不影响的降级行为完全不变——合并只是性能优化，不改变行为保证。
    """
    try:
        return _run_enhancement_chain_combined(src, workdir)
    except Exception as e:
        logger.warning(f"  apply_style: 合并增强通道失败，退回三步串行: {e}")
        return _run_enhancement_chain_sequential(src, workdir)


def _run_enhancement_chain_combined(src: str, workdir: Path) -> str:
    """face_enhance + color_grade + audio_enhance 在一次 ffmpeg 调用里全部做完。

    直接复用三个工具各自的滤镜构造逻辑（_build_filter / PRESETS），只是不
    分别起 ffmpeg 进程——所以视觉/听觉效果跟三步串行版本应当逐帧一致，唯一
    区别是省掉两次多余的解码+编码。任何一步取不到滤镜串、或 ffmpeg 本身报错
    /超时，都整体抛异常交给调用方退回三步串行，不在这里做部分容错（部分容错
    在单次 ffmpeg 调用里做不到——一旦开始编码就没有"这步跳过、那步继续"的
    余地，这也是保留三步串行作为退路的原因）。
    """
    from tools.audio.audio_enhance import PRESETS as _AUDIO_PRESETS
    from tools.enhancement.color_grade import ColorGrade
    from tools.enhancement.face_enhance import FaceEnhance

    face_vf = FaceEnhance()._build_filter({"preset": "talking_head_standard"})
    color_vf = ColorGrade()._build_filter({"profile": "cinematic_warm", "intensity": 0.85})
    af = _AUDIO_PRESETS["clean_speech"]["af"]
    if not face_vf or not color_vf or not af:
        raise RuntimeError("滤镜串为空")

    # 沿用 face_enhance.py/color_grade.py 里那份 CFR+关键帧间隔的教训（Fix
    # C12 等）：re-encode 必须钉死 fps/-g，否则 Remotion 渲染阶段会报
    # "No frame found at position N"。
    fps = 30
    out = workdir / "_op_audio_enhance.mp4"  # 沿用原三步链最后一步的文件名，
    # 下游（simulate_job.py 等）按这个文件名找"增强完成的视频"，合并版本
    # 产出同名文件保持兼容，不需要改动任何调用方。
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-vf", f"{face_vf},{color_vf}",
        "-af", af,
        "-c:v", "libx264", "-crf", "20", "-preset", "fast",
        "-fps_mode", "cfr", "-r", str(fps), "-g", str(fps),
        "-c:a", "aac", "-b:a", "192k",
        str(out),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=_RENDER_TIMEOUT_S)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("合并增强产出文件为空")
    logger.info("  apply_style: face_enhance+color_grade+audio_enhance 合并为一次编码完成")
    return str(out)


def _run_enhancement_chain_sequential(src: str, workdir: Path) -> str:
    """face_enhance -> color_grade -> audio_enhance 原始三步串行版本，
    best-effort（单步失败不影响其它步骤）——合并路径（见上）失败时的退路。

    对应 compose-director.md Step 1（"Attempt every step if the tool is
    available — do not skip steps without a reason"）。三个工具都是纯 FFmpeg
    滤镜链，无需 GPU、无需标准安装之外的依赖。eye_enhance 故意不在这里接入——
    全仓库零测试覆盖，且需要标准安装里没有的 mediapipe/opencv-python 才能做到
    比"全局调亮"更精细的效果，等它被真正跑过一次再考虑接入。
    """
    from tools.audio.audio_enhance import AudioEnhance
    from tools.enhancement.color_grade import ColorGrade
    from tools.enhancement.face_enhance import FaceEnhance

    steps: list[tuple[str, type, dict]] = [
        ("face_enhance", FaceEnhance, {"preset": "talking_head_standard"}),
        ("color_grade", ColorGrade, {"profile": "cinematic_warm", "intensity": 0.85}),
        ("audio_enhance", AudioEnhance, {"preset": "clean_speech"}),
    ]

    for name, tool_cls, extra_inputs in steps:
        out = workdir / f"_op_{name}.mp4"
        inputs = {"input_path": src, "output_path": str(out), **extra_inputs}
        try:
            r = tool_cls().execute(inputs)
        except Exception as e:
            logger.warning(f"  apply_style: {name} 出错，跳过（沿用未增强的视频): {e}")
            continue
        if not r.success:
            logger.warning(f"  apply_style: {name} 失败，跳过（沿用未增强的视频): {r.error}")
            continue
        new_src = r.data.get("output") or (r.artifacts[0] if r.artifacts else None)
        if new_src and Path(new_src).exists():
            logger.info(f"  apply_style: {name} 完成")
            src = new_src
        else:
            logger.warning(f"  apply_style: {name} 未产出文件，跳过")

    return src


# Fix C5（2026-07-16）：跟 content_planner.plan_content 的 criterion loop 用同一个
# 有界重试次数——用户明确要求过循环要"KEEP LOOPING AND EXITING WHEN YOU'VE
# FULFILLED THE CRITERION"，且不是只对某一条视频生效。之前 props_lint 这一层
# 只重试一次，见 _op_apply_style 里那段旧注释。
_PROPS_LINT_MAX_ATTEMPTS = 3

# 架构复审后新增（2026-07-24）：content_planner.plan_content 自己的 3 轮质量
# 判定循环，被 props_lint 的 3 轮重试，又被 vision-QA 触发的重规划各自嵌套调用
# ——最坏情况下一条视频的 apply_style 要打 9+ 次内容规划 LLM 调用。真实事故
# （job_fa4ee47e9676，2026-07-23）：DeepSeek 那天响应慢，这套嵌套加起来拖到了
# 33 分钟。这个预算不改变任何质量判断标准——每一轮该跑的检查一次不少——只是
# 给"还要不要再等一轮 LLM"这件事设一个总时长上限，超了就直接走本来就有的
# best-of 交付（正常轮数用尽时也是同一条路径），不是新的降级逻辑。
_APPLY_STYLE_CONTENT_DEADLINE_S = int(os.getenv("OM_APPLY_STYLE_CONTENT_DEADLINE_S", "720"))

# 参与"丰富度"计分的 props 字段——每一项都是真正的动画/图形，不是纯文字。
_RICHNESS_FIELDS = (
    "dataCards", "gauges", "countdowns", "calendarEvents", "beforeAfter",
    "stepLists", "topicCards", "cornerCards", "quotes",
    "comparisons", "rankedLists", "checklists", "locationPins", "testimonials", "iconClusters",
    "progressBars", "prosCons", "milestoneTracks", "trustBadges", "barCharts", "milestoneUnlocks",
)

# 视觉复审重规划的"内容是否真的变了"对比字段——_RICHNESS_FIELDS 加上
# chapters/sections（vision QA 实际审的是这些字段的渲染结果）。刻意不比较
# scenes/opacityKeyframes 等派生字段：_recompute_scenes_from_content 每次都
# 会重算它们，哪怕内容一字未变也会产生不同的值，拿来比较只会永远判定"变了"。
_CONTENT_COMPARISON_FIELDS = ("chapters", "sections") + _RICHNESS_FIELDS


def _content_unchanged(props_a: dict, props_b: dict) -> bool:
    """视觉复审触发重规划后，新一轮 `_build(feedback=...)` 产出的内容是否跟
    上一轮完全一样（LLM 没有真的按反馈调整任何东西，只是原样吐回来）。

    只在这种"确认没变"的情况下跳过第二轮 qa_stills（还是几帧静态图 + 一次
    视觉模型调用，不是整片重渲染——重渲染在这一步之后才发生、且全程只有
    一次，见 _op_apply_style 末尾）：内容没变，第二轮复审几乎一定会原样
    再报一次同样的 high severity 问题，等于花一次视觉模型调用去确认一个
    已经知道的答案。严格相等比较（不做"差不多算变了"的模糊匹配）——只有
    真正的一字不差才会命中，任何真实调整过的版本都会走回正常的复审路径，
    不会有内容真的变了却被误判"没变"而跳过复审的风险。
    """
    return all(props_a.get(f) == props_b.get(f) for f in _CONTENT_COMPARISON_FIELDS)


def _fill_intro_lead_dead_space(props: dict, findings: list[dict], captions: list[dict], segments: list[dict]) -> dict:
    """Fix C13（2026-07-17，真实生产复现——同一支 backtest 视频连续 3 轮重规划
    都没修掉 intro_lead_dead_space，最终交付版本仍是长达 5.2s 的纯说话人+字幕
    空白，用户直接在渲染出的截图里抓到）。

    这条 finding 的检测（Rule 9/props_lint.py）从没失手过——三次独立 backtest
    都精准报出同一类缺口；但把它当反馈文字喂给 LLM 重规划，三轮下来没有一次
    真正补上过。跟 D3-D6/E2 是同一类教训：LLM 对某条 finding 反复失败，就不该
    继续指望"这次会听话"，该换成确定性保证。这里不追加一次 LLM 调用，直接在
    最终交付的 props 上机械地插入一个轻量 topicCard 盖住缺口——内容取这段时间
    实际讲的字幕原文（掐头去尾，不超过 _MAX_HEADLINE_CHARS），不是编造的品牌语
    ——跟"_fallback_topic_cards_for_gaps 被删掉"不是同一类问题：那个是对*所有*
    稀疏 gap 无差别地机械填充、多次重复才显得像"为了有而有的弹窗"；这里只在
    "LLM 已经真实尝试过 3 轮、专门针对 intro 这一个位置仍然失败"之后才触发一次，
    是保底，不是默认行为。
    """
    from .content_planner import FPS, _CONTENT_ZONE_X, _CONTENT_ZONE_Y
    from .props_lint import lint_props, _transition_windows

    gap = next((f for f in findings if f.get("check") == "intro_lead_dead_space"), None)
    if gap is None:
        return props

    gap_start, gap_end = gap["gap_start"], gap["gap_end"]
    # gap_start 就是 introOutFrame（props_lint.py 里这条 check 的定义）——但卡片
    # 实际收到 workflow 尺寸的时间点不一定等于 introOutFrame+20：Fix C14 把它
    # 的上限设在 introOutFrame+100，真实收缩点可能落在 20-100 之间任何地方。
    # 用真实的 scenes 过渡窗口而不是猜一个固定偏移——固定 +20 在 C14 落地前
    # 曾经把这张卡直接放进了仍在变形的过渡区间，反而多产生一条
    # element_mounts_during_card_transition（验证时抓到的真实回归）。
    mount = gap_start + 20
    for win_start, win_end in _transition_windows(props.get("scenes") or []):
        if win_start < mount < win_end:
            mount = win_end
    # Fix C13b（2026-07-17，真实生产复现——job_b7e1b7f96481，用户 WhatsApp 上
    # 真实收到降级交付后发现）：这里原来是 max(mount+20, gap_end-10)，两个候选
    # 取较大值——但 gap_end 是"第一个真实内容元素挂载的那一帧"，取较大值在
    # mount 本身已经很晚（贴着 gap_end）时会让 end 反而超出 gap_end，把卡片
    # 的尾巴伸进下一个真实元素的地盘，制造一条新的 element_overlap，安全阀
    # 因此拒绝插入——保底本身失效，intro_lead_dead_space 原样交付给用户，
    # 最终触发 vision QA 的"空画布"判定和整段降级。改成夹在 gap_end 这个硬
    # 上限以内：还是尽量给够 20 帧的最小展示时长，但绝不越界侵入下一个元素
    # 的时间窗——真的挤不下（mount 本身已经 >= gap_end）就老实放弃插入，
    # 好过插入一个会引发新重叠的版本。
    end = min(max(mount + 20, gap_end - 10), gap_end)
    if end <= mount:
        return props

    gap_start_ms, gap_end_ms = gap_start / FPS * 1000, gap_end / FPS * 1000
    overlapping = [c for c in captions if c["startMs"] < gap_end_ms and c["endMs"] > gap_start_ms]
    # Fix C25（2026-07-20，真实生产复现——job_452ef6c48100，用户反馈"it's David
    # from... 没用啊"）：这条 gap 紧跟在 intro 后面，overlapping[0] 常常正好是
    # captions[0]——全片第一句话，几乎总是"Hi, it's <name> from <company>"这类
    # 自我介绍/问候。这段身份信息 IntroTitle 卡片在片头已经完整展示过一次
    # （姓名+公司），这里再截一小段同样的话塞进一张卡，既不提供新信息，字数
    # 上限还经常把整句砍在词中间——用户截图看到的就是这种"半句自我介绍+省略号"
    # 的卡片。C10 这条 finding 自己给出的建议原文已经写明这种情况可以不补：
    # "如果这段时间说的是纯问候/自我介绍...没有数字/工具名可以提前挂上去"——
    # 之前的实现没有落实这句话，无条件用 overlapping[0] 的文字兜底。这里改成：
    # 命中"第一句话"这个具体、可判断的情况时，宁可让这段时间保持"只有说话人
    # +字幕"（intro 卡片已经把身份信息交代过了），也不插入一张重复、且大概率
    # 被截断的卡片——跟"_fallback_topic_cards_for_gaps 被删掉"是同一个教训，
    # 这次是把它落实到 C13 自己身上。
    # Fix C43（2026-07-21，真实生产复现——job_452ef6c48100，用户直接抓到"it's
    # david from..."在 Fix C25 上线后又出现了一次）：C25 的判定只在 overlapping
    # 的第一条恰好是 captions[0] 这个具体的 PHRASE-CAPTION 对象时才成立——但
    # phrase-level captions 是把一整句话（比如"Hi there, it's David from
    # Pacific Life, quick reminder"）按词时间戳拆成好几个短字幕块显示，自我
    # 介绍这一整句话本身横跨好几个 phrase-caption。只要这段 gap 的起点落在
    # 这句话中间（intro 收起的时刻很常见地卡在句子说到一半），overlapping[0]
    # 就是这句话后半段的某个 phrase-caption，而不是字面上的 captions[0]——
    # identity 判定因此漏判，即使内容仍然是纯自我介绍。改成检查真正要拿来
    # 当标题来源的 overlapping[0] 是否落在第一个真实语音 segment（一整句话，
    # 不是拆碎的字幕块）的时间范围内——不管它是不是 captions 列表里字面上的
    # 第一个对象，只要内容还没超出开场这句自我介绍，就仍然算重复。
    first_segment_end_ms = (segments[0]["end"] * 1000) if segments else -1
    is_self_intro_repeat = (
        bool(overlapping) and overlapping[0]["endMs"] <= first_segment_end_ms + 200
    )
    _MAX_HEADLINE_CHARS = 16
    if is_self_intro_repeat:
        return props
    elif overlapping:
        text = overlapping[0]["text"].strip()
        headline = text if len(text) <= _MAX_HEADLINE_CHARS else text[:_MAX_HEADLINE_CHARS] + "…"
    else:
        headline = "AI EDIT"  # 这段时间没有任何字幕可用时的最后兜底，不编造具体内容

    filler_card = {
        "headline": headline, "icon": "sparkle",
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": 960,
        "mountFrame": mount, "endFrame": end,
    }
    candidate = dict(props)
    candidate["topicCards"] = [*(props.get("topicCards") or []), filler_card]

    # 保底本身不能制造新问题——插入前后都跑一次 lint。光比总数不够：验证时
    # 真实抓到过一次总数持平(5->5)但内容换了的回归——intro_lead_dead_space
    # 和 low_visual_richness 消失，换成了一条新的 element_over_card 重复项
    # 和一条新的 element_mounts_during_card_transition，总数假装没变化，实际
    # 是拿一个已知问题换了两个新问题。改成比较 check 类型集合：新版本不能
    # 出现插入前完全没有过的 check 类型，哪怕总数打平或更少。
    before_findings = lint_props(props)
    after_findings = lint_props(candidate)
    before_checks = {f["check"] for f in before_findings}
    after_checks = {f["check"] for f in after_findings}
    new_check_types = after_checks - before_checks
    if new_check_types or len(after_findings) > len(before_findings):
        logger.warning(
            f"  apply_style: intro_lead_dead_space 确定性兜底会引入新问题"
            f"({len(before_findings)}->{len(after_findings)} findings, 新增类型: "
            f"{new_check_types or '无，但总数变多'})，放弃插入，保留原版本（Fix C13 安全阀）"
        )
        return props

    logger.info(
        f"  apply_style: intro_lead_dead_space 3 轮重规划仍未解决，确定性兜底插入"
        f"轻量 topicCard('{headline}') 覆盖第 {mount}-{end} 帧（Fix C13）"
    )
    return candidate


def _demote_content_free_takeovers(props: dict, findings: list[dict]) -> dict:
    """Fix C15（2026-07-17，真实生产复现——同一支 dajaai-walking backtest 视频，
    用户截图直接抓到）：'流程/PROCESS' 接管区间(417-657)既没有 timeline 也没有
    icon，SectionLayer 只能画标题+一个纯装饰性的模糊光斑——跟 Rule 4 记录的
    bug 视觉上一模一样，但根因不同：Rule 4 那次是 TimelineSection 被写死只在
    dark 模式渲染，这次是 content_planner 这一轮的方案压根没给这个接管章节挂
    timeline/icon 中的任何一个。

    跟 intro_lead_dead_space（Fix C13）同一类教训：LLM 三轮重规划都没修好，
    该换成确定性保证，不再赌"这次会听话"。这里选 props_lint 自己给的第三个
    选项——"这段内容其实不值得全画布接管，改回普通 workflow 模式"——而不是
    硬造一个 timeline：这个章节自己的 stepList（DIGITAL HUMAN PROCESS，
    mountFrame 367-978）已经完整覆盖了 417-657 这整个接管区间，说话人被藏起来
    换来的只有一个空气泡，内容一点没多。直接去掉这个 section（不再全画布接管）
    + 去掉对应的说话人隐藏关键帧，说话人正常留在画面上，stepList 该怎么显示
    还怎么显示，不需要凭空造内容。

    只删 sections 列表本身不够——真正驱动说话人可见度的是 opacityKeyframes，
    是渲染时读的独立字段，不是从 sections 派生的；只删 sections 会留下"说话人
    仍不可见，但连装饰性光斑都没了"的更差状态（纯空气泡）。两个字段必须一起改。
    """
    bad = [f for f in findings if f.get("check") == "section_takeover_lacks_content"]
    if not bad:
        return props
    bad_spans = [(f["fromFrame"], f["toFrame"]) for f in bad]

    candidate = dict(props)
    candidate["sections"] = [
        s for s in (props.get("sections") or [])
        if (s.get("fromFrame"), s.get("toFrame")) not in bad_spans
    ]
    # 淡出/淡回的关键帧紧贴 fromFrame/toFrame 但不完全等于（_workflow_mode_schedule
    # 的事件扫描 + _TAKEOVER_FADE_FRAMES 淡入淡出会有几帧偏移），用缓冲区间匹配
    # 而不是精确相等。
    _BUFFER = 20
    old_opacity = props.get("opacityKeyframes") or []
    new_opacity = [
        k for k in old_opacity
        if not any(fr - _BUFFER <= k["frame"] <= to + _BUFFER for fr, to in bad_spans)
    ]
    if new_opacity:
        candidate["opacityKeyframes"] = new_opacity
    else:
        candidate.pop("opacityKeyframes", None)

    from .props_lint import lint_props
    before_findings = lint_props(props)
    after_findings = lint_props(candidate)
    before_checks = {f["check"] for f in before_findings}
    after_checks = {f["check"] for f in after_findings}
    new_check_types = after_checks - before_checks
    if new_check_types or len(after_findings) > len(before_findings):
        logger.warning(
            f"  apply_style: section_takeover_lacks_content 确定性兜底会引入新问题"
            f"({len(before_findings)}->{len(after_findings)} findings, 新增类型: "
            f"{new_check_types or '无，但总数变多'})，放弃降级，保留原版本（Fix C15 安全阀）"
        )
        return props

    logger.info(
        f"  apply_style: section_takeover_lacks_content 3 轮重规划仍未解决，"
        f"确定性降级为普通 workflow 模式（去掉 {len(bad_spans)} 个空内容接管，"
        f"说话人保持可见）（Fix C15）"
    )
    return candidate


_FACECAM_RESTORE_BUFFER_FRAMES = 60  # 2s @ 30fps -- enough runway for the speaker to visibly reappear before the video ends


def _restore_facecam_before_end(props: dict, findings: list[dict], duration_frames: int) -> dict:
    """Fix C37（2026-07-21，通过 replica harness 在真实 job 上反复确认——
    job_51f154a80f9b 和 job_73e873e4f7e1 各自的 content_planner 重规划都撞上
    过）：props_lint 的 facecam_never_restored 在最后一个隐藏(接管)区间一路
    延伸到片尾时触发——说话人在视频结束前再也没有恢复可见。跟 C13/C15 同一类
    教训：这条 finding 在多轮真实重规划里反复出现，不该继续赌"这次 LLM 会
    收窄接管范围"。

    修法跟 Rule 4/D2 的"裁不是删"一致——这个接管区间的内容（timeline/icon/
    随便什么）可能完全正当，唯一错的是它跑得太靠近片尾、没给说话人留返场
    的时间。把该 section 的 toFrame 裁到 duration_frames - buffer，而不是
    整段删掉；裁完之后原有的 opacityKeyframes 淡入淡出时机点已经不对（本来
    就没排"恢复可见"这一步，因为接管当时判定跑到片尾），所以跟 Fix C33 一样
    从头对当前（裁剪后）的 sections + 全部内容区图形重新扫一遍 workflow_ranges，
    重算 scenes/opacityKeyframes，而不是手工去猜该在哪一帧插一个淡入关键帧。

    跟 C15 共用同一张安全阀：裁剪后findings 变多或出现新类型就放弃，保留原版本。
    """
    bad = [f for f in findings if f.get("check") == "facecam_never_restored"]
    if not bad:
        return props
    hidden_from = bad[0]["hidden_from_frame"]
    new_end = duration_frames - _FACECAM_RESTORE_BUFFER_FRAMES
    if new_end <= hidden_from:
        return props  # 没有可裁的空间（隐藏区间本身已经短于 buffer），维持原样

    candidate = dict(props)
    candidate_sections = []
    capped_any = False
    for s in (props.get("sections") or []):
        s = dict(s)
        if s.get("fromFrame", 0) <= hidden_from < s.get("toFrame", 0) and s["toFrame"] >= duration_frames - 1:
            s["toFrame"] = new_end
            capped_any = True
        candidate_sections.append(s)
    if not capped_any:
        return props
    candidate["sections"] = candidate_sections
    candidate = _recompute_scenes_from_content(candidate, duration_frames)

    from .props_lint import lint_props
    before_findings = lint_props(props)
    after_findings = lint_props(candidate)
    before_checks = {f["check"] for f in before_findings}
    after_checks = {f["check"] for f in after_findings}
    new_check_types = after_checks - before_checks
    if new_check_types or len(after_findings) > len(before_findings):
        logger.warning(
            f"  apply_style: facecam_never_restored 确定性兜底会引入新问题"
            f"({len(before_findings)}->{len(after_findings)} findings, 新增类型: "
            f"{new_check_types or '无，但总数变多'})，放弃裁剪，保留原版本（Fix C37 安全阀）"
        )
        return props

    logger.info(
        f"  apply_style: facecam_never_restored 3 轮重规划仍未解决，"
        f"确定性裁短接管区间到第 {new_end} 帧（留 {_FACECAM_RESTORE_BUFFER_FRAMES} 帧"
        f"给说话人在片尾前恢复可见）（Fix C37）"
    )
    return candidate


def _visual_richness(props: dict) -> int:
    """Fix C6（2026-07-16）：确认过的真实生产 bug——MrBeast backtest
    (job_95e1e08b0995)第一轮规划出了完整的 TIMELINE 时间线图形 + 数据卡 +
    前后对比；props_lint 发现 2 处 element_overlap 后触发重新规划，新一轮
    plan_content 是完全独立的 LLM 调用（不是在旧方案上打补丁），随机生成出
    一版丢了时间线、丢了数据卡、只剩一张前后对比卡的方案——但这一版恰好
    没有 element_overlap，findings 数量比第一轮少，于是 Fix C5 的 best-of
    比较（只看 len(candidate_findings) < len(best_findings)）就把它当"更好"
    采用了，把真正的动画内容换成了"说话人+字幕"的空壳。用户原话："why did
    you not include animations...it's almost every video"——根因就是这个
    比较完全不看内容丰不丰富，只看有没有几何问题，而一个内容空空如也的
    方案天然不会有任何东西可以重叠。这个函数给 candidate 算一个丰富度分数
    （数出所有真正带图形/动画的 props 字段一共有多少项，process_timeline
    额外算 1 项），下面 C5 的比较逻辑据此拒绝"findings 更少但内容更寡淡"的
    候选版本。"""
    score = sum(len(props.get(f) or []) for f in _RICHNESS_FIELDS)
    score += sum(1 for s in (props.get("sections") or []) if s.get("timeline"))
    return score


# 架构复审后新增（2026-07-27）：真实日志统计过（2026-07-13~27，22 个跑过
# apply_style 的任务），6 个（27%）最终降级；把每一次降级前的视觉复审发现
# 拉出来看，高严重度问题几乎全部是这两类："说话人取景不当/脸部被裁切"、
# "对比度过低"。这两类完全由 speakerObjectPosition/colorMode 决定——两者都
# 在 apply_style 一开始就算好一次（见 _op_apply_style 里 speaker_object_
# position 的赋值），此后不管内容规划重试多少轮都不会再被碰——用"重新规划
# 内容"去回应这两类问题，规划出来的内容因此每次都跟上一轮一字不差（反馈
# 里说的东西它根本无权修改），白白烧光一整轮的时间预算才降级，"慢"和"没
# 套上模板"是同一个根因的两个症状。
#
# 对症的做法：识别出高严重度问题**只**是这两类（没有掺杂真正的内容问题）
# 时，跳过昂贵的内容重规划（零 LLM 调用），直接调整对应参数重渲染一次
# 验证——取景问题把裁剪窗口往上移让出更多头顶空间，对比度问题切换明暗
# 配色。混杂了其它类型问题时仍然走原来的内容重规划路径，不动那条路径的
# 行为。
_FRAMING_ISSUE_KEYWORDS = ("取景", "裁切", "贴边")
_CONTRAST_ISSUE_KEYWORDS = ("对比度",)
_GEOMETRY_COLOR_KEYWORDS = _FRAMING_ISSUE_KEYWORDS + _CONTRAST_ISSUE_KEYWORDS


def _is_geometry_or_color_only(findings: list[dict]) -> bool:
    """高严重度发现是否**全部**属于取景/对比度这类跟内容选择无关的几何或
    配色问题——只要有一条不属于，就说明混杂了真正的内容问题，不适用这条
    对症修复捷径，交回原来的内容重规划路径处理。"""
    if not findings:
        return False
    return all(
        any(kw in f.get("issue", "") for kw in _GEOMETRY_COLOR_KEYWORDS)
        for f in findings
    )


def _correct_geometry_and_color(props: dict, findings: list[dict]) -> dict:
    """直接调整 speakerObjectPosition/colorMode，不调用 LLM——这是真正对症
    的修复，而不是像内容重规划那样反馈了个它管不了的问题。"""
    corrected = dict(props)
    issues_text = " ".join(f.get("issue", "") for f in findings)

    if any(kw in issues_text for kw in _FRAMING_ISSUE_KEYWORDS):
        pos = corrected.get("speakerObjectPosition") or "50% 50%"
        try:
            x_part, y_part = pos.replace("%", "").split()
            # object-position 的 Y 值越小，裁剪窗口越往上（露出更多头顶）；
            # 报告"脸部被裁切"说明当前窗口偏下，把 Y 往下调 15 个百分点、
            # 下限钉在 10%（避免反过来把下巴/胸口裁没）。
            new_y = max(10.0, float(y_part) - 15.0)
            corrected["speakerObjectPosition"] = f"{x_part}% {new_y:.0f}%"
            logger.info(
                f"  apply_style: 取景问题——speakerObjectPosition 从 '{pos}' "
                f"调整为 '{corrected['speakerObjectPosition']}'（露出更多头顶空间）"
            )
        except (ValueError, AttributeError):
            logger.warning(f"  apply_style: speakerObjectPosition '{pos}' 格式无法解析，跳过取景修正")

    if any(kw in issues_text for kw in _CONTRAST_ISSUE_KEYWORDS):
        old_mode = corrected.get("colorMode", "warm")
        corrected["colorMode"] = "dark" if old_mode == "warm" else "warm"
        logger.info(
            f"  apply_style: 对比度问题——colorMode 从 '{old_mode}' "
            f"切换为 '{corrected['colorMode']}'"
        )

    return corrected


# 验证上面这条捷径时发现的第二个、更根本的问题（2026-07-27，直接复现
# job_cb04960d9a48）：真实触发降级的那条 finding（frame_index 0）实测根本
# 不是裁剪窗口问题——它采样到的是 intro 标题卡（IntroTitle.tsx）还没淡出
# 的那一帧（frame 28 < introOutFrame(80) + 12），那段时间整屏盖着深色渐变
# 蒙层 + 大标题文字，说话人的脸本来就该被压暗/半遮挡，是设计如此。实测
# speakerObjectPosition 从 51% 一路调到 25%（Y 方向移动了 26 个百分点）
# 这条 finding 原样复现——不是修正力度不够，是这类 finding 根本不归
# speakerObjectPosition 管，跟 Rule 14 那类"采样帧本来就不该被这条判断
# 标准检查"是同一种 bug，只是这次不是"帧还没渲染任何东西"，是"帧本来就
# 该长这样"。往内容重规划那条路径走一样无解——intro 蒙层是固定的渲染
# 组件逻辑，不受 content_planner 的规划结果影响。
# 对症做法：intro 蒙层仍在生效的窗口内、且 finding 是取景类问题的，直接
# 认定"设计如此"丢弃，不进入任何重试/降级判断——不止在这条捷径分支生效，
# 三处读取 major 的地方都要用同一个函数过滤（Rule 5/13 的教训：一个只在
# 单个调用点生效的保证不是保证）。
#
# 同一晚验证时又实测复现了第二种、第三种同源问题（job_7a33f9a80af8，两轮
# 独立尝试都踩中）：这次 intro 用的是 StatsHookIntro.tsx（"stats_hook"
# 变体，跟 IntroTitle.tsx 是同一批"固定深色开场"组件的另一个），frame 28
# 报"对比度过低"——`_correct_geometry_and_color` 照常把 colorMode 从
# warm 切成 dark 去"修"，但 StatsHookIntro.tsx 第 77 行的背景色是
# `colorMode === "warm" ? "#0D1117" : palette.bgDeep`，而 `palette.bgDeep`
# 在 dark 主题下是 "#090C10"——两个分支都是近乎全黑，colorMode 根本不
# 影响这个组件的背景色，是这两个 intro 组件共同的设计（"dark full-bleed
# opener"，不管全片选的是哪个 colorMode，开场这几十帧本来就该是近黑背景
# 配大字号高对比文字）。切换 colorMode 对这条 finding 完全是无效操作，
# 还会把后面一整条视频的配色也带偏（colorMode 是全局属性，SpeakerCard/
# 数据卡等其它组件都真的会跟着变）。同一次重试里，切换 colorMode 之后
# 复审反而多冒出 4 条取景类 high 发现——大概率是同一个说话人视频取景本来
# 就临界（这一晚另外两个 job 也是这个说话人、同一个 43% 51% 校准值，见
# job_cb04960d9a48/job_5b0ec0b914ee 的调查记录），叠加上视觉模型本身的
# 判断噪音，不是 colorMode 切换真的让画面变差了，但也没有证据证明切换
# colorMode 帮上了忙——两轮独立尝试，"对比度过低"->切换->复审都变得更差，
# 一次巧合可以理解成噪音，两次同源复现更像是这条捷径对这类 finding 从
# 结构上就不该出手。同一帧也报了"画面为黑色，无任何内容"——StatsHookIntro
# 的设计就是"深色满屏 + 居中大字号数字 + 一条细进度条 + 最多两行小标签"，
# 本来就没有大面积"内容"可言（跟 IntroTitle 的深色蒙层是同一类"设计如此"，
# 只是这次视觉模型换了个说法）。
# 三类关键词一起在 intro 蒙层窗口内丢弃，不只丢取景类：取景/对比度/黑屏
# 空画布，在这个窗口内都不是 speakerObjectPosition/colorMode/内容规划
# 能真正修好的问题。
_INTRO_SCRIM_TAIL_FRAMES = 12  # 对应 IntroTitle.tsx/StatsHookIntro.tsx: exit 的 interpolate 终点是 introOutFrame + 12
_INTRO_UNFIXABLE_KEYWORDS = _GEOMETRY_COLOR_KEYWORDS + ("黑色", "纯黑", "无任何内容", "空画布")


def _drop_intro_scrim_unfixable_findings(
    findings: list[dict], stills: list[dict], intro_out_frame: int
) -> list[dict]:
    dropped = []
    kept = []
    for f in findings:
        idx = f.get("frame_index")
        frame_no = stills[idx]["frame"] if isinstance(idx, int) and 0 <= idx < len(stills) else None
        in_scrim = frame_no is not None and frame_no <= intro_out_frame + _INTRO_SCRIM_TAIL_FRAMES
        is_unfixable = any(kw in f.get("issue", "") for kw in _INTRO_UNFIXABLE_KEYWORDS)
        if in_scrim and is_unfixable:
            dropped.append(f)
        else:
            kept.append(f)
    if dropped:
        logger.info(
            f"  apply_style: 忽略 intro 深色开场窗口内的取景/对比度/黑屏类发现（设计如此，非缺陷）: {dropped}"
        )
    return kept


def _apply_geometry_color_shortcut(
    props: dict, props_path: Path, major: list[dict], remotion_dir: Path, workdir: Path, duration: float
) -> tuple[dict, list[dict]]:
    """真正执行取景/对比度捷径修正 + 重新过一遍视觉复审，返回更新后的
    (props, major)。两个调用点共享（第一次视觉复审 AND 内容重规划之后的
    第二次视觉复审）——Rule 5/13 的教训：只在一个调用点生效的修正不是
    真正的修正，内容重规划把"纯黑画面"这类真内容问题修好之后，剩下的
    发现完全可能变成纯取景/对比度类，这时候一样该走这条捷径，不该直接
    降级交付。"""
    from .qa_stills import run_props_qa

    issues = "; ".join(f"still #{f.get('frame_index')}: {f.get('issue', '')}" for f in major)[:500]
    logger.warning(f"  apply_style: 视觉复审发现的问题都是取景/对比度类，跳过内容重规划直接调参重试: {issues}")
    props = _correct_geometry_and_color(props, major)
    props = _recompute_scenes_from_content(props, round(duration * 30))
    props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
    qa_result = run_props_qa(props, props_path, remotion_dir, workdir / "qa_stills")
    major = _major_vision_findings(qa_result, props)
    return props, major


def _major_vision_findings(qa_result: dict, props: dict) -> list[dict]:
    vision_findings = (qa_result.get("vision_review") or {}).get("findings") or []
    major = [f for f in vision_findings if f.get("severity") == "high"]
    return _drop_intro_scrim_unfixable_findings(
        major, qa_result.get("stills") or [], props.get("introOutFrame", 20)
    )


def _op_apply_style(src: str, op: dict, workdir: Path) -> Optional[str]:
    """转写 + 内容规划 + 用 Remotion 渲染 XiaojinEditorial（contract②）。

    对应 VeLL-lab/video-studio 的 tools/directors/compose-director.md（"xiaojin-
    editorial" style）——组件由 P3 移植/维护在 remotion-composer/src/XiaojinEditorial.tsx
    + components/xiaojin/*，本函数只负责按 contracts/render_props.schema.json
    构建 props 并调用 P3 的稳定渲染入口：
    `npx remotion render XiaojinEditorial --props=<json>`。

    章节/数据卡/仪表盘/倒计时/日历默认都走 content_planner 的完整 Data Display
    Analysis（按 compose-director.md 的表格把每个数据点分到该用的图形，不再只有
    count-up 一种）；调用方也可以显式传 op["chapters"] / op["data_cards"] /
    op["gauges"] / op["countdowns"] / op["calendar_events"] / op["mode_schedule"]
    覆盖（例如手工编排的演示）。

    QR + 联系方式（props["qrContact"]）不经过 content_planner 的语义判断——
    是否显示 QR 完全取决于调用方是否在 op["qr_contact"] 里给了真实联系方式，
    绝不凭空编造一个。
    """
    from .content_planner import plan_content

    config = get_config()

    # Enhancement chain (compose-director.md Step 1: "attempt every step if the
    # tool is available — do not skip steps without a reason"). Order matches
    # the doc exactly: face -> eye -> color -> audio, then everything else
    # (transcription/captions/render) runs on the enhanced video. eye_enhance
    # is deliberately excluded here — unlike the other three, it has zero test
    # coverage anywhere in this codebase and needs mediapipe/opencv-python
    # (not part of the standard install) to do anything beyond a crude global
    # brightness fallback; revisit once it's actually been exercised once.
    # Each step is best-effort: if a tool errors unexpectedly, log and keep
    # going with the pre-that-step video rather than failing the whole edit —
    # matching the "attempt, don't hard-fail" philosophy already used
    # throughout this file for optional refinement steps.
    src = _run_enhancement_chain(src, workdir)
    src_path = Path(src)

    duration = _probe_duration(src_path)

    t = _safe_transcribe(src, workdir, config.faster_whisper_model)
    if t is None:
        # 转写失败（工具报告失败或底层库抛异常）不该搞垮整条 apply_style——
        # 降级为"有卡片+章节条+品牌条+进度条，但无字幕/图形"的版本，好过全失败。
        logger.warning("  apply_style: 转写不可用（降级为无字幕/无图形版本）")
        segments: list[dict] = []
        captions: list[dict] = []
        word_timestamps: list[dict] = []
    else:
        segments = t.data.get("segments") or []
        word_timestamps = t.data.get("word_timestamps") or []
        # 短语级字幕（词级时间戳重组），不是 5-6 行的 segment 大段
        captions = build_caption_phrases(word_timestamps, segments)

        # Fix A4：对"剪完之后真正会播出的内容"做最后一道确定性检查——转写的
        # 是已经剪过口误的视频，这里的 word_timestamps 就是最终播出文本。
        # 不依赖 LLM，纯规则扫一遍重复短语；抓的是"remove_filler 那一步的 LLM
        # 判断+复核+确定性兜底全都没拦住"这种极端情况（理论上不该发生，但这是
        # 最后一次还能在渲染前发现的机会）。只打日志，不阻断渲染。
        from .content_planner import _cut_duplicate_phrases
        leftover_dupes = _cut_duplicate_phrases(word_timestamps, set())
        if leftover_dupes:
            dupe_words = " ".join(word_timestamps[i]["word"] for i in sorted(leftover_dupes))
            logger.warning(
                f"  apply_style: 最终播出内容里检测到疑似遗留重复短语（remove_filler 应该已经剪掉但没有）: "
                f"{dupe_words}"
            )

    remotion_dir = Path(config.openmontage_root) / "remotion-composer"
    job_slug = workdir.name
    # 素材不再拷进 remotion public/：预打包 bundle 是 public/ 的打包时快照，
    # 打包后 staged 的素材看不见（整类 404 事故的根源，含 7-10 那次）。改走
    # 本机 API 的 /files 路由（SpeakerCard 对 http 开头的 src 直接透传），
    # bundle 从此纯只读共享——没有素材同步问题，也没有 public/jobs 无限膨胀。
    video_src_url = f"{config.local_api_base}/files/{job_slug}/{Path(src).name}"

    # 人脸裁剪校准是确定性的（同一段视频每次算出来的结果一样），跟内容规划反馈
    # 无关，只需要在下面的重试闭包外面算一次——重试它只会得到一模一样的值。
    speaker_object_position = op.get("speaker_object_position") or calibrate_speaker_object_position(src, workdir)

    # presenter：insert_broll 以 presenter 模式跑过时留下的人物视频+时间窗，
    # 渲染成模板下方的人物小窗（卡片此时只放 b-roll，人脸不再烧进画面被裁）。
    presenter_prop = None
    _presenter_meta = workdir / "_presenter.json"
    if _presenter_meta.exists():
        try:
            _pm = json.loads(_presenter_meta.read_text(encoding="utf-8"))
            _psrc = _pm.get("person_src")
            _pwins = [
                {"fromFrame": max(0, round(float(w["start"]) * 30)),
                 "toFrame": max(0, round(float(w["end"]) * 30))}
                for w in (_pm.get("windows") or [])
                if w.get("start") is not None and w.get("end") is not None
            ]
            if _psrc and _pwins:
                presenter_prop = {
                    "src": f"{config.local_api_base}/files/{job_slug}/{_psrc}",
                    "windows": _pwins,
                    "x": 604, "y": 1270, "w": 400, "h": 440, "radius": 28,
                    "objectPosition": speaker_object_position,
                }
        except Exception as _e:
            logger.warning(f"  apply_style: presenter 元数据解析失败，跳过下方人物小窗: {_e}")

    props_path = workdir / "_op_apply_style_props.json"

    # 从内容规划真正开始算起（不算前面 enhancement chain/转写的时间——那些
    # 各自已经有自己的超时/信号量保护），到 props_lint 循环 + vision-QA 触发
    # 的重规划全部结束为止的总预算。见 _APPLY_STYLE_CONTENT_DEADLINE_S 的
    # 说明。
    #
    # 真实事故（2026-07-27，job_cb04960d9a48）：run_talking_head_pipeline 主
    # 循环里 apply_style 失败会自动整体重试一次（_DEGRADABLE_OPS 的通用逻辑，
    # 见文件顶部）——但这个预算原本每次调用 _op_apply_style 都重新算一次
    # deadline，导致第一次尝试吃满 12 分钟预算触发降级、外层重试后第二次
    # 尝试又重新吃满 12 分钟，从确认到交付实测花了 30 分 47 秒，是预算本身的
    # 2 倍还多。用 workdir 里的一个标记文件让两次调用共享同一个总预算——
    # 第二次调用读到第一次算好的截止时间，不会重新给满整段预算。用挂钟时间
    # （不是 time.monotonic()）存盘，两次调用之间即使隔着进程重启也不会失真。
    _deadline_marker = workdir / "_apply_style_deadline.txt"
    try:
        content_deadline_wall = float(_deadline_marker.read_text().strip())
    except (OSError, ValueError):
        content_deadline_wall = time.time() + _APPLY_STYLE_CONTENT_DEADLINE_S
        _deadline_marker.write_text(str(content_deadline_wall))
    # 内部判断继续用 monotonic 语义（跟 time.time() 的差值在同一次调用里是
    # 稳定的，不受挂钟被外部改动影响）；两个时间基准这里只做一次换算。
    content_deadline = time.monotonic() + (content_deadline_wall - time.time())

    def _build(feedback: Optional[str] = None) -> dict[str, Any]:
        """内容规划 + 组 contract② props。包成闭包是为了让视觉复核重试只重新
        走这一步（一次 LLM 调用 + 一轮 QA stills），不用重新跑 enhancement
        chain / 转写 / 完整 Remotion 渲染——那些跟"这次数据点怎么摆"无关，
        重来一遍纯浪费（渲染整片是整条管线里最贵、也没有 subprocess 超时
        保护的一步）。
        """
        if op.get("chapters") or op.get("data_cards"):
            chapters = op.get("chapters") or []
            data_cards = op.get("data_cards") or []
            gauges = op.get("gauges") or []
            countdowns = op.get("countdowns") or []
            calendar_events = op.get("calendar_events") or []
            before_after = op.get("before_after") or []
            mode_schedule = op.get("mode_schedule") or [{"frame": 0, "mode": "dominant"}]
            plan_intro = None
            plan_outro = None
            plan_sections = op.get("sections") or []
            plan_quotes = op.get("quotes") or []
            plan_contact_cue = op.get("contact_cue")
            plan_pills = op.get("pills") or []
            plan_zone_headers = op.get("zone_headers") or []
            plan_step_lists = op.get("step_lists") or []
            plan_topic_cards = op.get("topic_cards") or []
            plan_corner_cards = op.get("corner_cards") or []
            plan_comparisons = op.get("comparisons") or []
            plan_ranked_lists = op.get("ranked_lists") or []
            plan_checklists = op.get("checklists") or []
            plan_location_pins = op.get("location_pins") or []
            plan_testimonials = op.get("testimonials") or []
            plan_icon_clusters = op.get("icon_clusters") or []
            plan_progress_bars = op.get("progress_bars") or []
            plan_pros_cons = op.get("pros_cons") or []
            plan_milestone_tracks = op.get("milestone_tracks") or []
            plan_trust_badges = op.get("trust_badges") or []
            plan_bar_charts = op.get("bar_charts") or []
            plan_milestone_unlocks = op.get("milestone_unlocks") or []
        else:
            logger.info("  apply_style: 内容规划中（章节 + 数据展示分析）...")
            content_plan = plan_content(segments, duration, feedback=feedback, word_timestamps=word_timestamps,
                                         deadline=content_deadline)
            chapters = content_plan["chapters"]
            data_cards = content_plan["data_cards"]
            gauges = content_plan["gauges"]
            countdowns = content_plan["countdowns"]
            calendar_events = content_plan["calendar_events"]
            before_after = content_plan.get("before_after") or []
            mode_schedule = content_plan["mode_schedule"]
            plan_intro = content_plan.get("intro")
            plan_outro = content_plan.get("outro")
            plan_sections = content_plan.get("sections") or []
            plan_quotes = content_plan.get("quotes") or []
            plan_contact_cue = content_plan.get("contact_cue")
            plan_pills = content_plan.get("pills") or []
            plan_zone_headers = content_plan.get("zone_headers") or []
            plan_step_lists = content_plan.get("step_lists") or []
            plan_topic_cards = content_plan.get("topic_cards") or []
            plan_corner_cards = content_plan.get("corner_cards") or []
            plan_comparisons = content_plan.get("comparisons") or []
            plan_ranked_lists = content_plan.get("ranked_lists") or []
            plan_checklists = content_plan.get("checklists") or []
            plan_location_pins = content_plan.get("location_pins") or []
            plan_testimonials = content_plan.get("testimonials") or []
            plan_icon_clusters = content_plan.get("icon_clusters") or []
            plan_progress_bars = content_plan.get("progress_bars") or []
            plan_pros_cons = content_plan.get("pros_cons") or []
            plan_milestone_tracks = content_plan.get("milestone_tracks") or []
            plan_trust_badges = content_plan.get("trust_badges") or []
            plan_bar_charts = content_plan.get("bar_charts") or []
            plan_milestone_unlocks = content_plan.get("milestone_unlocks") or []
            logger.info(
                f"  apply_style: 规划出 {len(chapters)} 个章节、{len(data_cards)} 个数据卡、"
                f"{len(gauges)} 个仪表盘、{len(countdowns)} 个倒计时、{len(calendar_events)} 个日历、"
                f"{len(before_after)} 个前后对比、{len(plan_quotes)} 条金句"
            )

        scenes, speaker_opacity = _mode_schedule_to_scenes(mode_schedule)

        props: dict[str, Any] = {
            "videoSrc": video_src_url,
            "durationSeconds": duration,
            "colorMode": op.get("colorMode", "warm"),
            "speakerObjectPosition": speaker_object_position,
            "scenes": scenes,
            "introOutFrame": 20,
            "chapters": chapters,
            "captions": captions,
        }
        if speaker_opacity:
            props["opacityKeyframes"] = speaker_opacity
        if presenter_prop:
            props["presenter"] = presenter_prop
        if plan_sections:
            props["sections"] = plan_sections
        if plan_quotes:
            props["quotes"] = plan_quotes
        if plan_pills:
            props["pills"] = plan_pills
        if plan_zone_headers:
            props["zoneHeaders"] = plan_zone_headers
        if plan_step_lists:
            props["stepLists"] = plan_step_lists
        if plan_topic_cards:
            props["topicCards"] = plan_topic_cards
        if plan_corner_cards:
            props["cornerCards"] = plan_corner_cards
        if plan_comparisons:
            props["comparisons"] = plan_comparisons
        if plan_ranked_lists:
            props["rankedLists"] = plan_ranked_lists
        if plan_checklists:
            props["checklists"] = plan_checklists
        if plan_location_pins:
            props["locationPins"] = plan_location_pins
        if plan_testimonials:
            props["testimonials"] = plan_testimonials
        if plan_icon_clusters:
            props["iconClusters"] = plan_icon_clusters
        if plan_progress_bars:
            props["progressBars"] = plan_progress_bars
        if plan_pros_cons:
            props["prosCons"] = plan_pros_cons
        if plan_milestone_tracks:
            props["milestoneTracks"] = plan_milestone_tracks
        if plan_trust_badges:
            props["trustBadges"] = plan_trust_badges
        if plan_bar_charts:
            props["barCharts"] = plan_bar_charts
        if plan_milestone_unlocks:
            props["milestoneUnlocks"] = plan_milestone_unlocks

        # 开场标题卡/片尾 CTA：模板一直支持（IntroTitle/OutroSection），此前管线从不
        # 生成——这是与 video-studio 手工参考成片(VeLL)最大的一块可自动化差距。
        # op 显式传入优先；否则用 content_planner 从转写里写的文案。
        intro = op.get("intro") or plan_intro
        if intro:
            props["intro"] = intro
            # codex：intro 约占开场 ~3s，期间隐藏 chrome/字幕
            intro_out = int(op.get("introOutFrame", 80))
            props["introOutFrame"] = intro_out
            # intro 期间卡片必须保持 Dominant(近全屏)——标题是压在大卡上的
            # (VeLL 参考)。把 introOutFrame 之前开始的 workflow 段推迟到 intro
            # 结束后 20 帧，避免标题叠在停靠小卡+背景上。
            #
            # Fix C14（2026-07-17，真实生产复现，dajaai-walking backtest）：上面
            # 这条只管"太早"（workflow 提前到 intro 还没播完就开始）——完全没管
            # "太晚"的反方向。第一段 workflow 的起始帧来自 _flush_stack 里的
            # stack_start，直接等于第一个内容点自己的 mountFrame（content_planner.py
            # 行 997），跟 introOutFrame 毫无关系。真实撞上的案例：第一个内容点
            # 直到第 236 帧才 mount，intro 在第 80 帧就已经结束，卡片就这么继续
            # 保持全尺寸又空占了 150 帧(5s)——props_lint 的 intro_lead_dead_space
            # 抓到的正是这段。这里补对称的上限：第一段 workflow 最迟从
            # intro_out + _MAX_DOMINANT_HOLD_FRAMES 开始，卡片按时收起，即使这时候
            # 还没有真实内容能填满收起后的位置也一样——腾出来的空当交给 props_lint
            # 的 intro_lead_dead_space + pipeline_runner._fill_intro_lead_dead_space
            # （Fix C13）兜底填一张轻量卡，好过卡片顶着全尺寸空转。只夹住*第一段*
            # workflow（第一个 mode=="workflow" 的项）——后面的 workflow/dominant
            # 交替是内容本身决定的真实时间点，不该跟着挪。
            _MAX_DOMINANT_HOLD_FRAMES = 100  # intro 结束后最多再保持满打满算 ~3.3s 全尺寸
            first_workflow_seen = False
            clamped = []
            for m in mode_schedule:
                m = dict(m)
                if m.get("mode") == "workflow" and m["frame"] < intro_out + 20:
                    m["frame"] = intro_out + 20
                elif (not first_workflow_seen and m.get("mode") == "workflow"
                        and m["frame"] > intro_out + _MAX_DOMINANT_HOLD_FRAMES):
                    m["frame"] = intro_out + _MAX_DOMINANT_HOLD_FRAMES
                if m.get("mode") == "workflow":
                    first_workflow_seen = True
                clamped.append(m)
            # 保持严格递增（推迟后可能与后续项撞帧）
            mode_schedule = []
            for m in sorted(clamped, key=lambda x: x["frame"]):
                if mode_schedule and m["frame"] <= mode_schedule[-1]["frame"]:
                    continue
                mode_schedule.append(m)
            props["scenes"], speaker_opacity = _mode_schedule_to_scenes(mode_schedule)
            if speaker_opacity:
                props["opacityKeyframes"] = speaker_opacity
            else:
                props.pop("opacityKeyframes", None)

            # Fix C2：intro 期间卡片保持 Dominant，上面只推迟了 mode_schedule
            # 本身（决定卡片什么时候开始收缩），但没动各个图形自己的
            # mountFrame——确认过的真实 bug：countdown 在 intro 卡片还没收起
            # (仍是 Dominant 大卡)的时候就已经 mountFrame=91 上场了，图形直接
            # 画在还没让开位置的大卡上面。图形要等卡片真正收缩完成（mode_
            # schedule 的 workflow 转场在 intro_out+20 触发，SpeakerCard.tsx
            # 自己的 TRANSITION_FRAMES=20 决定转场再花 20 帧完成）才能上场。
            # Operate on the LOCAL variables directly, not props[...] — several
            # of these (dataCards/gauges/countdowns/calendarEvents/beforeAfter)
            # aren't assigned into props until further below, so reading them
            # back via props.get(...) here would silently no-op.
            _mount_floor = intro_out + 20 + 20  # intro_out+20(clamp) + TRANSITION_FRAMES(20)
            # plan_corner_cards 加进这个列表是架构复审后新增（2026-07-28，真实
            # 复现 job_5b0ec0b914ee）：这条地板线本来就覆盖了几乎所有图形类型，
            # 唯独漏了 corner_cards——CornerCard.tsx 自己的文档明确写了"绝不在
            # section takeover/quote 期间渲染，卡片被隐藏时渲染了也看不见"，但
            # 那条保护只覆盖 content_planner 认识的 sections/quote takeover，intro
            # 是下游 pipeline_runner 才算出来的独立窗口，content_planner 规划
            # corner_card 时根本不知道它的存在。真实复现：mountFrame=0 的聊天
            # 气泡卡片跟 intro 深色开场大标题同时出现在画面上，视觉复审判定其中
            # 文字"被截断"（实际是卡片被挤到画布边缘、跟开场标题抢位置），
            # 是这个 job 最终降级交付的直接原因。corner_card 渲染在 SpeakerCard
            # 内部、不参与内容区堆叠，机制上跟这条地板线已覆盖的其它图形类型
            # 完全一样，直接并入同一条地板线，不用另写一套逻辑。
            for _items in (data_cards, gauges, countdowns, calendar_events,
                           before_after, plan_quotes, plan_pills, plan_step_lists, plan_topic_cards,
                           plan_comparisons, plan_ranked_lists, plan_checklists,
                           plan_location_pins, plan_testimonials, plan_icon_clusters,
                           plan_progress_bars, plan_pros_cons, plan_milestone_tracks,
                           plan_trust_badges, plan_bar_charts, plan_milestone_unlocks,
                           plan_corner_cards):
                _floor_shift_graphics(_items, _mount_floor)
            _floor_shift_zone_headers(plan_zone_headers, _mount_floor)

            # Fix C24: 片头这一次 dominant 窗口处理完了，但视频中段还会按内容
            # 反复回到 Dominant——同一类避让要对每一次窗口都做，不只是片头。
            for _items in (data_cards, gauges, countdowns, calendar_events,
                           before_after, plan_quotes, plan_pills, plan_step_lists, plan_topic_cards,
                           plan_comparisons, plan_ranked_lists, plan_checklists,
                           plan_location_pins, plan_testimonials, plan_icon_clusters,
                           plan_progress_bars, plan_pros_cons, plan_milestone_tracks,
                           plan_trust_badges, plan_bar_charts, plan_milestone_unlocks):
                _shift_off_dominant_windows(_items, mode_schedule)
            _shift_off_dominant_windows_headers(plan_zone_headers, mode_schedule)

            # 段落接管同样不得在 intro 期间开始
            if props.get("sections"):
                adjusted = []
                for sec in props["sections"]:
                    sec = dict(sec)
                    if sec["fromFrame"] < intro_out + 20:
                        sec["fromFrame"] = intro_out + 20
                    if sec["toFrame"] - sec["fromFrame"] >= 40:
                        adjusted.append(sec)
                props["sections"] = adjusted

        # QuoteCard 是唯一"solo"(占满整个画布，把说话人完全盖住)的图形类型
        # ——确认过的真实用户反馈：LLM 把开场问候语（"Hi there, it's David
        # from Pacific Life."）当成 quote 素材，导致刚看完片头(甚至没有片头
        # 时从第 0 帧起)就立刻被一张文字卡盖住脸，说话人露脸的第一个真正
        # 时刻反而被挡掉了。这条地板线不依赖 intro 是否存在（上面那个 C2
        # 区块整个包在 `if intro:` 里，没有片头标题卡时完全不会跑，之前这
        # 类视频完全没有保护）——任何 quote 都不能在视频最开始这段"先让观众
        # 看到人、听到人说话"的缓冲期内上场。
        # _floor_shift_graphics only ever pushes an item LATER (no-op if it's
        # already past the floor), so this is safe to apply unconditionally
        # on top of whatever the `if intro:` block above already did.
        _floor_shift_graphics(plan_quotes, _QUOTE_MIN_START_FRAMES)

        outro = op.get("outro") or plan_outro
        if outro:
            duration_frames = max(1, round(duration * 30))
            # 片尾最后 ~5s 交给 outro（不足 12s 的视频不上 outro，避免喧宾夺主）。
            # fromFrame 必须排在最后一个内容图形结束之后——OutroSection 画的是
            # 不透明整幅背景，固定 duration-150 的旧算法在短片上会直接把片尾
            # 附近的数据图形整个盖掉（确认过的真实 bug：一条 22.8s 的片子里
            # $100K→$1.5M 的 before/after 预算揭晓排在 550-725 帧，outro 却在
            # 535 帧就把画布糊上了——全片最有料的一个图形完全没露过面）。
            # 内容排到片尾没剩多少空间时，宁可整个跳过 outro，也不盖内容。
            if duration_frames >= 360:
                last_content_end = 0
                for group in (data_cards, gauges, countdowns, calendar_events,
                              before_after, plan_quotes, plan_pills,
                              plan_step_lists, plan_topic_cards,
                              plan_comparisons, plan_ranked_lists, plan_checklists,
                              plan_location_pins, plan_testimonials, plan_icon_clusters,
                              plan_progress_bars, plan_pros_cons, plan_milestone_tracks,
                              plan_trust_badges, plan_bar_charts, plan_milestone_unlocks):
                    for g in group or []:
                        end = min(int(g.get("endFrame", 0) or 0), duration_frames)
                        last_content_end = max(last_content_end, end)
                outro = dict(outro)
                outro.setdefault("fromFrame", max(duration_frames - 150, last_content_end + 10))
                if duration_frames - outro["fromFrame"] >= 60:
                    props["outro"] = outro
                else:
                    logger.info("  apply_style: 片尾内容排满，跳过 outro（不盖住收尾图形）")
        if data_cards:
            props["dataCards"] = data_cards
        if gauges:
            props["gauges"] = gauges
        if countdowns:
            props["countdowns"] = countdowns
        if calendar_events:
            props["calendarEvents"] = calendar_events
        if before_after:
            props["beforeAfter"] = before_after
        qr_input = op.get("qr_contact") or {}
        if qr_input.get("contact_url"):
            from .qr_gen import generate_qr
            # 同 videoSrc：生成进任务目录、走 /files 伺服，不进 remotion public/
            qr_abs = workdir / "qr.png"
            if generate_qr(qr_input["contact_url"], qr_abs):
                # Priority 1 (root fix): content_planner detected the actual
                # moment the speaker says "WhatsApp me"/"scan the QR code"/etc
                # (contact_cue) — mount the card exactly then, in the normal
                # full-width content zone, same as every other data-display
                # card. Confirmed real user complaint: the card previously
                # only ever appeared near a generic end-of-video offset,
                # completely disconnected from when the video actually talks
                # about how to reach the speaker.
                #
                # Priority 2 (fallback, no contact_cue detected — e.g. the
                # video never explicitly narrates a contact moment): anchor
                # to the outro instead of an independent duration-based
                # offset — the two used to be timed off separate constants
                # (outro: duration-150, qrContact: duration-200), so the QR
                # card would pop in ~1.7s BEFORE the outro it's meant to
                # accompany, at its default y=780 which sits inside outro's
                # own headline/CTA column. OutroSection's own content ends by
                # local~52f (its footer reveal) and reserves y=88-1848 for
                # itself, with its last element (footer) at y=1260 —
                # mounting at outro.fromFrame+60 and y=1360 lands it just
                # after outro's entrance finishes, below the footer.
                #
                # Priority 3 (last resort, no outro either): the original
                # duration-based heuristic.
                if plan_contact_cue and plan_contact_cue.get("mountFrame") is not None:
                    default_mount = plan_contact_cue["mountFrame"]
                    # y comes from the planner's stacking lane assignment —
                    # the QR card may be stacked under another visual.
                    default_x, default_y, default_w = (
                        _CONTENT_ZONE_X, plan_contact_cue.get("y", _CONTENT_ZONE_Y), _CONTENT_ZONE_WIDTH)
                else:
                    outro_block = props.get("outro")
                    if outro_block and outro_block.get("fromFrame") is not None:
                        default_mount = outro_block["fromFrame"] + 60
                        default_x, default_y, default_w = 80, 1360, 920
                    else:
                        default_mount = max(0, round(duration * 30) - 200)
                        default_x, default_y, default_w = 80, 780, 920
                qr_contact: dict[str, Any] = {
                    "qrSrc": f"{config.local_api_base}/files/{job_slug}/qr.png",
                    "contactName": qr_input.get("contact_name", ""),
                    "ctaLabel": qr_input.get("cta_label", "WhatsApp Now"),
                    "mountFrame": qr_input.get("mount_frame", default_mount),
                    "x": qr_input.get("x", default_x),
                    "y": qr_input.get("y", default_y),
                    "width": qr_input.get("width", default_w),
                }
                if qr_input.get("contact_company"):
                    qr_contact["contactCompany"] = qr_input["contact_company"]
                props["qrContact"] = qr_contact
        if op.get("brand"):
            props["brand"] = op["brand"]
        elif op.get("compliance"):
            props["compliance"] = op["compliance"]

        props = _recompute_scenes_from_content(props, round(duration * 30))
        props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
        return props

    props = _build()

    # Fix C4：确定性的 props 层面几何×时间重叠检查（whatsapp_mvp/props_lint.py）
    # ——不需要真的渲染/看 stills，直接从最终 props 的数字算出一整类真实发生
    # 过的视觉 bug（header 画在还没收起的卡片上、说话人被隐藏太久且没恢复、
    # outro 落在隐藏区间里等，见 job_e44166eb8c38）。跑在 QA stills 之前，因为
    # 这一步几乎不花钱（纯 Python 计算），能在浪费一次渲染/视觉复核之前先把
    # 明显的问题喂回内容规划重试。真正从根源上消除这几类 bug 的是 Fix C1/C2
    # （header 窗口跟随内容、图形挂载时间不早于卡片收起完成）和 content_planner
    # 的 Fix D1/D2/D3（接管时长上限、隐藏时长预算、片尾前强制恢复说话人）——
    # 这一层是诊断/安全网，不做额外的几何硬裁剪，只负责发现问题并把内容规划
    # 逼着重试。
    #
    # Fix C5（2026-07-16）：这里原本只重试一次，重试后不管有没有更好都直接
    # 拿第二次的结果去交付，即使它比第一次还差。跟 content_planner.plan_content
    # 的 criterion loop 统一成同一套架构（用户明确要求过——"KEEP LOOPING AND
    # EXITING WHEN YOU'VE FULFILLED THE CRITERION"，且要对所有视频生效，不是
    # 只在出问题的那一条上补丁）：有界循环，findings 清空就提前退出；轮数
    # 用尽后交付 findings 最少的一版（best-of），而不是无条件用最后一轮。
    from .props_lint import lint_props

    def _run_props_lint(p: dict) -> list[dict]:
        return lint_props(p)

    best_props, best_findings = props, _run_props_lint(props)
    best_richness = _visual_richness(props)
    attempt = 1
    while best_findings and attempt <= _PROPS_LINT_MAX_ATTEMPTS:
        if time.monotonic() >= content_deadline:
            logger.warning(
                f"  apply_style: 内容规划总预算已用完，跳过 props_lint 第 "
                f"{attempt}/{_PROPS_LINT_MAX_ATTEMPTS} 轮重规划，交付目前最好的一版"
            )
            break
        logger.warning(
            f"  apply_style: props_lint 第 {attempt}/{_PROPS_LINT_MAX_ATTEMPTS} 轮发现 "
            f"{len(best_findings)} 处问题，重新规划: {[f['check'] for f in best_findings]}"
        )
        lint_feedback = "; ".join(f["detail"] for f in best_findings)[:600]
        candidate = _build(feedback=lint_feedback)
        candidate_findings = _run_props_lint(candidate)
        candidate_richness = _visual_richness(candidate)
        # Fix C6：findings 更少不够——还要求丰富度没有下降，否则一个几乎
        # 没有图形内容的空壳方案会因为"天然没什么可以重叠"而赢得比较，把
        # 真正的动画内容换掉（见上面 _visual_richness 的完整案例）。两个条件
        # 都满足才采用这一轮；丰富度下降就算 findings 更少也不换。
        if len(candidate_findings) < len(best_findings) and candidate_richness >= best_richness:
            best_props, best_findings, best_richness = candidate, candidate_findings, candidate_richness
            attempt += 1
            continue
        # 早退（保质量提速）：本轮没有产出"更少问题且不降丰富度"的改进。喂回的
        # lint_feedback 只由 best_findings 决定，而 best 这轮没变——再跑同样的重
        # 规划只会得到同样结果，后续轮次是确定性空转（low_visual_richness/
        # element_over_card 这类"内容本身改不动"的 finding 会一路耗满 _PROPS_LINT_
        # MAX_ATTEMPTS 轮，每轮一次完整 LLM 规划）。提前结束：best_props 已保留，
        # 交付版本与跑满全部轮次完全一致，只省掉注定白烧的后续 LLM 调用。通用于
        # 任何"重规划改不动"的 finding，不特判某一类；能持续改进时仍会继续（上面
        # accept 分支 continue）。
        if len(candidate_findings) < len(best_findings):
            logger.warning(
                f"  apply_style: props_lint 第 {attempt}/{_PROPS_LINT_MAX_ATTEMPTS} 轮的重规划"
                f"findings 更少({len(candidate_findings)} < {len(best_findings)})，但丰富度从 "
                f"{best_richness} 降到 {candidate_richness}——拒绝采用，保留内容更丰富的版本"
            )
        else:
            logger.info(
                f"  apply_style: props_lint 第 {attempt} 轮重规划未改进"
                f"（{[f['check'] for f in best_findings]} 修不动）——提前结束重试，交付当前最佳版本"
            )
        break
    if best_findings:
        logger.warning(
            f"  apply_style: props_lint {_PROPS_LINT_MAX_ATTEMPTS} 轮后仍有 "
            f"{len(best_findings)} 处问题，交付问题最少的一版（不阻断渲染）: "
            f"{[f['check'] for f in best_findings]}"
        )
    elif attempt > 1:
        logger.info(f"  apply_style: props_lint 全部通过（第 {attempt - 1}/{_PROPS_LINT_MAX_ATTEMPTS} 轮重试后）")
    def _apply_deterministic_guarantees(p: dict) -> dict:
        """Fix C16（2026-07-17，真实生产复现——同一支 backtest 视频，用户截图
        直接抓到的"截图4"問題重规划 3 轮后仍在，一路查下去发现 C13/C15 从没被
        真正应用过）：这两个确定性保底只挂在 props_lint 重试循环*后面*一次，
        但 qa_stills 视觉复审如果抓到 high 严重度问题，下面会整段调用
        `props = _build(feedback=...)` 重新规划——这是全新一次内容规划，产出
        的新 props 从没经过 props_lint 循环、更没经过 C13/C15，直接原样送去
        渲染。C13/C15 的保底逻辑因此形同虚设：只要视觉复审恰好在第一轮就抓到
        问题（这条 backtest 真实发生的情况——C13 自己的安全阀又刚好拒绝了
        插入，intro_lead_dead_space 缺口原样留着，被 vision QA 判成"空画布"
        high severity），保底代码从头到尾没有执行的机会。抽成一个函数，在
        主循环后、以及 qa_stills 触发的每一次重规划后都调用，不管 props 是
        从哪条路径产出的，最终送去渲染的版本都保证经过同一套确定性检查。

        Fix C38（2026-07-21，通过 replica harness 在 job_452ef6c48100 上反复
        复现）：C13 插入的新 topicCard（或 C37 裁剪后的 sections）改的是内容区
        图形本身，但这几个保底函数都不会重算 scenes——`_build()` 收尾时已经
        调用过一次 `_recompute_scenes_from_content`（Fix C33），可这里对 p 的
        任何修改都发生在那次 recompute *之后*，冻结的 scenes 又变成了旧的
        真相。不管上面 3 个保底最终谁改了、改没改，返回前统一再重算一次——
        便宜（纯 Python，不用重新渲染），也是唯一能保证"最终送渲染的 scenes
        跟这里最终的内容区图形互相对得上"的办法。
        """
        findings = _run_props_lint(p)
        if findings:
            p = _fill_intro_lead_dead_space(p, findings, captions, segments)
            findings = _run_props_lint(p)
        if findings:
            p = _demote_content_free_takeovers(p, findings)
            findings = _run_props_lint(p)
        if findings:
            p = _restore_facecam_before_end(p, findings, round(duration * 30))
            findings = _run_props_lint(p)
        p = _recompute_scenes_from_content(p, round(duration * 30))
        return p

    props = _apply_deterministic_guarantees(best_props)
    best_findings = _run_props_lint(props)
    # Fix C9（2026-07-16）：确认过的真实生产 bug——_build() 每次调用都会无条件
    # 把自己产出的 props 写到 props_path（见 _build 最后一行），循环跑完之后
    # 磁盘上留的是*最后一次*调用的内容，不一定是 best_props（只有当赢家恰好
    # 是最后一次调用时两者才碰巧一致，dajaai backtest 的真实一跑就撞上了不
    # 一致的情况：第 2 轮赢了 best_props，第 3 轮又调用一次 _build 但没有更
    # 好，磁盘上却被第 3 轮的内容覆盖了）。实际渲染命令读的是 props_path
    # 这个文件，不是这个函数里的 Python 变量——磁盘和内存不同步，意味着
    # C5/C6 循环选出的"最佳版本"可能根本没有被真正渲染，整个丰富度比较沦为
    # 摆设。循环结束后必须显式把 best_props 写回磁盘，不能假设某次内部调用
    # 顺带写对了。
    props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
    (workdir / "props_lint.json").write_text(
        json.dumps(best_findings, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 渲染整片前抽 QA stills 做机器检查 + 视觉复审（video-studio CLAUDE-v2 §9
    # "score before you ship" 自我修正循环的自动化版本）。视觉复审本身
    # （qa_stills._vision_review/call_vision_chat）已经存在；这里补上原本
    # 缺失的一环——真的按它的发现做点什么，而不是只记录进 qa_report.json
    # 就撒手不管。发现 "high" 级问题就把问题喂回内容规划重试一次——只重新走
    # 这一步（一次 LLM 调用 + 一轮 QA stills），不用重新渲染整片。重试后仍有
    # 问题就 raise，交给下面已有的 _DEGRADABLE_OPS 降级交付逻辑处理——不是
    # 发明新的失败处理方式，是复用已经存在、已经验证过的那一套（render 失败
    # 时走的就是同一条路）。
    if not op.get("skipQaStills"):
        from .qa_stills import run_props_qa

        qa_result = run_props_qa(props, props_path, remotion_dir, workdir / "qa_stills")
        major = _major_vision_findings(qa_result, props)
        if major and time.monotonic() >= content_deadline:
            logger.warning(
                "  apply_style: 内容规划总预算已用完，跳过视觉复审触发的重规划，"
                "直接走降级交付（不是新逻辑，跟重试后仍有问题走的是同一条路）"
            )
            raise RuntimeError(f"apply_style: 内容规划总预算已用完，视觉复审发现的问题未再尝试修复，触发降级交付: {major}")
        if major and _is_geometry_or_color_only(major):
            # 取景/对比度问题跟内容规划无关（speakerObjectPosition/colorMode
            # 只算一次，任何重规划都碰不到它们）——走内容重规划必然原样复现
            # 同一个问题，白白多花一轮 LLM + QA stills 还是没用。直接调参数、
            # 重跑一次确定性保底（对齐 scenes）、重新过一遍视觉复审验证。
            props, major = _apply_geometry_color_shortcut(props, props_path, major, remotion_dir, workdir, duration)
            if major:
                raise RuntimeError(f"apply_style: 取景/对比度修正后仍发现问题，触发降级交付: {major}")
        elif major:
            feedback = "; ".join(f"still #{f.get('frame_index')}: {f.get('issue', '')}" for f in major)[:500]
            logger.warning(f"  apply_style: 视觉复审发现问题，重新规划一次: {feedback}")
            replanned = _apply_deterministic_guarantees(_build(feedback=feedback))
            # 重规划内容跟上一轮一字不差 → LLM 没有真的按反馈调整任何东西，
            # 第二轮 qa_stills 几乎一定原样报回同样的 high severity 问题——
            # 直接跳过（省一次视觉模型调用 + 几帧静态图渲染），走跟"重试后
            # 仍有问题"完全一样的降级路径，不做无意义的重复确认。
            if _content_unchanged(props, replanned):
                logger.warning(
                    "  apply_style: 重规划内容与上一轮完全一致，判定反馈未被采纳，"
                    "跳过第二轮视觉复审直接触发降级交付"
                )
                raise RuntimeError(f"apply_style: 重规划未产生实质变化，视觉复审发现的问题预计仍然存在，触发降级交付: {major}")
            props = replanned
            # _build() 自己已经无条件写过一次 props_path（未经确定性保底的版本，
            # 见 Fix C9 的注释）——这里必须用保底之后的版本覆盖写回去，否则
            # run_props_qa/最终渲染读到的还是磁盘上那份没经过 C13/C15 的旧内容
            # （跟 C9 是同一类"内存和磁盘不同步"教训，只是这次是 Fix C16 引入的
            # 新调用点）。
            props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
            qa_result = run_props_qa(props, props_path, remotion_dir, workdir / "qa_stills")
            major = _major_vision_findings(qa_result, props)
            if major and _is_geometry_or_color_only(major):
                # 内容重规划把真正的内容问题（比如上面反馈里带的"纯黑画面"）
                # 修好之后，剩下的发现完全可能变成纯取景/对比度类——这时候
                # 还有必要再走一次内容重规划吗？不需要，跟第一次检查同一条
                # 捷径，同一个函数（Rule 5/13：只在一个调用点生效的修正不是
                # 真正的修正）。
                props, major = _apply_geometry_color_shortcut(props, props_path, major, remotion_dir, workdir, duration)
            if major:
                raise RuntimeError(f"apply_style: 视觉复审重试后仍发现问题，触发降级交付: {major}")

    out = workdir / "_op_styled.mp4"
    npx_bin = shutil.which("npx") or "npx"  # Windows: subprocess needs the resolved npx.cmd, plain "npx" raises WinError 2
    from .remotion_bundle import ensure_remotion_bundle
    bundle = ensure_remotion_bundle(remotion_dir)
    # props_path/out must be absolute — this subprocess runs with cwd=remotion_dir,
    # so a relative path (e.g. "storage/jobs/<id>/_op_apply_style_props.json")
    # resolves against remotion-composer/ instead of the repo root, and Remotion
    # rejects it outright ("neither valid JSON nor a file path to a valid JSON
    # file"). Confirmed real production bug: apply_style silently degraded to
    # the bare unstyled cut on every run where workdir happened to be relative,
    # with qa_stills' own still-renders (same bug, same fix needed there) failing
    # identically just before it.
    cmd = [npx_bin, "remotion", "render"] + ([bundle] if bundle else []) + [
        "XiaojinEditorial", str(out.resolve()),
        f"--props={props_path.resolve()}",
        "--crf=18",
    ]
    logger.info(f"  apply_style: rendering via {' '.join(cmd)} (cwd={remotion_dir})")
    # 重试一次：确认过真实生产 bug——同一份 props/视频独立跑总是成功，只有紧跟在
    # qa_stills 那几次连续 still 渲染后面立刻起片渲染时才会报 "No frame found at
    # position N"（Remotion 自己的 asset 缓存/本地 server 在 qa_stills 和整片渲染
    # 之间交接时的瞬时状态，不是数据或编码问题——独立复现直接 1462/1462 渲染成功）。
    # 跟这个文件里其它瞬时失败（LLM 调用、口误复核）已有的重试模式一致，不是发明
    # 新机制。
    last_result = None
    for attempt in range(2):
        with _RENDER_SLOTS:  # Remotion 渲染跨任务串行 + 硬超时防卡死占坑
            result = subprocess.run(cmd, cwd=str(remotion_dir), capture_output=True, text=True,
                                    timeout=_RENDER_TIMEOUT_S)
        if result.returncode == 0:
            last_result = None
            break
        last_result = result
        if attempt == 0:
            logger.warning(f"  apply_style: 渲染失败(exit {result.returncode})，重试一次: {result.stderr[-500:]}")

    if last_result is not None:
        logger.error(f"apply_style render stderr: {last_result.stderr[-4000:]}")
        raise RuntimeError(f"apply_style 渲染失败 (exit {last_result.returncode})")

    return str(out) if out.exists() else None



_OP_HANDLERS: dict[str, Callable[[str, dict, Path], Optional[str]]] = {
    "trim_start": _op_trim_start,
    "trim_end": _op_trim_end,
    "keep_range": _op_keep_range,
    "remove_segment": _op_remove_segment,
    "remove_silences": _op_remove_silences,
    "remove_filler": _op_remove_filler,
    "speed_up_silence": _op_speed_up_silence,
    "trim_leading_silence": _op_trim_leading_silence,
    "reframe": _op_reframe,
    "color_grade": _op_color_grade,
    "insert_broll": _op_insert_broll,
    # add_music 不在这里注册——和 add_subtitles 一样被摘出主循环单独在最后执行
    # （见 run_talking_head_pipeline 里的 music_op 分支），不走通用 handler 派发。
    "apply_style": _op_apply_style,
    # add_subtitles 在主流程末尾单独处理（需要先转写）；apply_style 已经自带
    # 转写+字幕烧录，跟 add_subtitles 同时出现时 planner 应该只选一个。
}


# ============================================================================
# 辅助
# ============================================================================

# AI 生成花的真金白银（b-roll/背景音乐等）落一个 job 内共享的账本文件，而不是
# 用模块级变量——op handler 之间没有别的共享状态通道，模块级变量在多个任务
# 并发跑（WA_WORKER_CONCURRENCY>1）时会互相污染，文件按 job_dir 天然隔离。
_COST_LEDGER_NAME = "_generation_costs.json"


def _record_generation_cost(workdir: Path, source: str, cost_usd: float) -> None:
    if not cost_usd:
        return
    ledger_path = workdir / _COST_LEDGER_NAME
    entries = []
    if ledger_path.exists():
        try:
            entries = json.loads(ledger_path.read_text(encoding="utf-8"))
        except Exception:
            entries = []
    entries.append({"source": source, "cost_usd": round(cost_usd, 4)})
    ledger_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _read_generation_cost(workdir: Path) -> float:
    ledger_path = workdir / _COST_LEDGER_NAME
    if not ledger_path.exists():
        return 0.0
    try:
        entries = json.loads(ledger_path.read_text(encoding="utf-8"))
        return round(sum(e.get("cost_usd", 0) for e in entries), 4)
    except Exception:
        return 0.0


def _probe_duration(path: Path) -> float:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(probe.stdout.strip())
    except Exception:
        return 0.0


def _probe_dimensions(path: Path) -> tuple:
    """ffprobe 取视频宽高 (w, h)；失败返回 (0, 0)。用于 b-roll PiP 几何。"""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, check=True,
        )
        # 稳健解析：某些容器（如 Remotion 输出的 _op_styled.mp4）ffprobe 会带
        # 尾随分隔符，"1080x1920x" 直接解包 w,h 会 ValueError→被 except 吞成
        # (0,0)，让 insert_broll 误判“无法读取主视频画幅”。改为取前两个纯数字段。
        parts = [p for p in probe.stdout.strip().split("x") if p.isdigit()]
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
        return 0, 0
    except Exception:
        return 0, 0


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ============================================================================
# Script 阶段：转录原始视频 + 产出 script artifact
# ============================================================================

def transcribe_segments(src: str, workdir: Path) -> list[dict]:
    """转录原始视频，返回精简的 [{id,start,end,text}]，缓存到 workdir/script_transcript.json。

    用于 script 阶段——在“规划时”把带时间戳的转录给 agent 看，好让它识别重复句/口误/
    自我打断并生成 remove_segment。这与 add_subtitles 在“执行时”对剪过的视频转写是两回事。
    """
    cache = workdir / "script_transcript.json"
    if cache.exists():
        try:
            segs = json.loads(cache.read_text(encoding="utf-8")).get("segments")
            if segs:
                return segs
        except Exception:
            pass

    # 曾是对 Transcriber() 的裸调用，完全绕开 _safe_transcribe——意味着这条
    # "规划阶段专门用来识别重复句/口误的转写"路径永远在用本地 faster-whisper，
    # 从未真正走到 elevenlabs（确认过的真实生产 bug 根因之一：L2 规划阶段用
    # 这里的转写判断要不要剪重录，判断本身就没吃到更准的转写）。改为调用
    # _safe_transcribe 以复用同一套 provider 分流 + 并发闸门（_TRANSCRIBE_SLOTS）。
    config = get_config()
    t = _safe_transcribe(src, workdir, config.faster_whisper_model)

    if t is None or not t.success:
        logger.warning(f"script 阶段转录失败: {getattr(t, 'error', 'unavailable')}")
        return []

    slim = [
        {"id": s.get("id"), "start": round(_num(s.get("start")) or 0.0, 2),
         "end": round(_num(s.get("end")) or 0.0, 2), "text": (s.get("text") or "").strip()}
        for s in (t.data.get("segments") or [])
    ]
    try:
        cache.write_text(json.dumps(
            {"segments": slim, "language": t.data.get("language"),
             "duration_seconds": t.data.get("duration_seconds")},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return slim


def build_script_artifact(segments: list[dict], duration: float,
                          title: str = "Talking-head source script") -> dict:
    """把转录段落转成 schema 合规的 script artifact（idea→script 阶段的产出）。"""
    sections = []
    for i, s in enumerate(segments, 1):
        st = _num(s.get("start")) or 0.0
        en = _num(s.get("end")) or st
        sections.append({
            "id": f"s{i}",
            "text": (s.get("text") or "").strip() or "(无文本)",
            "start_seconds": round(st, 2),
            "end_seconds": round(max(en, st), 2),
        })
    if not sections:
        sections = [{"id": "s1", "text": "(无转录)", "start_seconds": 0.0,
                     "end_seconds": round(max(duration, 1.0), 2)}]
    return {
        "version": "1.0",
        "title": title,
        "total_duration_seconds": round(max(duration, 1.0), 2),
        "sections": sections,
    }


def validate_artifact(art: dict, schema_name: str) -> tuple[Optional[bool], Optional[str]]:
    """按 schemas/artifacts/<schema_name> 校验 artifact。返回 (True/False/None, err)。"""
    try:
        import jsonschema
    except ImportError:
        return None, "jsonschema 未安装"
    try:
        schema_path = (Path(get_config().openmontage_root)
                       / "schemas" / "artifacts" / schema_name)
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.validate(art, schema)
        return True, None
    except Exception as e:
        return False, str(e)[:300]


def _load_plan(job: Job) -> dict:
    """安全加载 LLM 编辑计划。"""
    try:
        plan = json.loads(job.planned_edit)
        if isinstance(plan, dict):
            return plan
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "edit_operations": [
            {"type": "remove_silences", "description": "去掉停顿使视频更紧凑"},
        ],
        "summary": "默认编辑：去掉停顿",
    }

# ============================================================================
# 浏览器编辑器支持 —— props 保存/渲染 + filmstrip/waveform/预览副本
# ============================================================================

class _EditorPropsInvalid(ValueError):
    """`render_props_directly`（预览编辑器"保存"路径）在 pin+strip 之后，
    发现结果不满足 contracts/render_props.schema.json 时抛出——发生在写盘/
    渲染之前，磁盘上什么都没被动过，调用方（webhook.py）应该直接回 400 +
    这条异常的错误文本，不需要走后台任务、不需要恢复任何快照。"""


class _EditorRenderFailed(RuntimeError):
    """`render_props_directly` 已经通过校验、真正尝试渲染，但
    `_remotion_render_props` 本身失败时抛出——语义跟 `_StyleRerunFailed`
    完全对应（同一类"决定要做、但没做成"），调用方同样必须恢复 props/
    vision-warnings 快照、保留旧 preview.mp4、状态退回 PREVIEW_READY。"""



def _finalize_pipeline_tail(job_dir: Path, src: str, *, subtitle_op: Optional[dict],
                            music_op: Optional[dict], applied: list[str],
                            degraded: list[str], job_id: str,
                            reuse_music: bool = False) -> dict[str, Any]:
    """管线尾段：字幕 -> 背景音乐 -> 定稿 preview.mp4 -> 组装结果 dict。

    从 `run_talking_head_pipeline` 抽出来，供 `rerun_style_only`（只重跑
    apply_style 的预览修订路径）共用——那条路径同样需要"字幕/音乐要不要重新
    走一遍、定稿到 preview.mp4、拼出同样形状的结果 dict"这整段逻辑，不能只
    复制粘贴一份容易跟这边的修复脱节。除了三处必要的参数化替换（preview_path
    本地计算、job.id -> job_id、reuse_music 分支），逻辑与原
    run_talking_head_pipeline 的对应片段完全一致，一字未改。
    """
    preview_path = job_dir / "preview.mp4"

    if subtitle_op is not None:
        logger.info("  执行操作: add_subtitles")
        new_src = _op_add_subtitles(src, subtitle_op, job_dir)
        if new_src and Path(new_src).exists():
            src = str(new_src)
            applied.append("add_subtitles")

    if music_op is not None:
        if reuse_music:
            music_op = {**music_op, "_reuse_cached_music": True}
        logger.info("  执行操作: add_music")
        try:
            new_src = _op_add_music(src, music_op, job_dir)
        except Exception as e:
            # Pixabay 检索走的是公开搜索页爬取（没有官方 API），失败常是 Cloudflare
            # 的人机验证挑战（cf-mitigated: challenge）——这类拦截通常几十秒内自行
            # 放行，立即重试大概率撞在同一个挑战窗口里、白重试一次。等一段再重试，
            # 成功率明显更高（2026-07-17 实测复现过：403 立即重试仍 403，等待后
            # 用完全相同的请求参数直接成功）。
            logger.warning(f"    add_music: 执行失败，{_MUSIC_RETRY_DELAY_S}s 后重试一次。原因: {e}")
            time.sleep(_MUSIC_RETRY_DELAY_S)
            try:
                new_src = _op_add_music(src, music_op, job_dir)
            except Exception as e2:
                logger.warning(
                    f"    add_music: 重试仍失败，降级——保留无背景音乐的版本继续交付"
                    f"（会显性告知用户，非静默）。原因: {e2}"
                )
                new_src = None
                degraded.append("add_music")
        if new_src and Path(new_src).exists():
            src = str(new_src)
            applied.append("add_music")

    # 定稿为 preview.mp4
    if Path(src).resolve() != preview_path.resolve():
        shutil.copyfile(src, preview_path)

    duration = _probe_duration(preview_path)
    if degraded:
        logger.warning(f"=== 降级交付: {job_id} 跳过失败的 {degraded}，交付上一步结果 ===")
    generation_cost = _read_generation_cost(job_dir)
    from .cost_tracking import read_llm_usage
    llm_usage = read_llm_usage(job_dir)
    quality_warnings = _read_vision_qa_warnings(job_dir)
    logger.info(f"=== 管线完成: {job_id} → {preview_path} ({duration:.1f}s), 应用: {applied} ===")
    return {
        "preview_path": str(preview_path),
        "duration": duration,
        "applied_operations": applied,
        "degraded_operations": degraded,
        "generation_cost_usd": generation_cost,
        "llm_tokens_input": llm_usage["prompt_tokens"],
        "llm_tokens_output": llm_usage["completion_tokens"],
        "llm_cost_usd": llm_usage["cost_usd"],
        "quality_warnings": quality_warnings,
    }


_VISION_QA_WARNINGS_NAME = "_vision_qa_warnings.json"


def _read_vision_qa_warnings(workdir: Path) -> list[str]:
    """读回 `_op_apply_style` 交付前记下的、未能在重试内解决的视觉复审发现
    （一句话描述，供交付消息如实告知用户）。没有文件（没发现问题，或走的是
    `skipQaStills` 路径）就返回空列表——不是失败信号，只是"没有需要说的"。"""
    ledger_path = workdir / _VISION_QA_WARNINGS_NAME
    if not ledger_path.exists():
        return []
    try:
        findings = json.loads(ledger_path.read_text(encoding="utf-8"))
        return [f.get("issue", "") for f in findings if isinstance(f, dict) and f.get("issue")]
    except Exception:
        return []


def _remotion_render_props(props_path: Path, out: Path, remotion_dir: Path, *,
                           on_progress: Callable[[str], None] = lambda _s: None,
                           composition: str = "XiaojinEditorial") -> Optional[str]:
    """从 `_op_apply_style` 抽出的渲染子进程调用——不依赖 job/转写/内容规划，
    只要一份已经写盘、已经过 schema 校验的 props 文件，就能渲染。供
    `render_props_directly`（预览编辑器"保存"路径）复用，让两条路径共享同一份
    重试/并发/超时逻辑，不必各自维护一份容易漂移的渲染调用。

    props_path/out 必须是绝对路径——这个子进程以 cwd=remotion_dir 运行，
    相对路径会解析到 remotion-composer/ 而不是仓库根目录，Remotion 会直接
    拒绝（"neither valid JSON nor a file path to a valid JSON file"）。
    """
    npx_bin = shutil.which("npx") or "npx"  # Windows: subprocess needs the resolved npx.cmd, plain "npx" raises WinError 2
    from .remotion_bundle import ensure_remotion_bundle
    bundle = ensure_remotion_bundle(remotion_dir)
    # props_path/out must be absolute — this subprocess runs with cwd=remotion_dir,
    # so a relative path (e.g. "storage/jobs/<id>/_op_apply_style_props.json")
    # resolves against remotion-composer/ instead of the repo root, and Remotion
    # rejects it outright ("neither valid JSON nor a file path to a valid JSON
    # file"). Confirmed real production bug: apply_style silently degraded to
    # the bare unstyled cut on every run where workdir happened to be relative,
    # with qa_stills' own still-renders (same bug, same fix needed there) failing
    # identically just before it.
    cmd = [npx_bin, "remotion", "render"] + ([bundle] if bundle else []) + [
        composition, str(out.resolve()),
        f"--props={props_path.resolve()}",
        "--crf=18",
    ]
    on_progress("rendering")
    logger.info(f"  apply_style: rendering via {' '.join(cmd)} (cwd={remotion_dir})")
    # 重试一次：确认过真实生产 bug——同一份 props/视频独立跑总是成功，只有紧跟在
    # qa_stills 那几次连续 still 渲染后面立刻起片渲染时才会报 "No frame found at
    # position N"（Remotion 自己的 asset 缓存/本地 server 在 qa_stills 和整片渲染
    # 之间交接时的瞬时状态，不是数据或编码问题——独立复现直接 1462/1462 渲染成功）。
    # 跟这个文件里其它瞬时失败（LLM 调用、口误复核）已有的重试模式一致，不是发明
    # 新机制。
    last_result = None
    for attempt in range(2):
        with _RENDER_SLOTS:  # Remotion 渲染跨任务串行 + 硬超时防卡死占坑
            result = subprocess.run(cmd, cwd=str(remotion_dir), capture_output=True, text=True,
                                    timeout=_RENDER_TIMEOUT_S)
        if result.returncode == 0:
            last_result = None
            break
        last_result = result
        if attempt == 0:
            logger.warning(f"  apply_style: 渲染失败(exit {result.returncode})，重试一次: {result.stderr[-500:]}")

    if last_result is not None:
        logger.error(f"apply_style render stderr: {last_result.stderr[-4000:]}")
        raise RuntimeError(f"apply_style 渲染失败 (exit {last_result.returncode})")

_ASSET_PINNED_TOP_LEVEL = ("videoSrc", "durationSeconds")


def pin_server_owned_props(user_props: dict, disk_props: dict) -> dict:
    """预览编辑器"保存"路径的安全闸门：把浏览器提交的这几项资源/时长字段
    强制换成服务端磁盘上真实的值，绝不信任客户端传来的原始内容。

    不是防御性冗余——`render_props.schema.json` 把 `videoSrc` 定义成一个裸
    字符串，没有这层覆盖，一次编辑器保存就能把它设成 `file:///…/.env`
    （本地任意文件读取，读出的内容会被渲染进一帧画面，攻击者下载视频就能
    拿到）或内网地址（SSRF）；`durationSeconds` 同理——`ceil(d*30)` 没有上限，
    设成一个极大值会占住唯一的 `RENDER_SLOTS` 信号量整整 `OM_RENDER_TIMEOUT_S`
    秒，拖垮所有 WhatsApp 任务。

    `videoSrc`/`presenter.src`/`qrContact.qrSrc` 是全部三个真正会被组件当
    资源加载的字段（`SpeakerCard`/`Presenter`/`QRContactCard`）——锁死这三个
    + 给 `durationSeconds` 封顶，就彻底堵死这条注入面，不需要逐个校验其它
    字段。

    `presenter`/`qrContact` 是可选块：磁盘上这个 job 如果从没有过合法的
    `src`/`qrSrc` 可以拿来钉死（例如这条 job 从没跑过 presenter 模式），就不
    猜、不放行用户提交的路径，把用户新加的整块直接去掉——总比信任一个未经
    验证的路径安全。
    """
    props = copy.deepcopy(user_props)
    for key in _ASSET_PINNED_TOP_LEVEL:
        if key in disk_props:
            props[key] = disk_props[key]

    disk_presenter_src = (disk_props.get("presenter") or {}).get("src")
    if "presenter" in props:
        if disk_presenter_src:
            props["presenter"] = {**props["presenter"], "src": disk_presenter_src}
        else:
            props.pop("presenter", None)

    disk_qr_src = (disk_props.get("qrContact") or {}).get("qrSrc")
    if "qrContact" in props:
        if disk_qr_src:
            props["qrContact"] = {**props["qrContact"], "qrSrc": disk_qr_src}
        else:
            props.pop("qrContact", None)

    return props


def _clamp_video_cuts(props: dict, disk_props: dict) -> dict:
    """Clamp client-supplied `videoCuts` against the real on-disk source
    duration before rendering — same threat model as `durationSeconds` above
    (a cut referencing frames past the real source video's end, or a huge
    bogus `toFrame`, would either error out mid-render or hold a
    `RENDER_SLOTS` slot on a nonsense range). `videoCuts` isn't in
    `_ASSET_PINNED_TOP_LEVEL` — the editor is meant to set it — so this
    normalizes/clamps rather than blanket-overwriting. Mirrors
    `normalizeCuts` in remotion-composer/src/cuts.ts exactly (see
    video_cuts.py's own header comment on why a Python port exists at all);
    a client whose source got reprocessed shorter since the editor loaded
    degrades gracefully (cuts past the new end just clamp down) rather than
    hard-failing the whole save.
    """
    from .content_planner import FPS
    from .video_cuts import normalize_cuts

    raw_cuts = props.get("videoCuts")
    if not raw_cuts:
        return props

    disk_duration = disk_props.get("durationSeconds")
    if not disk_duration:
        props.pop("videoCuts", None)
        return props

    import math
    src_len = max(1, math.ceil(float(disk_duration) * FPS))
    cuts = normalize_cuts(raw_cuts if isinstance(raw_cuts, list) else None, src_len)

    if not cuts:
        props.pop("videoCuts", None)
    else:
        props["videoCuts"] = cuts
    return props


def _editor_disk_props(job: Job) -> Optional[dict]:
    props_path = job.job_dir / "_op_apply_style_props.json"
    if props_path.exists():
        try:
            return json.loads(props_path.read_text(encoding="utf-8"))
        except Exception:
            return None
    # Arm B (AI-authored) jobs never produce _op_apply_style_props.json —
    # their render props live at authored/props.json instead, written by
    # authored_renderer._stage_workspace's own props dict, in a different
    # shape (durationInFrames + fps, not durationSeconds). Without this
    # fallback, ensure_editor_filmstrip/ensure_editor_waveform below always
    # returned empty for every Arm B job (this function's None short-circuits
    # both), so the editor's Video/Audio reference lanes were silently
    # always missing on that arm. Normalize into the two keys those two
    # functions actually read (videoSrc, durationSeconds) rather than
    # duplicating their logic for a second props source.
    authored_props_path = job.job_dir / "authored" / "props.json"
    if not authored_props_path.exists():
        return None
    try:
        authored_props = json.loads(authored_props_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    duration_frames = authored_props.get("durationInFrames")
    fps = authored_props.get("fps")
    if not duration_frames or not fps:
        return None
    return {
        "videoSrc": authored_props.get("videoSrc"),
        "durationSeconds": float(duration_frames) / float(fps),
    }


def _source_video_path(job: Job, disk_props: dict) -> Optional[Path]:
    """`videoSrc` on disk is always `f"{local_api_base}/files/{job_id}/{filename}"`
    (see webhook.py's `_rewrite_asset_url` docstring) — resolve it back to the
    real local file under this job's dir without an HTTP round-trip."""
    video_src = disk_props.get("videoSrc")
    if not video_src:
        return None
    filename = video_src.rsplit("/", 1)[-1]
    path = job.job_dir / filename
    return path if path.exists() else None


_FILMSTRIP_COUNT = 10


def ensure_editor_filmstrip(job: Job) -> list[Path]:
    """Generate (once, cached to disk) `_FILMSTRIP_COUNT` evenly-spaced JPEG
    thumbnails from the job's current source video, for the editor timeline's
    Video track. Deliberately raw ffmpeg frame-grabs from the source only —
    no Remotion composition render involved, so none of the "20 full
    composition renders would be brutal" concern that ruled out a filmstrip
    in the Phase 3 editor applies here (this thumbnails the raw clip, not the
    ~40-card composition).
    """
    job_dir = job.job_dir
    existing = [job_dir / f"_editor_filmstrip_{i}.jpg" for i in range(_FILMSTRIP_COUNT)]
    if all(p.exists() for p in existing):
        return existing

    disk_props = _editor_disk_props(job)
    if disk_props is None:
        return []
    src = _source_video_path(job, disk_props)
    duration = disk_props.get("durationSeconds")
    if src is None or not duration or duration <= 0:
        return []

    out_paths: list[Path] = []
    for i in range(_FILMSTRIP_COUNT):
        # Sample the midpoint of each of N equal slices, not the exact edges
        # — avoids landing on frame 0 (often a blank pre-roll moment; same
        # class of "meaningless sample" this codebase already excludes for
        # QA-still sampling elsewhere) or right at EOF.
        t = (i + 0.5) * duration / _FILMSTRIP_COUNT
        out = job_dir / f"_editor_filmstrip_{i}.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(src),
                 "-frames:v", "1", "-vf", "scale=-2:120", "-q:v", "4", str(out)],
                capture_output=True, check=True, timeout=30,
            )
        except Exception:
            logger.warning(f"filmstrip 缩略图生成失败 job={job.id} i={i}", exc_info=True)
            continue
        if out.exists():
            out_paths.append(out)
    return out_paths


_WAVEFORM_NAME = "_editor_waveform.png"


def ensure_editor_waveform(job: Job) -> Optional[Path]:
    """Generate (once, cached) a waveform PNG from the job's current source
    video's audio track via ffmpeg's `showwavespic` filter — one subprocess
    call. Deliberately server-side, not decoded client-side via
    @remotion/media-utils: that would mean downloading/decoding the whole
    audio track on a phone connection just to draw a strip of pixels."""
    job_dir = job.job_dir
    out = job_dir / _WAVEFORM_NAME
    if out.exists():
        return out

    disk_props = _editor_disk_props(job)
    if disk_props is None:
        return None
    src = _source_video_path(job, disk_props)
    if src is None:
        return None

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-filter_complex", "showwavespic=s=1600x120:colors=0x6C63FF",
             "-frames:v", "1", str(out)],
            capture_output=True, check=True, timeout=30,
        )
    except Exception:
        logger.warning(f"waveform 生成失败 job={job.id}", exc_info=True)
        return None
    return out if out.exists() else None


_EDITOR_PREVIEW_VIDEO_NAME = "_editor_preview.mp4"


def ensure_editor_preview_video(job: Job) -> Optional[Path]:
    """Generate (once, cached) a small, faststart (moov-at-front) copy of the
    job's current source video, for the browser editor's `<video>` element to
    load.

    Two separate bugs stacked here, found in order:

    1. **Root cause of "permanently black, no error".** Every intermediate
       `_op_*.mp4` this pipeline produces (confirmed: 70/70
       `_op_audio_enhance.mp4` on disk, the file `videoSrc` actually points at
       15/15 times on the current code path) has its `moov` atom written
       LAST — `_op_audio_enhance.mp4` specifically is a pure `-c:v copy`
       remux (`tools/audio/audio_enhance.py`), and `-c:v copy` does NOT imply
       faststart. A browser cannot decode a single frame of a non-faststart
       MP4 until it has fetched essentially the whole file (the moov atom
       carries the sample tables). Confirmed via direct atom parsing: moov at
       byte 8,016,184 of 8,070,768 on a real job.

    2. **Root cause of "faststart fixed, still nothing for 17+ seconds".**
       Confirmed live via a real user's Network tab: with faststart alone
       (pure `-c copy` remux, same ~8MB as the source), the browser's request
       for the file transferred only ~3.7MB in 17+ seconds over their actual
       connection to the ngrok tunnel (~220KB/s) — a direct curl of the exact
       same URL through the exact same path (ngrok -> Node gateway -> this
       server) from a different network got the full file in under 5s, so
       this is that specific user's link to the tunnel being slow, not a
       server/proxy bug. Faststart alone doesn't help enough at that speed
       for an ~8MB file. Fix: re-encode down to something that finishes fast
       even on a slow link — editing (positioning cards, checking caption
       sync, judging cut points) doesn't need source-quality video.

    Deliberately NOT a pure remux anymore (was `-c copy`, sub-second) — this
    re-encodes, which costs a few seconds of one-time generation (cached
    after) in exchange for a file roughly an order of magnitude smaller.

    It intentionally does NOT touch the original file: `render_props_directly`
    always renders from the real `videoSrc` on disk (this copy is
    browser-facing only, see `webhook._rewrite_props_for_browser`), and
    `pin_server_owned_props` overwrites whatever `videoSrc` a save
    round-trips back before it's ever persisted, so this cannot leak into
    what actually gets rendered or delivered.
    """
    job_dir = job.job_dir
    out = job_dir / _EDITOR_PREVIEW_VIDEO_NAME
    if out.exists():
        return out

    disk_props = _editor_disk_props(job)
    if disk_props is None:
        return None
    src = _source_video_path(job, disk_props)
    if src is None:
        return None

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             # Portrait source (~478x850 typical) -> cap width at 360,
             # height auto (kept even, "-2") — plenty for judging card
             # position/timing/captions on a phone or laptop screen; not
             # meant to represent final delivered quality.
             "-vf", "scale=360:-2",
             "-c:v", "libx264", "-crf", "30", "-preset", "veryfast",
             # -ar 44100 is load-bearing, not cosmetic: this pipeline's
             # source audio is 96kHz (confirmed via ffprobe), and at a small
             # bitrate that combination made Chrome's WebAudio pipeline
             # (which Remotion's Player routes audio through for volume
             # control) throw a real, reproducible error mid-playback —
             # "Code 3 - PipelineStatus::AUDIO_RENDERER_ERROR" — confirmed
             # live via a real user's console, right around the 1s mark.
             # 44.1kHz is the standard, universally-supported web audio rate.
             "-c:a", "aac", "-ar", "44100", "-b:a", "128k",
             "-movflags", "+faststart",
             str(out)],
            capture_output=True, check=True, timeout=120,
        )
    except Exception:
        logger.warning(f"编辑器预览副本生成失败 job={job.id}——编辑器会退回原始文件（可能仍然黑屏/很慢）", exc_info=True)
        return None
    return out if out.exists() else None

def validate_render_props(props: dict) -> tuple[Optional[bool], Optional[str]]:
    """按 contracts/render_props.schema.json 校验渲染 props。返回
    (True/False/None, err)——跟同文件里 `validate_artifact` 同一套约定
    （None 表示 jsonschema 没装，不是校验失败；False 才是真的没通过）。"""
    try:
        import jsonschema
    except ImportError:
        return None, "jsonschema 未安装"
    try:
        schema_path = Path(get_config().openmontage_root) / "contracts" / "render_props.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.validate(props, schema)
        return True, None
    except jsonschema.ValidationError as e:
        return False, e.message
    except Exception as e:
        return False, str(e)[:300]


def apply_editor_music_volume(music_op: Optional[dict], music_volume: Any) -> Optional[dict]:
    """Editor-authored musicVolume override (Phase C) for the render_props_directly
    save path. `music_volume` absent/non-numeric (the overwhelming majority
    of saves, and every job before this field existed) returns `music_op`
    untouched — unchanged behavior. `music_volume <= 0` means "drop the
    music bed" rather than passing 0 through: _op_add_music's own volume
    read (`vol = _num(op.get("volume")); vol = ... if vol else 0.18`) treats
    an explicit 0 as falsy and silently substitutes the 0.18 default, which
    would be exactly backwards for a user who dragged the slider to mute.
    Never mutates `music_op` in place."""
    if not isinstance(music_volume, (int, float)):
        return music_op
    if music_volume <= 0:
        return None
    if music_op is None:
        return None
    return {**music_op, "volume": music_volume}


def render_props_directly(job: Job, user_props: dict, *,
                          on_progress: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """预览编辑器"保存"的渲染路径——用户在浏览器里手改的 props 直接拿去渲染，
    不经过 `plan_content`、不经过 `_apply_deterministic_guarantees`、不经过
    props_lint 重试循环、不经过视觉复审。用户的编辑就是最终结果，这几层
    "自动纠正"机制全都会在用户没要求的情况下悄悄改写内容（详见
    `_op_apply_style` 内 `_apply_deterministic_guarantees` 和
    `_recompute_scenes_from_content` 的文档）——编辑器场景下这些改写反而是
    需要绕开的东西，不是需要复用的保障。

    raises:
        _EditorPropsInvalid: pin+strip 之后仍不满足 schema——磁盘上什么都
            没被动过，调用方应直接回 400，不必进后台任务、不必恢复快照。
        _EditorRenderFailed: 校验通过、真正尝试渲染，但渲染本身失败——旧
            preview.mp4 未被触碰，props/vision-warnings 快照已恢复。
    """
    job_dir = job.job_dir
    disk_props_path = job_dir / "_op_apply_style_props.json"
    disk_props: dict = {}
    if disk_props_path.exists():
        try:
            disk_props = json.loads(disk_props_path.read_text(encoding="utf-8"))
        except Exception:
            disk_props = {}

    props = pin_server_owned_props(user_props, disk_props)
    props = _clamp_video_cuts(props, disk_props)
    # contentBeats 在 XiaojinEditorial.tsx 里被解构读取，但从未被加进 schema
    # （schema 是 additionalProperties:false）——任何带着它的 props 都会在
    # jsonschema 这一步被直接拒绝。防御性剔除，不让这个历史遗留字段挡路。
    props.pop("contentBeats", None)

    ok, err = validate_render_props(props)
    if ok is False:
        raise _EditorPropsInvalid(f"props failed contract② validation: {err}")

    # 快照当前 props/视觉复审警告，供渲染失败时恢复——跟 rerun_style_only
    # 同一个理由：`_animations_summary`（webhook.py）不能读到一份从未真正
    # 渲染成功的 props，向用户播报假动画清单；_vision_qa_warnings.json 只在
    # 非空时被覆写，干净的保存必须显式清掉旧账。
    warnings_path = job_dir / _VISION_QA_WARNINGS_NAME
    prev_props_text = disk_props_path.read_text(encoding="utf-8") if disk_props_path.exists() else None
    prev_warnings_text = warnings_path.read_text(encoding="utf-8") if warnings_path.exists() else None

    def _restore_snapshots() -> None:
        if prev_props_text is not None:
            disk_props_path.write_text(prev_props_text, encoding="utf-8")
        else:
            disk_props_path.unlink(missing_ok=True)
        if prev_warnings_text is not None:
            warnings_path.write_text(prev_warnings_text, encoding="utf-8")
        else:
            warnings_path.unlink(missing_ok=True)

    disk_props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")

    remotion_dir = Path(get_config().openmontage_root) / "remotion-composer"
    out = job_dir / "_op_styled.mp4"
    try:
        styled = _remotion_render_props(disk_props_path, out, remotion_dir,
                                        on_progress=on_progress or (lambda _s: None))
    except Exception as e:
        _restore_snapshots()
        raise _EditorRenderFailed(f"editor render failed: {e}") from e
    if not styled or not Path(styled).exists():
        _restore_snapshots()
        raise _EditorRenderFailed("editor render produced no output")

    # 没有跑过视觉复审——必须在 _finalize_pipeline_tail 之前清掉，不能等它
    # 之后再删：_finalize_pipeline_tail 内部会调用 _read_vision_qa_warnings
    # 读这个文件算 quality_warnings（Fix，2026-07-27 真实复现——这里原来写
    # 在 _finalize_pipeline_tail 之后，导致一次编辑器保存的 quality_warnings
    # 里混进了这个 job 更早一轮、完全无关的旧视觉复审发现，读的时候已经
    # 来不及了）。
    warnings_path.unlink(missing_ok=True)

    plan = _load_plan(job)
    operations = plan.get("edit_operations", [])
    music_op = next((o for o in operations if o.get("type") == "add_music"), None)
    subtitle_op = next((o for o in operations if o.get("type") == "add_subtitles"), None)
    music_op = apply_editor_music_volume(music_op, props.get("musicVolume"))
    result = _finalize_pipeline_tail(job_dir, styled, subtitle_op=subtitle_op, music_op=music_op,
                                     applied=["apply_style"], degraded=[], job_id=job.id,
                                     reuse_music=True)

    # 标记这个 job 已经被手动编辑过——server/worker.js 的 reviseJob 靠它决定
    # 要不要在 WhatsApp 文字反馈快速路径（会从转写重新生成一切）覆盖用户的
    # 手动改动前先弹一句警告确认（Stage 7）。只在这里（保存成功）写，不在
    # 校验失败/渲染失败的分支写——那些情况用户的手动编辑其实没有真正生效。
    (job_dir / "_manual_edit.json").write_text(
        json.dumps({"edited_at": time.time()}), encoding="utf-8")

    prev_degraded: list[str] = []
    try:
        prev_degraded = json.loads(job.degraded_operations) if job.degraded_operations else []
    except Exception:
        prev_degraded = []
    carried_over = [d for d in prev_degraded if d not in ("apply_style", "add_music", "add_subtitles")]
    result["degraded_operations"] = list(dict.fromkeys(result["degraded_operations"] + carried_over))
    return result


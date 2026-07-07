# WhatsApp MVP - Pipeline Runner
# L1.5：通过 OpenMontage 正式工具执行 talking-head 编辑。
# 结构为 "op -> handler 注册表"：每个 handler 是对一个正式工具的薄封装，
# 这一层在将来升级到 L2（agent 编排）时可原样复用，只需换掉上面的编排头。

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from .config import get_config
from .database import Job

logger = logging.getLogger(__name__)


# ============================================================================
# 主入口
# ============================================================================

def run_talking_head_pipeline(job: Job) -> dict[str, Any]:
    """按编辑计划用 OpenMontage 正式工具执行编辑，输出 preview.mp4。

    顺序：先应用所有视频类操作，字幕留到最后（转写才对得上剪过的时间轴）。
    """
    job_dir = job.job_dir
    input_video = job_dir / "input.mp4"
    preview_path = job_dir / "preview.mp4"

    if not input_video.exists():
        raise FileNotFoundError(f"找不到输入视频: {input_video}")

    plan = _load_plan(job)
    operations = plan.get("edit_operations", [])
    logger.info(f"=== 运行管线: {job.id}，{len(operations)} 个操作 ===")

    src = str(input_video)
    applied: list[str] = []

    # 执行顺序：多个 remove_segment 按 start 降序“从后往前”切（转录给的是原始时间轴
    # 坐标；从后往前切，前面的刀就不会移动后面那刀之前的坐标）。其余视频操作保持原序，
    # 字幕永远最后（要对剪过的时间轴转写）。
    subtitle_ops = [op for op in operations if op.get("type") == "add_subtitles"]
    video_ops = [op for op in operations if op.get("type") != "add_subtitles"]
    removes = sorted(
        [op for op in video_ops if op.get("type") == "remove_segment"],
        key=lambda o: _num(o.get("start_seconds")) or 0.0, reverse=True,
    )
    others = [op for op in video_ops if op.get("type") != "remove_segment"]
    ordered_ops = removes + others
    if len(removes) > 1:
        logger.info(f"  {len(removes)} 段 remove_segment 将按起点降序执行（防时间轴错位）")

    subtitle_op: Optional[dict] = subtitle_ops[0] if subtitle_ops else None

    for op in ordered_ops:
        op_type = op.get("type", "")
        handler = _OP_HANDLERS.get(op_type)
        if handler is None:
            logger.warning(f"  跳过不支持的操作: {op_type}")
            continue
        logger.info(f"  执行操作: {op_type}")
        before = _probe_duration(Path(src))
        new_src = handler(src, op, job_dir)
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

    # 定稿为 preview.mp4
    if Path(src).resolve() != preview_path.resolve():
        shutil.copyfile(src, preview_path)

    duration = _probe_duration(preview_path)
    logger.info(f"=== 管线完成: {job.id} → {preview_path} ({duration:.1f}s), 应用: {applied} ===")
    return {
        "preview_path": str(preview_path),
        "duration": duration,
        "applied_operations": applied,
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
    这两个参数同时也是契约①（op_registry）里 remove_silences 的可选参数。
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


def _safe_transcribe(src: str, workdir: Path, model_size: str):
    """跑 Transcriber，把"工具报告失败"和"工具本身抛异常"这两种失败都统一
    收敛成返回 None——Transcriber 底层用的 faster-whisper/PyAV 在遇到损坏或
    非视频文件时，实测会直接抛 av.error.InvalidDataError 之类的异常，不会走
    它自己 ToolResult(success=False) 那条路径。调用方不该因为转写这一步失败
    就整个崩掉，应该拿到一个清楚的"没有转写结果"信号去走降级逻辑。
    """
    import os as _os

    from tools.analysis.transcriber import Transcriber

    config = get_config()
    _hf = _os.environ.pop("HF_TOKEN", None)
    try:
        t = Transcriber().execute({
            "input_path": src,
            "output_dir": str(workdir),
            "model_size": model_size,
        })
    except Exception as e:
        logger.warning(f"  转写调用异常（非工具自身报告的失败，是真的抛异常）: {e}")
        return None
    finally:
        if _hf is not None:
            _os.environ["HF_TOKEN"] = _hf

    if not t.success:
        logger.warning(f"  转写失败: {t.error}")
        return None
    return t


def _op_remove_filler(src: str, op: dict, workdir: Path) -> Optional[str]:
    """转写(词级) -> LLM 判断口误/重录 -> VideoTrimmer concat 只保留干净片段。

    这是 video-use / edit-director.md 的思路："不用打分公式，让 LLM 读转写稿
    自己判断哪里该剪"——跟 remove_silences 的纯静音检测是互补的两件事，静音
    检测测不到有声的语气词、也测不到中间没停顿的重录。LLM 不可用、判断没有
    需要剪的地方、或者转写本身失败时，都原样返回，不影响后续步骤——这个操作
    整体是锦上添花的精修，不应该因为它失败就搞垮整条剪辑流程。
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


def _op_add_subtitles(src: str, op: dict, workdir: Path) -> Optional[str]:
    """转写 -> 烧录字幕（原语言）。翻译成其他语言暂不支持（见 planner 的 unsupported）。

    这里跟 apply_style/remove_filler 不同：字幕是用户显式要的东西，转写失败时
    没有"降级但仍然有意义"的输出可给，所以仍然是 raise，不做静默兜底——但用
    _safe_transcribe 统一收敛异常，让失败原因清楚可读，而不是让 av 库的原始
    异常直接炸穿上层。
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
    r = RemotionCaptionBurn().execute({
        "input_path": src, "output_path": str(out),
        "segments": segments, "force_ffmpeg": True,
    })
    if not r.success:
        raise RuntimeError(f"字幕烧录失败: {r.error}")
    return r.artifacts[0] if r.artifacts else str(out)


# ---------------------------------------------------------------------------
# apply_style — renders the branded XiaojinEditorial template (contract ②).
#
# Split into a pure prop-builder (`build_xiaojin_render_props`, no side
# effects beyond the video copy, independently testable/schema-validatable)
# and the handler itself (which also invokes the actual render). Per the P2
# task spec: the acceptance bar for this task is "the props this function
# builds pass contracts/render_props.schema.json" — NOT "the render
# succeeds". XiaojinEditorial on P3's side may not consume these props
# correctly yet; that integration happens once P3 finishes their half.
# ---------------------------------------------------------------------------

# Fallback objectPosition when no source-video face calibration has been run
# yet (P2 MVP scope — see contracts/README.md: "P2 runs face_tracker on the
# source" is the eventual real version of this).
_DEFAULT_SPEAKER_OBJECT_POSITION = "50% 35%"
_DEFAULT_INTRO_OUT_FRAME = 20

# ---------------------------------------------------------------------------
# Canvas / chrome geometry + beat-driven scene generation.
#
# Numbers come from video-studio's compose-director.md style codex (1080x1920,
# ChapterNav 0-88, BrandBar y=1848, captions pinned bottom:90) — that codex is
# what produced the reference-quality builds, and the core rule this encodes
# is: the speaker card only shrinks WHEN content needs the canvas, and returns
# to full when it doesn't. A fixed scene schedule (the previous _DEFAULT_SCENE)
# leaves most of the canvas empty for most of the video, which is exactly the
# "under-edited" failure video-studio's docs call out.
# ---------------------------------------------------------------------------
_FPS = 30
# Card boxes. FULL: near-full portrait (bottom=1800, clears BrandBar@1848).
# CONTENT: card docked to the top so the zone below is free for data cards
# (codex: workflow card min height 900 — shorter crops a portrait source to
# head-only).
_FULL_BOX = {"x": 60, "y": 104, "w": 960, "h": 1696}
_CONTENT_BOX = {"x": 40, "y": 104, "w": 1000, "h": 900}
_CONTENT_TOP = 1044          # 40px gap below CONTENT box bottom (1004)
_CAPTION_SAFE_TOP = 1680     # caption pill zone starts around here; content must end above
_TRANSITION_FRAMES = 20      # codex: APPLE easing over 20 frames
_CARD_HOLD_FRAMES = 150      # ~5s reading hold after the last row lands
_MERGE_GAP_FRAMES = 60       # gaps shorter than this stay in content mode (no thrash)
_DATA_CARD_ROW_H = 64        # InfoCard row height estimate for overflow clamping
_DATA_CARD_CHROME_H = 96     # title + padding estimate


def _data_card_end_frame(card: dict) -> int:
    """Frame at which a card's content has fully landed and been read."""
    last_row = max((r.get("mountOffset", 0) for r in card.get("rows", [])), default=0)
    return card["mountFrame"] + last_row + _CARD_HOLD_FRAMES


def build_xiaojin_scenes(content_plan: dict, duration_seconds: float) -> list[dict]:
    """dataCards 的 beat -> SpeakerCard 的 scenes 关键帧。纯函数。

    规则（对齐 compose-director 的实际做法）：卡片默认全屏漂浮；只在某张数据卡
    需要画布时，提前 20 帧过渡到 CONTENT 盒（顶部停靠），数据卡读完（最后一行
    落位 + ~5s）再过渡回全屏。相邻数据卡间隔太短就保持 CONTENT 不来回抖。
    没有任何数据卡（很常见、完全正常）-> 单关键帧全屏，卡片全程不动。

    SpeakerCard 对 scenes 做的是全序列 interpolate（帧号必须严格递增），所以
    "停留"要靠成对关键帧表达：{f,box} {f+20,box'} 之间是过渡，其余区间保持。
    """
    duration_frames = max(1, round(duration_seconds * _FPS))
    cards = sorted(content_plan.get("dataCards") or [], key=lambda c: c["mountFrame"])
    if not cards:
        return [{"frame": 0, **_FULL_BOX}]

    # Shrink intervals [start, end] in frames, merged when nearly adjacent.
    intervals: list[list[int]] = []
    for card in cards:
        start = max(card["mountFrame"] - _TRANSITION_FRAMES, 1)
        end = _data_card_end_frame(card)
        if intervals and start - intervals[-1][1] <= _MERGE_GAP_FRAMES + 2 * _TRANSITION_FRAMES:
            intervals[-1][1] = max(intervals[-1][1], end)
        else:
            intervals.append([start, end])

    scenes: list[dict] = [{"frame": 0, **_FULL_BOX}]
    for start, end in intervals:
        # Guarantee strictly increasing frames even for a card mounting at ~0.
        start = max(start, scenes[-1]["frame"] + 1)
        end = max(end, start + _TRANSITION_FRAMES + 1)
        scenes.append({"frame": start, **_FULL_BOX})
        scenes.append({"frame": start + _TRANSITION_FRAMES, **_CONTENT_BOX})
        if end + _TRANSITION_FRAMES >= duration_frames:
            break  # video ends while in content mode — stay docked, no return
        scenes.append({"frame": end, **_CONTENT_BOX})
        scenes.append({"frame": end + _TRANSITION_FRAMES, **_FULL_BOX})
    return scenes


def place_data_cards(data_cards: list[dict]) -> list[dict]:
    """给数据卡补默认坐标并夹进安全区。纯函数，不改传入的列表。

    契约②里 dataCards 的 schema 默认 y=900——但卡片 CONTENT 盒底在 1004，
    y=900 会被说话人卡片压住；底部还有字幕带(~1680 起)/BrandBar(1848)。这里
    统一放进内容区(_CONTENT_TOP 起)，并按行数估高夹住 y 保证不进字幕带。
    """
    placed = []
    for card in data_cards:
        c = dict(card)
        est_h = _DATA_CARD_CHROME_H + len(c.get("rows", [])) * _DATA_CARD_ROW_H
        c.setdefault("x", 80)
        c.setdefault("width", 920)
        y = c.get("y", _CONTENT_TOP + 56)
        y = max(y, _CONTENT_TOP)
        y = min(y, _CAPTION_SAFE_TOP - est_h)
        c["y"] = y
        placed.append(c)
    return placed


def calibrate_speaker_object_position(src: str, workdir: Path) -> str:
    """对源视频跑 face_tracker，取人脸中心的中位数 -> CSS object-position 字符串。

    compose-director.md 的校准公式：objPos ≈ (face_center_y_in_source /
    source_height) * 100 —— FaceTracker 的 bbox 已经是按源视频宽高归一化过的
    比例（0..1），所以人脸中心比例可以直接当百分比用，不需要再除一次源尺寸。

    用中位数而不是均值：跟 clipper 技能的 smart_crop.py 一个思路——对偶尔的
    误检测/头部转动更稳健。检测不到人脸时，原样退回旧的静态默认值
    "50% 35%"，不让这一步的失败搞垮整条 apply_style。
    """
    try:
        from tools.analysis.face_tracker import FaceTracker

        out_json = workdir / "_op_apply_style_faces.json"
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
        cx = centers_x[mid]
        cy = centers_y[mid]

        obj_pos = f"{round(cx * 100)}% {round(cy * 100)}%"
        logger.info(f"  apply_style: 人脸校准取景 -> {obj_pos}（{len(faces)}帧检出人脸，取中位数）")
        return obj_pos
    except Exception as e:
        logger.warning(f"  apply_style: face_tracker 调用异常，用默认取景: {e}")
        return _DEFAULT_SPEAKER_OBJECT_POSITION


def apply_style_params_to_op(op: dict, style_params: dict) -> dict:
    """契约③(style_params，来自 reference_analyzer 对示例视频的分析)-> 合并进
    op 参数里，喂给 build_xiaojin_render_props。纯函数，不改动传入的 op。

    contracts/README.md 说得很明确：style_params 只管审美（配色/字幕观感/画幅/
    节奏），不管 speakerObjectPosition/scenes——那两个必须来自源视频本身的人脸
    位置，不能被示例视频带偏。这里只把 colorMode 合并进去；explicit 的
    op["colorMode"]（如果调用方直接传了）优先级更高，不会被示例视频覆盖。
    """
    merged = dict(op)
    if style_params.get("colorMode") and "colorMode" not in op:
        merged["colorMode"] = style_params["colorMode"]
    return merged


def resolve_reframe_op(style_params: dict, source_aspect: Optional[str]) -> Optional[dict]:
    """判断示例视频的画幅要不要触发对源视频的 reframe。

    XiaojinEditorial 目前只有一个固定竖屏画布（1080x1920，scenes 坐标是相对
    这个画布写死的），所以"画幅"这个风格参数落不到 render_props 里任何字段
    上——它真正的意义是："如果源视频本身不是竖屏，需要先跑一次 reframe 操作
    再喂给 apply_style"。这是编排层（P1 的活）该往 edit_operations 里插的一
    个操作，不是 apply_style 自己该做的事——这个函数只负责判断"要不要"，
    返回一个可以直接放进 edit_operations 的 reframe op dict，不自己执行。
    """
    target = style_params.get("aspect")
    if not target:
        return None
    if source_aspect and source_aspect == target:
        return None
    return {"type": "reframe", "aspect": target, "description": f"按示例视频画幅转成 {target}"}


def build_xiaojin_render_props(
    video_src_rel: str,
    duration_seconds: float,
    captions: list[dict],
    content_plan: dict,
    op: dict,
) -> dict:
    """拼契约②(render_props.schema.json)吃的 props dict。纯函数，方便脱离
    真实渲染单独做 schema 校验（这个任务的验收门就是这个，不是渲染成功）。
    """
    props: dict = {
        "videoSrc": video_src_rel,
        "durationSeconds": duration_seconds,
        "colorMode": op.get("colorMode", "warm"),
        "speakerObjectPosition": op.get("speakerObjectPosition", _DEFAULT_SPEAKER_OBJECT_POSITION),
        "scenes": op.get("scenes") or build_xiaojin_scenes(content_plan, duration_seconds),
        "introOutFrame": op.get("introOutFrame", _DEFAULT_INTRO_OUT_FRAME),
        "chapters": content_plan.get("chapters", []),
        "captions": captions,
    }
    if content_plan.get("dataCards"):
        props["dataCards"] = place_data_cards(content_plan["dataCards"])
    if op.get("compliance"):
        props["compliance"] = op["compliance"]
    if op.get("brand"):
        props["brand"] = op["brand"]
    if op.get("intro"):
        props["intro"] = op["intro"]
    if op.get("outro"):
        props["outro"] = op["outro"]
    if op.get("headingFont"):
        props["headingFont"] = op["headingFont"]
    if op.get("labelFont"):
        props["labelFont"] = op["labelFont"]
    return props


_MAX_CAPTION_WORDS = 7
_MAX_CAPTION_CHARS = 42


def build_caption_phrases(words: list[dict], segments: list[dict]) -> list[dict]:
    """词级时间戳 -> 短语级字幕。纯函数。

    直接用转写 segment 当字幕，一条能到 200+ 字符、屏幕上 5-6 行，把画面压掉
    小半截——codex 明确要求 phrase-level captions。这里按词重组：满 7 词/42 字
    符、或遇到句读（.?!，。？！）就断一条。没有词级数据时退回 segment 级
    （长，但有总比没有好）。
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
        text = "".join(w["word"] for w in cur).strip()
        if text:
            phrases.append({
                "text": text,
                "startMs": round(cur[0]["start"] * 1000),
                "endMs": round(cur[-1]["end"] * 1000),
            })
        cur.clear()

    for w in words:
        cur.append(w)
        text = "".join(x["word"] for x in cur).strip()
        ends_sentence = text.endswith((".", "?", "!", "。", "？", "！", ",", "，"))
        if len(cur) >= _MAX_CAPTION_WORDS or len(text) >= _MAX_CAPTION_CHARS or ends_sentence:
            flush()
    flush()
    return phrases


def _op_apply_style(src: str, op: dict, workdir: Path) -> Optional[str]:
    """转写 + content_planner 内容规划 + 人脸校准 -> 拼契约②的 props -> 渲染 XiaojinEditorial。

    跟 add_subtitles 是互斥的两个操作（这个已经自带转写+烧字幕），上游 planner
    不应该把两个都放进同一个 edit_operations 里。

    speakerObjectPosition 现在是对源视频真实跑 face_tracker 校准出来的（见
    calibrate_speaker_object_position），不再是固定默认值——除非 op 里显式
    传了 speakerObjectPosition，那种情况尊重调用方的显式覆盖。scenes 卡片
    位置本身是画布坐标、跟源视频尺寸无关，仍用默认盒子。
    """
    from .content_planner import plan_content

    config = get_config()
    src_path = Path(src)

    t = _safe_transcribe(src, workdir, config.faster_whisper_model)

    duration = _probe_duration(src_path)

    if t is None:
        # 转写失败（无论是工具报告失败，还是转写库本身抛异常）都不该搞垮整条
        # apply_style——用户仍然应该拿到一条"有卡片+品牌条+进度条，但没有字幕/
        # 章节/数据卡"的降级版本，好过什么都没有。
        logger.warning("  apply_style: 转写不可用（降级为无字幕版本）")
        captions: list[dict] = []
        content_plan = {"chapters": [], "dataCards": []}
    else:
        captions = build_caption_phrases(
            t.data.get("word_timestamps") or [], t.data.get("segments") or []
        )
        logger.info("  apply_style: 内容规划中（章节 + 数据卡）...")
        content_plan = plan_content(t.data.get("segments") or [], duration)
        logger.info(
            f"  apply_style: 规划出 {len(content_plan['chapters'])} 个章节、"
            f"{len(content_plan['dataCards'])} 个数据卡"
        )

    if op.get("style"):
        op = apply_style_params_to_op(op, op["style"])

    if not op.get("speakerObjectPosition"):
        op = {**op, "speakerObjectPosition": calibrate_speaker_object_position(src, workdir)}

    remotion_dir = Path(config.openmontage_root) / "remotion-composer"
    job_slug = workdir.name
    video_src_rel = f"jobs/{job_slug}/source.mp4"
    video_src_abs = remotion_dir / "public" / "jobs" / job_slug / "source.mp4"
    video_src_abs.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, video_src_abs)

    props = build_xiaojin_render_props(video_src_rel, duration, captions, content_plan, op)
    props_path = workdir / "_op_apply_style_props.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")

    # 渲染整片前先抽 QA stills 做机器检查（video-studio CLAUDE-v2 §8 的自动化
    # 部分）。findings 目前只记录不阻断——空画布这类问题是布局生成的 bug 信号，
    # 值得暴露在日志里，但半成品总好过整条失败。stills 和 qa_report.json 留在
    # workdir，后续 P1 的 agent 可拿去做有眼睛的复审。
    if not op.get("skipQaStills"):
        from .qa_stills import run_props_qa

        run_props_qa(props, props_path, remotion_dir, workdir / "qa_stills")

    out = workdir / "_op_styled.mp4"
    cmd = [
        "npx", "remotion", "render", "XiaojinEditorial", str(out),
        f"--props={props_path}",
        "--crf=18",
    ]
    logger.info(f"  apply_style: rendering via {' '.join(cmd)} (cwd={remotion_dir})")
    result = subprocess.run(cmd, cwd=str(remotion_dir), capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"apply_style render stderr: {result.stderr[-4000:]}")
        raise RuntimeError(f"apply_style 渲染失败 (exit {result.returncode})")

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
    "apply_style": _op_apply_style,
    # add_subtitles 在主流程末尾单独处理（需要先转写）；apply_style
    # 会自带转写+烧字幕，跟 add_subtitles 同时出现时上游 planner 应该只选一个。
}


# ============================================================================
# 辅助
# ============================================================================

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

    import os as _os

    from tools.analysis.transcriber import Transcriber

    config = get_config()
    # 临时移除可能含非 ASCII 的 HF_TOKEN，避免 httpx header 编码错误
    _hf = _os.environ.pop("HF_TOKEN", None)
    try:
        t = Transcriber().execute({
            "input_path": src, "output_dir": str(workdir),
            "model_size": config.faster_whisper_model,
        })
    finally:
        if _hf is not None:
            _os.environ["HF_TOKEN"] = _hf

    if not t.success:
        logger.warning(f"script 阶段转录失败: {t.error}")
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

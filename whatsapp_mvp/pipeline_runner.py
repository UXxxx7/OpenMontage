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
    subtitle_op: Optional[dict] = None
    applied: list[str] = []

    for op in operations:
        op_type = op.get("type", "")
        if op_type == "add_subtitles":
            subtitle_op = op  # 字幕最后处理
            continue
        handler = _OP_HANDLERS.get(op_type)
        if handler is None:
            logger.warning(f"  跳过不支持的操作: {op_type}")
            continue
        logger.info(f"  执行操作: {op_type}")
        new_src = handler(src, op, job_dir)
        if new_src and Path(new_src).exists():
            src = str(new_src)
            applied.append(op_type)

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
    """去掉所有静音/停顿使更紧凑 -> SilenceCutter mode=remove。

    min_silence_duration/silence_threshold_db 是契约①（op_registry）里
    remove_silences 的可选参数——SilenceCutter 工具本身早就支持这两个输入，
    只是 handler 之前没转发，这里补上。不传时工具自己的默认值生效
    （min_silence_duration=0.5, silence_threshold_db=-35）。
    """
    from tools.video.silence_cutter import SilenceCutter
    out = workdir / "_op_nosilence.mp4"
    inputs = {"input_path": src, "mode": "remove", "output_path": str(out)}
    if op.get("min_silence_duration") is not None:
        inputs["min_silence_duration"] = op["min_silence_duration"]
    if op.get("silence_threshold_db") is not None:
        inputs["silence_threshold_db"] = op["silence_threshold_db"]
    r = SilenceCutter().execute(inputs)
    if not r.success:
        raise RuntimeError(f"remove_silences 失败: {r.error}")
    # 无静音时工具会把 output 设为原文件路径
    return r.data.get("output") or (r.artifacts[0] if r.artifacts else None)


def _op_remove_filler(src: str, op: dict, workdir: Path) -> Optional[str]:
    """转写(词级) -> LLM 判断口误/重录 -> VideoTrimmer concat 只保留干净片段。

    这是 video-use / edit-director.md 的思路："不用打分公式，让 LLM 读转写稿
    自己判断哪里该剪"——跟 remove_silences 的纯静音检测是互补的两件事，静音
    检测测不到有声的语气词、也测不到中间没停顿的重录。LLM 不可用或判断没有
    需要剪的地方时，原样返回，不影响后续步骤。
    """
    import os as _os

    from tools.analysis.transcriber import Transcriber
    from tools.video.video_trimmer import VideoTrimmer

    from .content_planner import plan_filler_removal

    config = get_config()

    _hf = _os.environ.pop("HF_TOKEN", None)
    try:
        t = Transcriber().execute({
            "input_path": src,
            "output_dir": str(workdir),
            "model_size": config.faster_whisper_model,
        })
    finally:
        if _hf is not None:
            _os.environ["HF_TOKEN"] = _hf

    if not t.success:
        raise RuntimeError(f"remove_filler 转写失败: {t.error}")

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
    """只去掉开头静音 -> SilenceCutter mark 找第一句话起点，再 VideoTrimmer cut。"""
    from tools.video.silence_cutter import SilenceCutter
    from tools.video.video_trimmer import VideoTrimmer
    mark_json = workdir / "_op_silence_mark.json"
    r = SilenceCutter().execute({
        "input_path": src, "mode": "mark", "output_path": str(mark_json),
    })
    if not r.success:
        logger.warning(f"  trim_leading_silence: mark 失败 {r.error}")
        return None
    try:
        data = json.loads(Path(r.data["output"]).read_text(encoding="utf-8"))
        speech = data.get("speech_segments", [])
    except Exception as e:
        logger.warning(f"  trim_leading_silence: 读取 mark 结果失败 {e}")
        return None
    if not speech:
        return None
    first_start = _num(speech[0].get("start")) or 0.0
    if first_start < 0.3:
        return None  # 开头没有明显静音
    out = workdir / "_op_trim_lead.mp4"
    tr = VideoTrimmer().execute({
        "operation": "cut", "input_path": src,
        "start_seconds": first_start, "codec": "libx264", "output_path": str(out),
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
    """转写 -> 烧录字幕（原语言）。翻译成其他语言暂不支持（见 planner 的 unsupported）。"""
    import os as _os

    from tools.analysis.transcriber import Transcriber
    from tools.video.remotion_caption_burn import RemotionCaptionBurn

    config = get_config()

    # 临时移除可能含非 ASCII 的 HF_TOKEN，避免 httpx header 编码错误
    _hf = _os.environ.pop("HF_TOKEN", None)
    try:
        t = Transcriber().execute({
            "input_path": src,
            "output_dir": str(workdir),
            "model_size": config.faster_whisper_model,
        })
    finally:
        if _hf is not None:
            _os.environ["HF_TOKEN"] = _hf

    if not t.success:
        raise RuntimeError(f"转写失败: {t.error}")
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

# Fallback scene/objectPosition when no source-video face calibration has
# been run yet (P2 MVP scope — see contracts/README.md: "P2 runs
# face_tracker on the source" is the eventual real version of this).
_DEFAULT_SPEAKER_OBJECT_POSITION = "50% 35%"
_DEFAULT_SCENE = {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100}
_DEFAULT_INTRO_OUT_FRAME = 20


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
        "scenes": op.get("scenes") or [dict(_DEFAULT_SCENE)],
        "introOutFrame": op.get("introOutFrame", _DEFAULT_INTRO_OUT_FRAME),
        "chapters": content_plan.get("chapters", []),
        "captions": captions,
    }
    if content_plan.get("dataCards"):
        props["dataCards"] = content_plan["dataCards"]
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


def _op_apply_style(src: str, op: dict, workdir: Path) -> Optional[str]:
    """转写 + content_planner 内容规划 -> 拼契约②的 props -> 渲染 XiaojinEditorial。

    跟 add_subtitles 是互斥的两个操作（这个已经自带转写+烧字幕），上游 planner
    不应该把两个都放进同一个 edit_operations 里。

    speakerObjectPosition/scenes 目前是 MVP 默认值（见模块顶部注释），没有对
    源视频做人脸校准——按 P2 任务说明，这是有意的分阶段简化，不是遗漏。
    """
    import os as _os

    from tools.analysis.transcriber import Transcriber

    from .content_planner import plan_content

    config = get_config()
    src_path = Path(src)

    _hf = _os.environ.pop("HF_TOKEN", None)
    try:
        t = Transcriber().execute({
            "input_path": src,
            "output_dir": str(workdir),
            "model_size": config.faster_whisper_model,
        })
    finally:
        if _hf is not None:
            _os.environ["HF_TOKEN"] = _hf

    if not t.success:
        raise RuntimeError(f"apply_style 转写失败: {t.error}")

    captions = [
        {"text": seg["text"].strip(), "startMs": round(seg["start"] * 1000), "endMs": round(seg["end"] * 1000)}
        for seg in (t.data.get("segments") or [])
        if seg.get("text", "").strip()
    ]

    duration = _probe_duration(src_path)
    logger.info("  apply_style: 内容规划中（章节 + 数据卡）...")
    content_plan = plan_content(t.data.get("segments") or [], duration)
    logger.info(
        f"  apply_style: 规划出 {len(content_plan['chapters'])} 个章节、"
        f"{len(content_plan['dataCards'])} 个数据卡"
    )

    remotion_dir = Path(config.openmontage_root) / "remotion-composer"
    job_slug = workdir.name
    video_src_rel = f"jobs/{job_slug}/source.mp4"
    video_src_abs = remotion_dir / "public" / "jobs" / job_slug / "source.mp4"
    video_src_abs.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, video_src_abs)

    props = build_xiaojin_render_props(video_src_rel, duration, captions, content_plan, op)
    props_path = workdir / "_op_apply_style_props.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")

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

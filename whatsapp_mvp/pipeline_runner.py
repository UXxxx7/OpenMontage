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

# Fallback scene/objectPosition when no source-video face calibration has
# been run yet (P2 MVP scope — see contracts/README.md: "P2 runs
# face_tracker on the source" is the eventual real version of this).
_DEFAULT_SPEAKER_OBJECT_POSITION = "50% 35%"
_DEFAULT_SCENE = {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100}
_DEFAULT_INTRO_OUT_FRAME = 20


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
        captions = [
            {"text": seg["text"].strip(), "startMs": round(seg["start"] * 1000), "endMs": round(seg["end"] * 1000)}
            for seg in (t.data.get("segments") or [])
            if seg.get("text", "").strip()
        ]
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

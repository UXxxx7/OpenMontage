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
    degraded: list[str] = []  # 非致命失败：跳过后仍交付上一步结果的操作

    # 优雅降级：这些操作失败时不许拖垮整个 job——保留上一步剪好的视频继续交付。
    # apply_style 是重量级 Remotion 渲染（失败面多：模板/字体/依赖/props）；零指令默认
    # 是 [remove_filler, apply_style]，渲染挂了也必须把剪好的视频还给用户，而不是整单报错。
    # 后续 compose 段算子（color_grade / audio_enhance 等）落地时按需加进来。
    _DEGRADABLE_OPS = {"apply_style", "insert_broll"}

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
        try:
            new_src = handler(src, op, job_dir)
        except Exception as e:
            if op_type in _DEGRADABLE_OPS:
                logger.warning(
                    f"    {op_type}: 执行失败，优雅降级——保留上一步结果继续交付。原因: {e}"
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

    # 定稿为 preview.mp4
    if Path(src).resolve() != preview_path.resolve():
        shutil.copyfile(src, preview_path)

    duration = _probe_duration(preview_path)
    if degraded:
        logger.warning(f"=== 降级交付: {job.id} 跳过失败的 {degraded}，交付上一步结果 ===")
    logger.info(f"=== 管线完成: {job.id} → {preview_path} ({duration:.1f}s), 应用: {applied} ===")
    return {
        "preview_path": str(preview_path),
        "duration": duration,
        "applied_operations": applied,
        "degraded_operations": degraded,
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

def _safe_transcribe(src: str, workdir: Path, model_size: str):
    """跑 Transcriber，把"工具报告失败"和"工具本身抛异常"统一收敛成返回 None。

    faster-whisper/PyAV 对损坏/非视频输入会直接抛 av.error.InvalidDataError
    之类的异常（实测），不会走 ToolResult(success=False)；调用方拿到 None 再
    决定降级还是报错，而不是被底层异常炸穿。
    """
    import os as _os

    from tools.analysis.transcriber import Transcriber

    _hf = _os.environ.pop("HF_TOKEN", None)
    try:
        t = Transcriber().execute({
            "input_path": src,
            "output_dir": str(workdir),
            "model_size": model_size,
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
    return t


_DEFAULT_SPEAKER_OBJECT_POSITION = "50% 35%"


def calibrate_speaker_object_position(src: str, workdir: Path) -> str:
    """对源视频跑 face_tracker，取人脸中心中位数 -> CSS object-position。

    compose-director.md 的强制校准项：objPos ≈ face_center_y/source_height*100，
    不同源视频没有通用值。检测不到人脸/缺 opencv 时退回静态默认值。
    （注意本机 opencv-python 必须 <5：5.0 wheel 不带 Haar cascade。）
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
        obj_pos = f"{round(centers_x[mid] * 100)}% {round(centers_y[mid] * 100)}%"
        logger.info(f"  apply_style: 人脸校准取景 -> {obj_pos}（{len(faces)}帧检出，取中位数）")
        return obj_pos
    except Exception as e:
        logger.warning(f"  apply_style: face_tracker 调用异常，用默认取景: {e}")
        return _DEFAULT_SPEAKER_OBJECT_POSITION


_MAX_CAPTION_WORDS = 7
_MAX_CAPTION_CHARS = 42


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


def _op_insert_broll(src: str, op: dict, workdir: Path) -> Optional[str]:
    """把用户上传的 b-roll 叠进成片（video_compose overlay，保留说话人原声，薄封装）。
    资产文件在 workdir/assets/broll_<asset_ref>.*（Phase 1 已下载）。
    默认 PiP 右下角小窗；mode=cutaway 时全屏。整片收尾类，排在剪辑之后。
    offset_seconds=start 让素材从插入点开始播；图片 loop 填满窗口。"""
    from tools.video.video_compose import VideoCompose
    items = op.get("items") or []
    if not items:
        return None
    assets_dir = workdir / "assets"
    base_w, base_h = _probe_dimensions(Path(src))
    if not base_w or not base_h:
        raise RuntimeError("insert_broll: 无法读取主视频画幅")
    overlays: list[dict] = []
    for it in items:
        ref = it.get("asset_ref")
        start = _num(it.get("start_seconds"))
        end = _num(it.get("end_seconds"))
        if ref is None or start is None or end is None or end <= start:
            continue
        matches = sorted(assets_dir.glob(f"broll_{ref}.*"))
        if not matches:
            logger.warning(f"  insert_broll: 找不到资产 broll_{ref}.*，跳过")
            continue
        asset = matches[0]
        is_image = asset.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
        # 视频素材：窗口收到不超过素材本身时长，避免叠加末尾冻帧
        if not is_image:
            clip_dur = _probe_duration(asset)
            if clip_dur and (end - start) > clip_dur:
                end = start + clip_dur
        mode = str(it.get("mode") or "pip").lower()
        if mode == "cutaway":
            w, h, x, y = base_w, base_h, 0, 0
        else:  # 默认 PiP：右下角小窗，留脸
            w = int(base_w * 0.38)
            h = int(w * 9 / 16)
            x = base_w - w - 40
            y = base_h - h - 40
        overlays.append({
            "asset_path": str(asset), "x": x, "y": y, "width": w, "height": h,
            "start_seconds": start, "end_seconds": end,
            "offset_seconds": start,        # 从插入点开始播（配合 video_compose 新参数）
            "loop": bool(is_image),         # 图片循环填满窗口
        })
    if not overlays:
        return None
    out = workdir / "_op_insert_broll.mp4"
    r = VideoCompose().execute({
        "operation": "overlay", "input_path": src,
        "overlays": overlays, "output_path": str(out),
    })
    if not r.success:
        raise RuntimeError(f"insert_broll 失败: {r.error}")
    return r.data.get("output") or (r.artifacts[0] if r.artifacts else None)


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
# reasons about WHEN to be in which mode (dominant vs workflow); the actual
# pixel box a mode maps to is a rendering-layer concern, not a planning one.
_DOMINANT_BOX = {"x": 60, "y": 104, "w": 960, "h": 1100}
_WORKFLOW_BOX = {"x": 740, "y": 1200, "w": 300, "h": 531}


def _mode_schedule_to_scenes(mode_schedule: list[dict]) -> list[dict]:
    """content_planner 的 dominant/workflow 模式时间表 -> contract② 的 scenes（具体像素坐标）。"""
    box_by_mode = {"dominant": _DOMINANT_BOX, "workflow": _WORKFLOW_BOX}
    return [
        {"frame": entry["frame"], **box_by_mode.get(entry.get("mode"), _DOMINANT_BOX)}
        for entry in mode_schedule
    ]


def _run_enhancement_chain(src: str, workdir: Path) -> str:
    """face_enhance -> color_grade -> audio_enhance，best-effort（单步失败不影响其它步骤）。

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
    else:
        segments = t.data.get("segments") or []
        # 短语级字幕（词级时间戳重组），不是 5-6 行的 segment 大段
        captions = build_caption_phrases(t.data.get("word_timestamps") or [], segments)

    if op.get("chapters") or op.get("data_cards"):
        chapters = op.get("chapters") or []
        data_cards = op.get("data_cards") or []
        gauges = op.get("gauges") or []
        countdowns = op.get("countdowns") or []
        calendar_events = op.get("calendar_events") or []
        mode_schedule = op.get("mode_schedule") or [{"frame": 0, "mode": "dominant"}]
        plan_intro = None
        plan_outro = None
        plan_sections = op.get("sections") or []
        plan_quotes = op.get("quotes") or []
        plan_atmosphere = op.get("atmosphere_keywords") or []
    else:
        logger.info("  apply_style: 内容规划中（章节 + 数据展示分析）...")
        content_plan = plan_content(segments, duration)
        chapters = content_plan["chapters"]
        data_cards = content_plan["data_cards"]
        gauges = content_plan["gauges"]
        countdowns = content_plan["countdowns"]
        calendar_events = content_plan["calendar_events"]
        mode_schedule = content_plan["mode_schedule"]
        plan_intro = content_plan.get("intro")
        plan_outro = content_plan.get("outro")
        plan_sections = content_plan.get("sections") or []
        plan_quotes = content_plan.get("quotes") or []
        plan_atmosphere = content_plan.get("atmosphere_keywords") or []
        logger.info(
            f"  apply_style: 规划出 {len(chapters)} 个章节、{len(data_cards)} 个数据卡、"
            f"{len(gauges)} 个仪表盘、{len(countdowns)} 个倒计时、{len(calendar_events)} 个日历"
        )

    scenes = _mode_schedule_to_scenes(mode_schedule)

    remotion_dir = Path(config.openmontage_root) / "remotion-composer"
    job_slug = workdir.name
    public_video_rel = f"jobs/{job_slug}/source.mp4"
    public_video_abs = remotion_dir / "public" / "jobs" / job_slug / "source.mp4"
    public_video_abs.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, public_video_abs)

    props: dict[str, Any] = {
        "videoSrc": public_video_rel,
        "durationSeconds": duration,
        "colorMode": op.get("colorMode", "warm"),
        "speakerObjectPosition": op.get("speaker_object_position")
            or calibrate_speaker_object_position(src, workdir),
        "scenes": scenes,
        "introOutFrame": 20,
        "chapters": chapters,
        "captions": captions,
    }
    if plan_sections:
        props["sections"] = plan_sections
    if plan_quotes:
        props["quotes"] = plan_quotes
    if plan_atmosphere:
        props["atmosphereKeywords"] = plan_atmosphere

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
        clamped = []
        for m in mode_schedule:
            m = dict(m)
            if m.get("mode") == "workflow" and m["frame"] < intro_out + 20:
                m["frame"] = intro_out + 20
            clamped.append(m)
        # 保持严格递增（推迟后可能与后续项撞帧）
        mode_schedule = []
        for m in sorted(clamped, key=lambda x: x["frame"]):
            if mode_schedule and m["frame"] <= mode_schedule[-1]["frame"]:
                continue
            mode_schedule.append(m)
        props["scenes"] = _mode_schedule_to_scenes(mode_schedule)
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
    outro = op.get("outro") or plan_outro
    if outro:
        duration_frames = max(1, round(duration * 30))
        # 片尾最后 ~5s 交给 outro（不足 12s 的视频不上 outro，避免喧宾夺主）
        if duration_frames >= 360:
            outro = dict(outro)
            outro.setdefault("fromFrame", duration_frames - 150)
            props["outro"] = outro
    if data_cards:
        props["dataCards"] = data_cards
    if gauges:
        props["gauges"] = gauges
    if countdowns:
        props["countdowns"] = countdowns
    if calendar_events:
        props["calendarEvents"] = calendar_events
    qr_input = op.get("qr_contact") or {}
    if qr_input.get("contact_url"):
        from .qr_gen import generate_qr
        qr_rel = f"jobs/{job_slug}/qr.png"
        qr_abs = remotion_dir / "public" / qr_rel
        if generate_qr(qr_input["contact_url"], qr_abs):
            qr_contact: dict[str, Any] = {
                "qrSrc": qr_rel,
                "contactName": qr_input.get("contact_name", ""),
                "ctaLabel": qr_input.get("cta_label", "WhatsApp Now"),
                "mountFrame": qr_input.get("mount_frame", max(0, round(duration * 30) - 200)),
            }
            if qr_input.get("contact_company"):
                qr_contact["contactCompany"] = qr_input["contact_company"]
            props["qrContact"] = qr_contact
    if op.get("brand"):
        props["brand"] = op["brand"]
    elif op.get("compliance"):
        props["compliance"] = op["compliance"]

    props_path = workdir / "_op_apply_style_props.json"
    props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")

    # 渲染整片前抽 QA stills 做机器检查（video-studio CLAUDE-v2 §8 的可自动化
    # 部分）。findings 只记录不阻断；stills + qa_report.json 留在 workdir 供
    # 有视觉的 agent 复审。
    if not op.get("skipQaStills"):
        from .qa_stills import run_props_qa

        run_props_qa(props, props_path, remotion_dir, workdir / "qa_stills")

    out = workdir / "_op_styled.mp4"
    npx_bin = shutil.which("npx") or "npx"  # Windows: subprocess needs the resolved npx.cmd, plain "npx" raises WinError 2
    cmd = [
        npx_bin, "remotion", "render", "XiaojinEditorial", str(out),
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
    "color_grade": _op_color_grade,
    "insert_broll": _op_insert_broll,
    "apply_style": _op_apply_style,
    # add_subtitles 在主流程末尾单独处理（需要先转写）；apply_style 已经自带
    # 转写+字幕烧录，跟 add_subtitles 同时出现时 planner 应该只选一个。
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


def _probe_dimensions(path: Path) -> tuple:
    """ffprobe 取视频宽高 (w, h)；失败返回 (0, 0)。用于 b-roll PiP 几何。"""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, check=True,
        )
        w, h = probe.stdout.strip().split("x")
        return int(w), int(h)
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
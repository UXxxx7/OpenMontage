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


_OP_HANDLERS: dict[str, Callable[[str, dict, Path], Optional[str]]] = {
    "trim_start": _op_trim_start,
    "trim_end": _op_trim_end,
    "keep_range": _op_keep_range,
    "remove_segment": _op_remove_segment,
    "remove_silences": _op_remove_silences,
    "speed_up_silence": _op_speed_up_silence,
    "trim_leading_silence": _op_trim_leading_silence,
    "reframe": _op_reframe,
    # add_subtitles 在主流程末尾单独处理（需要先转写）
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

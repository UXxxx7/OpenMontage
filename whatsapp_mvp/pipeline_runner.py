# WhatsApp MVP - Pipeline Runner
# 连接 WhatsApp 任务到 OpenMontage talking-head 管线。

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import get_config
from .database import Job

logger = logging.getLogger(__name__)


# ============================================================================
# 主入口
# ============================================================================

def run_talking_head_pipeline(job: Job) -> dict[str, Any]:
    """运行完整 talking-head 管线：转写 → 静音检测 → 字幕 → 剪辑 → 预览。

    Returns:
        {"preview_path": str, "transcript_path": str, "duration": float}
    """
    job_dir = job.job_dir
    input_video = job_dir / "input.mp4"
    preview_path = job_dir / "preview.mp4"

    if not input_video.exists():
        raise FileNotFoundError(f"找不到输入视频: {input_video}")

    logger.info(f"=== 开始运行管线: {job.id} ===")
    logger.info(f"  输入: {input_video}")

    # ── 步骤1: 转写 ──
    logger.info("步骤1/4: 语音转写...")
    transcript_path = run_transcription(input_video, job_dir)

    # ── 步骤2: 静音检测 ──
    logger.info("步骤2/4: 静音检测...")
    silences = detect_silence(input_video)

    # ── 步骤3: 构建剪辑决策 ──
    logger.info("步骤3/4: 构建剪辑决策...")
    plan = _load_plan(job)
    edit_cuts = build_edit_cuts(plan, silences, input_video)

    # ── 步骤4: 合成预览 ──
    logger.info("步骤4/4: 合成预览视频...")
    duration = compose_preview(input_video, edit_cuts, transcript_path, preview_path, plan)

    logger.info(f"=== 管线完成: {job.id} → {preview_path} ({duration:.1f}s) ===")

    return {
        "preview_path": str(preview_path),
        "transcript_path": str(transcript_path),
        "duration": duration,
    }


def run_final_export(job: Job) -> dict[str, Any]:
    """最终导出：基于预览生成高质量 final.mp4。

    Phase 6: 使用更高码率重新编码预览。
    """
    job_dir = job.job_dir
    preview_path = job_dir / "preview.mp4"
    final_path = job_dir / "final.mp4"

    if not preview_path.exists():
        # 回退：如果没有预览，重新运行管线
        logger.warning("预览不存在，重新运行管线生成最终版本")
        result = run_talking_head_pipeline(job)
        preview_path = Path(result["preview_path"])

    logger.info(f"最终导出: {preview_path} → {final_path}")

    # 高质量重新编码
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(preview_path),
            "-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(final_path),
        ],
        capture_output=True,
        check=True,
    )

    logger.info(f"最终导出完成: {final_path}")
    return {"final_path": str(final_path)}


# ============================================================================
# 转写
# ============================================================================

def run_transcription(video_path: Path, output_dir: Path) -> Path:
    """faster-whisper 语音转写，输出 transcript.json。"""
    config = get_config()
    model_size = config.faster_whisper_model

    # 提取音频
    audio_path = output_dir / "audio.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(audio_path),
        ],
        capture_output=True,
        check=True,
    )

    # 转写（清理可能含非ASCII的环境变量，避免 httpx header 编码错误）
    import os as _os
    _hf_token = _os.environ.pop("HF_TOKEN", None)
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    finally:
        if _hf_token:
            _os.environ["HF_TOKEN"] = _hf_token
    segments, info = model.transcribe(str(audio_path), beam_size=5, word_timestamps=True)

    transcript = {
        "language": info.language,
        "duration": info.duration,
        "segments": [],
    }
    for seg in segments:
        words = [
            {"word": w.word, "start": w.start, "end": w.end}
            for w in (seg.words or [])
        ]
        transcript["segments"].append({
            "id": seg.id,
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
            "words": words,
        })

    transcript_path = output_dir / "transcript.json"
    transcript_path.write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"  转写完成: {len(transcript['segments'])} 段, 语言={info.language}")
    return transcript_path


# ============================================================================
# 静音检测
# ============================================================================

def detect_silence(video_path: Path) -> list[dict]:
    """FFmpeg silencedetect 检测静音段。"""
    result = subprocess.run(
        [
            "ffmpeg", "-i", str(video_path),
            "-af", "silencedetect=noise=-30dB:d=0.5",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    silences = []
    for line in result.stderr.split("\n"):
        if "silence_start" in line:
            start = float(re.search(r"silence_start:\s*([\d.]+)", line).group(1))
            silences.append({"start": start})
        elif "silence_end" in line:
            end = float(re.search(r"silence_end:\s*([\d.]+)", line).group(1))
            duration = float(re.search(r"silence_duration:\s*([\d.]+)", line).group(1))
            if silences:
                silences[-1].update({"end": end, "duration": duration})

    logger.info(f"  检测到 {len(silences)} 段静音")
    return silences


# ============================================================================
# 剪辑决策
# ============================================================================

def build_edit_cuts(
    plan: dict,
    silences: list[dict],
    video_path: Path,
    known_duration: float | None = None,
) -> list[dict]:
    """根据 LLM 编辑计划 + 静音数据构建具体的剪辑决策。

    返回: [{"start": float, "end": float}, ...]  要保留的片段列表
    """
    # 获取视频时长
    if known_duration is not None:
        duration = known_duration
    else:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, check=True,
        )
        duration = float(probe.stdout.strip())
    logger.info(f"  视频时长: {duration:.1f}s")

    operations = plan.get("edit_operations", [])

    # 默认保留全部
    keep_segments = [(0.0, duration)]
    trim_lead = 0.0  # 开头要裁剪的秒数
    trim_trail = 0.0  # 结尾要裁剪的秒数

    for op in operations:
        op_type = op.get("type", "")

        if op_type == "trim_leading_silence":
            # 找到第一个非静音点
            if silences and silences[0].get("start", 0) < 0.5:
                # 从开头开始的静音段
                trim_lead = silences[0].get("end", 0)
                logger.info(f"  裁剪开头静音: 0 → {trim_lead:.1f}s")

        elif op_type == "trim_trailing_silence":
            # 找到最后一个非静音点
            if silences:
                last = silences[-1]
                if last.get("end", 0) > duration - 1.0:
                    trim_trail = last.get("start", duration)
                    logger.info(f"  裁剪结尾静音: {trim_trail:.1f}s → {duration:.1f}s")

        elif op_type == "remove_repetition":
            logger.info("  标记重复片段（Phase 6: 基础版跳过复杂重复检测）")

        elif op_type == "remove_interruption":
            logger.info("  标记中断片段（Phase 6: 基础版跳过复杂中断检测）")

        elif op_type == "smooth_cuts":
            logger.info("  启用平滑过渡")

    # 应用 trim
    keep_segments = [(
        max(0, trim_lead),
        min(duration, duration - trim_trail),
    )]

    # 移除中间的显著静音段（>1.5s 的静音）
    if any(op.get("type") in ("remove_repetition", "smooth_cuts") for op in operations):
        keep_segments = _remove_mid_silences(keep_segments, silences, min_gap=1.5)

    logger.info(f"  最终保留 {len(keep_segments)} 个片段")
    for i, (s, e) in enumerate(keep_segments):
        logger.info(f"    片段{i+1}: {s:.1f}s → {e:.1f}s ({(e-s):.1f}s)")

    return keep_segments


def _remove_mid_silences(
    segments: list[tuple[float, float]],
    silences: list[dict],
    min_gap: float = 1.5,
) -> list[tuple[float, float]]:
    """移除较长静音段（开头、中间、结尾）。"""
    result = []
    for seg_start, seg_end in segments:
        current = seg_start
        effective_end = seg_end
        for sil in silences:
            s_start = sil.get("start", 0)
            s_end = sil.get("end", 0)
            s_dur = sil.get("duration", 0)

            # 开头静音：推进 current（不限时长）
            if s_start <= seg_start + 0.5:
                current = max(current, s_end)
                continue

            # 结尾静音：缩短 effective_end（不限时长）
            if s_end >= seg_end - 0.5:
                effective_end = min(effective_end, s_start)
                continue

            # 中间静音：跳过短静音
            if s_dur < min_gap:
                continue

            if s_start < current:
                continue

            # 保留 current → s_start，跳过静音段
            if s_start - current > 0.5:
                result.append((current, s_start))
            current = s_end

        # 保留最后一段
        if effective_end - current > 0.5:
            result.append((current, effective_end))

    return result if result else segments


# ============================================================================
# 预览合成
# ============================================================================

def compose_preview(
    video_path: Path,
    edit_cuts: list[tuple[float, float]],
    transcript_path: Path,
    output_path: Path,
    plan: dict,
) -> float:
    """使用 FFmpeg 合成预览视频：裁剪 + 拼接 + 烧录字幕。"""
    has_subtitles = any(
        op.get("type") == "add_subtitles"
        for op in plan.get("edit_operations", [])
    )

    # 生成 SRT 字幕
    srt_path = None
    if has_subtitles:
        srt_path = output_path.parent / "subtitles.srt"
        srt_path = generate_subtitles(transcript_path, srt_path)

    if len(edit_cuts) == 1 and not has_subtitles:
        # 简单情况：单个片段，无字幕 → 直接裁剪复制
        start, end = edit_cuts[0]
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start), "-to", str(end),
             "-i", str(video_path), "-c", "copy", str(output_path)],
            capture_output=True, check=True,
        )
        return end - start

    # 复杂情况：多片段拼接 + 字幕
    return _compose_with_concat(video_path, edit_cuts, srt_path, output_path)


def _compose_with_concat(
    video_path: Path,
    cuts: list[tuple[float, float]],
    srt_path: Path | None,
    output_path: Path,
) -> float:
    """用 FFmpeg concat demuxer 拼接多个片段并烧录字幕。"""
    # 步骤1: 逐个裁剪片段
    clip_files = []
    temp_dir = output_path.parent
    total_duration = 0.0

    for i, (start, end) in enumerate(cuts):
        clip_path = temp_dir / f"_clip_{i:03d}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start), "-to", str(end),
             "-i", str(video_path), "-c", "copy", str(clip_path)],
            capture_output=True, check=True,
        )
        clip_files.append(clip_path)
        total_duration += end - start

    if len(clip_files) == 1 and not srt_path:
        clip_files[0].rename(output_path)
        return total_duration

    # 步骤2: 创建 concat 文件列表
    concat_file = temp_dir / "_concat.txt"
    concat_lines = [f"file '{str(f).replace(chr(92), '/')}'" for f in clip_files]
    concat_file.write_text("\n".join(concat_lines), encoding="utf-8")

    # 步骤3: 拼接（+ 烧录字幕）
    if srt_path and srt_path.exists():
        # 需要重新编码才能烧录字幕
        srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
        subprocess.run(
            ["ffmpeg", "-y",
             "-f", "concat", "-safe", "0", "-i", str(concat_file),
             "-vf", f"subtitles='{srt_escaped}':force_style='FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1'",
             "-c:v", "libx264", "-crf", "23", "-preset", "fast",
             "-c:a", "aac", "-b:a", "128k",
             str(output_path)],
            capture_output=True, check=True,
        )
    else:
        subprocess.run(
            ["ffmpeg", "-y",
             "-f", "concat", "-safe", "0", "-i", str(concat_file),
             "-c", "copy", str(output_path)],
            capture_output=True, check=True,
        )

    # 清理临时文件
    for f in clip_files:
        f.unlink(missing_ok=True)
    concat_file.unlink(missing_ok=True)

    logger.info(f"  合成完成: {output_path} ({total_duration:.1f}s)")
    return total_duration


# ============================================================================
# 字幕生成
# ============================================================================

def generate_subtitles(transcript_path: Path, output_path: Path) -> Path:
    """从 transcript.json 生成 SRT 字幕。"""
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))

    srt_lines = []
    for i, seg in enumerate(transcript["segments"], 1):
        start_ts = _format_timestamp(seg["start"])
        end_ts = _format_timestamp(seg["end"])
        srt_lines.append(f"{i}")
        srt_lines.append(f"{start_ts} --> {end_ts}")
        srt_lines.append(seg["text"])
        srt_lines.append("")

    output_path.write_text("\n".join(srt_lines), encoding="utf-8")
    logger.info(f"  字幕: {output_path}")
    return output_path


def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ============================================================================
# 辅助函数
# ============================================================================

def _load_plan(job: Job) -> dict:
    """安全加载 LLM 编辑计划。"""
    try:
        return json.loads(job.planned_edit)
    except (json.JSONDecodeError, TypeError):
        return {
            "edit_operations": [
                {"type": "trim_leading_silence", "description": "去除开头空白"},
                {"type": "add_subtitles", "language": "zh", "description": "添加中文字幕"},
            ],
            "summary": "默认编辑计划",
        }


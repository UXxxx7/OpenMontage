"""Video trimmer tool wrapping FFmpeg.

Provides cut, trim, speed adjustment, and concatenation of video segments.
All operations are deterministic and produce lossless or near-lossless output
by default.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ResumeSupport,
    ToolResult,
    ToolStability,
    ToolTier,
)


class VideoTrimmer(BaseTool):
    name = "video_trimmer"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "video_post"
    provider = "ffmpeg"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["cmd:ffmpeg"]
    install_instructions = (
        "Install FFmpeg: https://ffmpeg.org/download.html\n"
        "Windows: winget install FFmpeg\n"
        "macOS: brew install ffmpeg\n"
        "Linux: sudo apt install ffmpeg"
    )
    agent_skills = ["ffmpeg", "video-toolkit"]

    capabilities = ["cut", "trim", "speed_adjust", "concat"]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["cut", "speed", "concat"],
            },
            "input_path": {"type": "string"},
            "output_path": {"type": "string"},
            "start_seconds": {"type": "number", "minimum": 0},
            "end_seconds": {"type": "number", "minimum": 0},
            "speed_factor": {"type": "number", "minimum": 0.1, "maximum": 100.0},
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "input_path": {"type": "string"},
                        "start_seconds": {"type": "number"},
                        "end_seconds": {"type": "number"},
                    },
                },
            },
            "codec": {"type": "string", "default": "copy"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=2000, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["FFmpeg error"])
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["operation", "input_path", "start_seconds", "end_seconds", "speed_factor"]
    side_effects = ["writes video file to output_path"]
    user_visible_verification = ["Play trimmed output and verify cut points"]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        operation = inputs["operation"]
        start = time.time()

        try:
            if operation == "cut":
                result = self._cut(inputs)
            elif operation == "speed":
                result = self._speed(inputs)
            elif operation == "concat":
                result = self._concat(inputs)
            else:
                return ToolResult(success=False, error=f"Unknown operation: {operation}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        result.duration_seconds = round(time.time() - start, 2)
        return result

    def _cut(self, inputs: dict[str, Any]) -> ToolResult:
        input_path = Path(inputs["input_path"])
        if not input_path.exists():
            return ToolResult(success=False, error=f"Input not found: {input_path}")

        start_s = inputs.get("start_seconds", 0)
        end_s = inputs.get("end_seconds")
        codec = inputs.get("codec", "copy")
        output_path = Path(
            inputs.get("output_path", str(input_path.with_stem(f"{input_path.stem}_cut")))
        )

        # -ss before -i (input seeking) + -t (duration) instead of -to (end
        # time) after -i — the reverse ordering is a confirmed bug class
        # (video-studio's CLAUDE-v2.md): combined with certain filters it can
        # silently truncate/zero out content past the seek point. This form
        # doesn't reproduce it, and is also faster (input seek vs. decode-then-
        # discard).
        cmd = ["ffmpeg", "-y"]
        if start_s:
            cmd.extend(["-ss", str(start_s)])
        cmd.extend(["-i", str(input_path)])
        if end_s is not None:
            cmd.extend(["-t", str(end_s - start_s)])
        if codec == "copy":
            cmd.extend(["-c", "copy"])
        else:
            cmd.extend(["-c:v", codec, "-c:a", "aac"])
        # moov-at-front — a browser <video> can't play a single frame of a
        # moov-at-end file until it's downloaded almost entirely (confirmed
        # real symptom: the live editor preview was permanently black). Free
        # here regardless of codec branch — same remux/encode, different
        # atom order.
        cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(output_path))

        self.run_command(cmd)

        return ToolResult(
            success=True,
            data={
                "operation": "cut",
                "input": str(input_path),
                "output": str(output_path),
                "start_seconds": start_s,
                "end_seconds": end_s,
            },
            artifacts=[str(output_path)],
        )

    def _speed(self, inputs: dict[str, Any]) -> ToolResult:
        input_path = Path(inputs["input_path"])
        if not input_path.exists():
            return ToolResult(success=False, error=f"Input not found: {input_path}")

        factor = inputs.get("speed_factor", 1.0)
        output_path = Path(
            inputs.get("output_path", str(input_path.with_stem(f"{input_path.stem}_speed")))
        )

        # Video: setpts adjusts presentation timestamps (inverse of speed)
        # Audio: atempo adjusts audio speed (must chain for >2x)
        video_filter = f"setpts={1.0/factor}*PTS"
        audio_filters = self._build_atempo_chain(factor)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-filter:v", video_filter,
            "-filter:a", audio_filters,
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac",
            "-movflags", "+faststart",
            str(output_path),
        ]

        self.run_command(cmd)

        return ToolResult(
            success=True,
            data={
                "operation": "speed",
                "input": str(input_path),
                "output": str(output_path),
                "speed_factor": factor,
            },
            artifacts=[str(output_path)],
        )

    def _concat(self, inputs: dict[str, Any]) -> ToolResult:
        segments = inputs.get("segments", [])
        if not segments:
            return ToolResult(success=False, error="No segments provided for concat")

        output_path = Path(inputs.get("output_path", "concat_output.mp4"))

        # First, cut each segment to a temp file if start/end are specified
        temp_files: list[Path] = []
        temp_dir = output_path.parent / ".concat_tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            for i, seg in enumerate(segments):
                seg_input = Path(seg["input_path"])
                if not seg_input.exists():
                    return ToolResult(success=False, error=f"Segment input not found: {seg_input}")

                seg_start = seg.get("start_seconds")
                seg_end = seg.get("end_seconds")

                if seg_start is not None or seg_end is not None:
                    temp_path = temp_dir / f"seg_{i:04d}{seg_input.suffix}"
                    # Segment boundaries here are arbitrary word-timestamp cut
                    # points (e.g. filler-word removal), not keyframe-aligned —
                    # -c copy can only cut at keyframes, so the concat result
                    # plays continuing audio over a FROZEN first frame until
                    # the next keyframe (reproduced on real WhatsApp jobs).
                    # Re-encode instead for frame-accurate cuts: -ss before -i
                    # + -t (not -to) after, per the same bug class as _cut
                    # above; crf 18 pins quality (the final concat re-encodes
                    # once more, so segments must not degrade on this pass).
                    #
                    # Fade duration bumped 30ms -> 60ms (confirmed real bug: a
                    # retake-removal join measured at only ~61ms of actual
                    # silence between the two spliced sentences — 30ms fades
                    # on each side leave almost no steady-state gap, reading as
                    # an abrupt/"choppy" jump rather than a clean cut, even
                    # though there's no technical corruption). 60ms per side
                    # is still short enough to avoid clipping adjacent words
                    # (paired with content_planner.py's FILLER_CUT_PAD_SECONDS)
                    # but gives noticeably more breathing room at the splice.
                    cmd = ["ffmpeg", "-y"]
                    if seg_start is not None:
                        cmd.extend(["-ss", str(seg_start)])
                    cmd.extend(["-i", str(seg_input)])
                    if seg_end is not None:
                        cmd.extend(["-t", f"{max(0.0, float(seg_end) - float(seg_start or 0)):.3f}"])
                    fade_d = 0.06
                    if seg_start is not None and seg_end is not None:
                        fade_out_st = max(0.0, float(seg_end) - float(seg_start) - fade_d)
                        cmd.extend(["-af", f"afade=t=in:d={fade_d},afade=t=out:st={fade_out_st:.3f}:d={fade_d}"])
                    else:
                        cmd.extend(["-af", f"afade=t=in:d={fade_d}"])
                    cmd.extend([
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                        "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k",
                        str(temp_path),
                    ])
                    self.run_command(cmd)
                    temp_files.append(temp_path)
                else:
                    temp_files.append(seg_input)

            # Write concat file list
            list_path = temp_dir / "concat_list.txt"
            with open(list_path, "w", encoding="utf-8") as f:
                for tf in temp_files:
                    # FFmpeg concat demuxer needs forward slashes and escaped quotes
                    safe_path = str(tf.resolve()).replace("\\", "/")
                    f.write(f"file '{safe_path}'\n")

            # Re-encode with a constant frame rate rather than -c copy —
            # concatenating segments cut at arbitrary (non-keyframe) points
            # can produce irregular timestamps that downstream frame-accurate
            # readers (e.g. Remotion's OffthreadVideo) fail to seek through
            # correctly past a certain point, silently holding the last
            # decodable frame while audio continues (confirmed symptom on a
            # real render). Same fix video-studio already validated for this
            # exact bug class (CLAUDE-v2.md): re-encode at constant frame rate
            # instead of stream-copying the concat.
            #
            # Hardcoded to 30 rather than probed from temp_files[0]: that file
            # is itself a segment just re-encoded from an arbitrary (non-
            # keyframe) -ss/-t cut, and ffprobing an already-irregularly-cut
            # source can read back a corrupted rate (e.g. a doubled/halved
            # value) — which then gets baked into -r for the WHOLE concat
            # output, silently multiplying or dropping frames across the
            # entire final video (this is very likely the source of the
            # "choppy cuts" reports: a filler-removal concat feeding a wrong
            # frame count into every downstream step). Confirmed and fixed the
            # same way earlier this session in tools/enhancement/face_enhance.py
            # and color_grade.py, which hit this exact failure mode when their
            # own _probe_fps() read 120fps off a 30fps source. The whole
            # pipeline assumes 30fps throughout (content_planner.py's FPS=30),
            # so there is nothing to gain by probing and real risk in doing so.
            # Fix C12（2026-07-17，真实生产复现）：GOP/关键帧间隔从没显式设过，
            # libx264 在没给 -g 时用自己的默认值（实测 250），37s/1119 帧的视频
            # 只有 4 个关键帧（帧 250/500/725/975），且帧 0 前面完全没有关键帧——
            # Remotion 的 Rust compositor 按帧随机 seek 时对着这种稀疏 GOP 直接
            # 报 "No frame found at position N"（独立复现：同一份 props 在
            # qa_stills 之后单独重跑照样炸，只是每次炸在不同帧——不是网上一直
            # 以为的"qa_stills 和整片渲染交接时的瞬时状态"，是 GOP 结构问题，
            # 换个随机 seek 目标自然换个炸点）。-g 30（30fps 下每秒一个关键帧）
            # 直接对齐 content_planner.py 的 FPS=30 假设。
            fps = 30.0
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_path),
                "-fps_mode", "cfr", "-r", str(fps), "-g", str(int(fps)),
                "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
                # moov-at-front — see this file's _cut() for the confirmed
                # real symptom this fixes.
                "-movflags", "+faststart",
                str(output_path),
            ]
            self.run_command(cmd)

            return ToolResult(
                success=True,
                data={
                    "operation": "concat",
                    "segment_count": len(segments),
                    "output": str(output_path),
                },
                artifacts=[str(output_path)],
            )
        finally:
            # Clean up temp segment files (but not the originals)
            for tf in temp_files:
                if tf.parent == temp_dir and tf.exists():
                    tf.unlink()
            if list_path.exists():
                list_path.unlink()
            if temp_dir.exists():
                try:
                    temp_dir.rmdir()
                except OSError:
                    pass

    @staticmethod
    def _build_atempo_chain(factor: float) -> str:
        """Build an atempo filter chain. atempo only accepts [0.5, 100.0]."""
        if factor <= 0:
            factor = 1.0
        # Chain multiple atempo filters for extreme values
        filters = []
        remaining = factor
        while remaining > 100.0:
            filters.append("atempo=100.0")
            remaining /= 100.0
        while remaining < 0.5:
            filters.append("atempo=0.5")
            remaining /= 0.5
        filters.append(f"atempo={remaining:.4f}")
        return ",".join(filters)

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


def _safe_transcribe(src: str, workdir: Path, model_size: str):
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

    config = get_config()
    if config.transcribe_provider == "elevenlabs" and config.elevenlabs_api_key:
        t = _transcribe_elevenlabs(src, config.elevenlabs_api_key)
        if t.success:
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
        if ref is None or start is None or end is None or end <= start:
            continue
        matches = sorted(assets_dir.glob(f"broll_{ref}.*"))
        if not matches:
            logger.warning(f"  insert_broll: 找不到资产 broll_{ref}.*，跳过")
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

    orientation = str(op.get("orientation") or "auto").lower()
    if orientation not in ("portrait", "landscape"):
        orientation = "landscape" if any_landscape else "portrait"
    out_w, out_h = (1920, 1080) if orientation == "landscape" else (1080, 1920)

    out = workdir / "_op_insert_broll.mp4"
    _composite_broll(src, resolved, out, out_w, out_h,
                     ins_h=round(out_h * 0.28), margin=round(out_w * 0.03))
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
            fc.append(f"[m{i}][insv{i}]overlay=W-w-{margin}:H-h-{margin}:enable='between(t,{s},{e})'[c{i}]")
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
    # interpolate() needs strictly increasing frames — drop any keyframe that
    # would violate that (e.g. two takeovers closer together than the fades).
    monotonic: list[dict] = []
    for k in opacity:
        if monotonic and k["frame"] <= monotonic[-1]["frame"]:
            continue
        monotonic.append(k)
    return scenes, monotonic


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


def _run_enhancement_chain(src: str, workdir: Path) -> str:
    with _ENHANCE_SLOTS:  # face/color/audio 增强都是 ffmpeg/模型重活，跨任务串行
        return _run_enhancement_chain_inner(src, workdir)


def _run_enhancement_chain_inner(src: str, workdir: Path) -> str:
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


# Fix C5（2026-07-16）：跟 content_planner.plan_content 的 criterion loop 用同一个
# 有界重试次数——用户明确要求过循环要"KEEP LOOPING AND EXITING WHEN YOU'VE
# FULFILLED THE CRITERION"，且不是只对某一条视频生效。之前 props_lint 这一层
# 只重试一次，见 _op_apply_style 里那段旧注释。
_PROPS_LINT_MAX_ATTEMPTS = 3

# 参与"丰富度"计分的 props 字段——每一项都是真正的动画/图形，不是纯文字。
_RICHNESS_FIELDS = (
    "dataCards", "gauges", "countdowns", "calendarEvents", "beforeAfter",
    "stepLists", "topicCards", "cornerCards", "quotes",
)


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
    public_video_rel = f"jobs/{job_slug}/source.mp4"
    public_video_abs = remotion_dir / "public" / "jobs" / job_slug / "source.mp4"
    public_video_abs.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, public_video_abs)

    # 人脸裁剪校准是确定性的（同一段视频每次算出来的结果一样），跟内容规划反馈
    # 无关，只需要在下面的重试闭包外面算一次——重试它只会得到一模一样的值。
    speaker_object_position = op.get("speaker_object_position") or calibrate_speaker_object_position(src, workdir)
    props_path = workdir / "_op_apply_style_props.json"

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
        else:
            logger.info("  apply_style: 内容规划中（章节 + 数据展示分析）...")
            content_plan = plan_content(segments, duration, feedback=feedback, word_timestamps=word_timestamps)
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
            logger.info(
                f"  apply_style: 规划出 {len(chapters)} 个章节、{len(data_cards)} 个数据卡、"
                f"{len(gauges)} 个仪表盘、{len(countdowns)} 个倒计时、{len(calendar_events)} 个日历、"
                f"{len(before_after)} 个前后对比、{len(plan_quotes)} 条金句"
            )

        scenes, speaker_opacity = _mode_schedule_to_scenes(mode_schedule)

        props: dict[str, Any] = {
            "videoSrc": public_video_rel,
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
            for _items in (data_cards, gauges, countdowns, calendar_events,
                           before_after, plan_quotes, plan_pills, plan_step_lists, plan_topic_cards):
                _floor_shift_graphics(_items, _mount_floor)
            _floor_shift_zone_headers(plan_zone_headers, _mount_floor)

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
                              plan_step_lists, plan_topic_cards):
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
            qr_rel = f"jobs/{job_slug}/qr.png"
            qr_abs = remotion_dir / "public" / qr_rel
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
                    "qrSrc": qr_rel,
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
        elif len(candidate_findings) < len(best_findings):
            logger.warning(
                f"  apply_style: props_lint 第 {attempt}/{_PROPS_LINT_MAX_ATTEMPTS} 轮的重规划"
                f"findings 更少({len(candidate_findings)} < {len(best_findings)})，但丰富度从 "
                f"{best_richness} 降到 {candidate_richness}——拒绝采用，保留内容更丰富的版本"
            )
        attempt += 1
    if best_findings:
        logger.warning(
            f"  apply_style: props_lint {_PROPS_LINT_MAX_ATTEMPTS} 轮后仍有 "
            f"{len(best_findings)} 处问题，交付问题最少的一版（不阻断渲染）: "
            f"{[f['check'] for f in best_findings]}"
        )
    elif attempt > 1:
        logger.info(f"  apply_style: props_lint 全部通过（第 {attempt - 1}/{_PROPS_LINT_MAX_ATTEMPTS} 轮重试后）")
    props = best_props
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
        vision_findings = (qa_result.get("vision_review") or {}).get("findings") or []
        major = [f for f in vision_findings if f.get("severity") == "high"]
        if major:
            feedback = "; ".join(f"still #{f.get('frame_index')}: {f.get('issue', '')}" for f in major)[:500]
            logger.warning(f"  apply_style: 视觉复审发现问题，重新规划一次: {feedback}")
            props = _build(feedback=feedback)
            qa_result = run_props_qa(props, props_path, remotion_dir, workdir / "qa_stills")
            vision_findings = (qa_result.get("vision_review") or {}).get("findings") or []
            major = [f for f in vision_findings if f.get("severity") == "high"]
            if major:
                raise RuntimeError(f"apply_style: 视觉复审重试后仍发现问题，触发降级交付: {major}")

    out = workdir / "_op_styled.mp4"
    npx_bin = shutil.which("npx") or "npx"  # Windows: subprocess needs the resolved npx.cmd, plain "npx" raises WinError 2
    from .remotion_bundle import ensure_remotion_bundle
    bundle = ensure_remotion_bundle(remotion_dir, job_slug=job_slug)
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
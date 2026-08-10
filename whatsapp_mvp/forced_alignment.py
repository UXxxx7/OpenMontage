# WhatsApp MVP - 强制对齐（可选增强，2026-07-24）
#
# 真实事故根因（同一天调查过两次）：faster-whisper 自己给的词级时间戳是靠
# attention 权重插值出来的，精度差——真实案例里单词 "Cloud"（"Cloud Code" 的
# 一部分）被报了 1.56 秒长，实际发音只有 0.5 秒左右。这类漂移会让下游任何
# "按时长判断异常"的逻辑（content_planner._flag_unaccounted_audio）、以及
# 字幕/卡拉OK高亮的视觉体验都跟着遭殃。
#
# WhisperX（github.com/m-bain/whisperX，BSD-2-Clause）在 faster-whisper 转写
# 结果之上加一层 wav2vec2 强制对齐（CTC），把"这段文字在音频里具体是哪一段"
# 重新算一遍，实测把 "Cloud" 的时长从 1.56s 修正到 0.52s；中文按字对齐，同样
# 实测过（重疾险的重要性...）。注意：强制对齐只修时间戳，不修文字本身——
# 转写文字错了（同音字/专有名词听错）它不会跟着改，那是 hotwords/已知文案
# 纠错（croll_script.txt）该管的事，两者互补，不是互相替代。
#
# 可选依赖（uv sync --extra align）：装了才会真正生效，没装/加载失败一律
# 优雅退回 faster-whisper 自己的词级时间戳，不阻断转写流程。

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

_align_cache: dict[str, Any] = {}
_align_cache_lock = threading.Lock()


def _get_align_model(language_code: str):
    """按语言懒加载 + 常驻缓存对齐模型——中文模型光加载就要 ~2 分钟（实测
    118.6s），绝不能每个 job 都重新加载一次；跨请求复用同一个内存中的模型。
    """
    with _align_cache_lock:
        cached = _align_cache.get(language_code)
        if cached is not None:
            return cached
        import whisperx

        model_a, metadata = whisperx.load_align_model(language_code=language_code, device="cpu")
        _align_cache[language_code] = (model_a, metadata)
        return model_a, metadata


def realign_word_timestamps(
    segments: list[dict], audio_path: str, language: str
) -> Optional[list[dict]]:
    """用 WhisperX 的强制对齐重新计算词级时间戳，失败返回 None（调用方按
    "这次没有更精确的时间戳可用"处理，沿用 faster-whisper 自己的结果，不
    抛异常、不阻断转写）。

    segments: faster-whisper 的分段结果（只需要 start/end/text）。
    返回：跟 faster-whisper word_timestamps 同形状的列表
    （word/start/end/probability），probability 取自对齐模型自己的置信度
    （whisperx 叫 "score"），下游 _flag_unaccounted_audio 等代码不用区分
    数据来源。
    """
    try:
        import whisperx
    except ImportError:
        logger.info("forced_alignment: whisperx 未安装（可选依赖），跳过强制对齐，沿用原始词级时间戳")
        return None

    try:
        audio = whisperx.load_audio(audio_path)
        model_a, metadata = _get_align_model(language)
        transcript = [
            {"start": s["start"], "end": s["end"], "text": s["text"]}
            for s in segments if s.get("text", "").strip()
        ]
        if not transcript:
            return None
        result = whisperx.align(transcript, model_a, metadata, audio, "cpu", return_char_alignments=False)
        words = result.get("word_segments") or []
        if not words:
            logger.warning("forced_alignment: 强制对齐没有产出词级结果，沿用原始词级时间戳")
            return None
        return [
            {
                "word": w["word"],
                "start": round(float(w["start"]), 3),
                "end": round(float(w["end"]), 3),
                "probability": round(float(w.get("score", 1.0)), 3),
            }
            for w in words
            if w.get("start") is not None and w.get("end") is not None
        ]
    except Exception as e:
        # 语言不支持 / 模型下载失败 / 音频格式问题等任何原因，都只降级不阻断——
        # 强制对齐是"锦上添花"的精度提升，不是转写能不能成功的前提。
        logger.warning(f"forced_alignment: 强制对齐失败，沿用原始词级时间戳: {e}")
        return None

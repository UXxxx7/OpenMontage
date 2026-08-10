# WhatsApp MVP - Clip Factory: one long recording -> several ranked, ready-to-post clips
#
# 纯函数模块：transcript segments in -> ranked candidates out，不碰 DB/Job/
# WhatsApp。跟今天 social_caption.py 走的是同一套纪律——先在这里把选片质量
# 调好、人工审过，再往上接数据库/管线/消息层（见 verify_clip_selection.py）。
#
# OpenMontage 自己的 pipeline_defs/clip-factory.yaml 定义了一套 7 阶段的
# agent 执行流程（idea/script/scene_plan/assets/edit/compose/publish，配
# 对应的 skills/pipelines/clip-factory/*.md）——那套是写给交互式 coding agent
# 一步步读着执行的，这个代码库里没有能通用执行 YAML+skill 的东西（跟
# talking-head 管线一样，真正跑起来的是手写 Python）。这个模块把 script
# 阶段（selection/scoring 那部分）的精神翻译成一次真实的 LLM 调用 + 硬性
# Python 兜底，不是照抄 YAML。

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .config import get_config
from .content_planner import _call_llm_json

logger = logging.getLogger(__name__)

# 产量目标表——直接照抄 pipeline_defs/clip-factory.yaml 的 idea-director 阶段
# 给的时长分档指引。8 分钟以下不值得跑 clip-factory（找不出几个够格的独立片段），
# 调用方应该退回 talking-head 单条剪辑。
YIELD_TABLE: list[tuple[float, float, int, int]] = [
    (0, 8 * 60, 0, 0),
    (8 * 60, 30 * 60, 3, 6),
    (30 * 60, 60 * 60, 5, 10),
    (60 * 60, float("inf"), 8, 15),
]

# Python 侧硬性下限，独立于 LLM 打分——哪怕模型评分很高，时长不在这个区间
# 内也直接丢弃（太短放不下一个完整意思，太长不是"短视频"了）。
MIN_CLIP_SECONDS = 15
MAX_CLIP_SECONDS = 180

# 60k 字符——比 content_planner 那边的 12k 大得多：那边是"看一遍抓章节大意"，
# 这里是真的要在一份 15-90+ 分钟的完整转写里挖素材，压太狠会让后半段内容
# 直接不可见。这是目前整个功能里最没有真实数据验证过的参数（见
# verify_clip_selection.py 的用法说明）——如果真实长视频测出来不够用，
# 加分段/多轮调用是逃生舱口，不是现在就要预先搭好的东西。
_MAX_TRANSCRIPT_CHARS = 60_000


def yield_target(duration_seconds: float) -> tuple[int, int]:
    """按源视频时长返回 (最少要几条, 最多几条)。纯函数，不依赖任何外部状态。"""
    for lo, hi, min_c, max_c in YIELD_TABLE:
        if lo <= duration_seconds < hi:
            return (min_c, max_c)
    return (8, 15)  # 理论上不会走到这里（YIELD_TABLE 最后一档上界是 inf）


def _build_numbered_transcript(segments: list[dict]) -> tuple[str, dict[int, dict]]:
    """把 segments 编号列成文本块给模型看，同时留一份 编号->segment 的查找表。

    候选片段只能引用这里的编号，绝不接受模型自己编的时间戳——segments 本身
    已经是转写时按停顿切好的边界，引用编号天然保证不会切到词中间，不需要
    额外的"贴到最近词边界"逻辑。
    """
    lines = []
    lookup: dict[int, dict] = {}
    for i, seg in enumerate(segments):
        lookup[i] = seg
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"[{i}] {text}")
    text = "\n".join(lines)
    if len(text) > _MAX_TRANSCRIPT_CHARS:
        truncated = text[:_MAX_TRANSCRIPT_CHARS]
        truncated = truncated.rsplit("\n", 1)[0]  # 不要在半行截断
        text = truncated + "\n[... transcript truncated here — only the portion above was considered ...]"
        logger.warning(
            f"clip_factory: 转写 {len(text)}+ 字符超过 {_MAX_TRANSCRIPT_CHARS} 上限，"
            "已截断——超出部分的候选片段会漏掉"
        )
    return text, lookup


_SELECTION_SYSTEM = """You are selecting the best short-clip candidates from a long recording's \
transcript, for a "clip factory" that repurposes one long recording into several ranked, \
ready-to-post short clips for social media.

You'll be given the source duration, a target clip count range, and the full transcript as \
NUMBERED segments (each segment is one spoken utterance, already boundary-detected at natural \
pauses). Reference candidates ONLY by these segment numbers — never invent a timestamp not tied \
to a real segment in the list.

Do the following:

1. Classify source_type: one of "webinar", "interview", "panel", "keynote", "stream", \
"customer_story", "other".

2. Propose candidate clips. Each spans from one segment id to another (start_segment_id, \
end_segment_id, both inclusive, referencing the numbered list given). A good clip is usually \
3-15 consecutive segments — long enough to deliver a complete thought, short enough to stay tight.

3. Score each candidate 0-10 on five axes:
   - hook: does the opening line grab attention in the first couple of seconds?
   - coherence: does it flow as one clean thought, no jarring topic jumps inside the clip?
   - value: does it deliver a real insight, story, or payoff — not just filler or a lead-in?
   - energy: is the delivery/pacing lively enough to hold attention?
   - platform_fit: would this work as a standalone short-form video, not a fragment that needs \
the rest of the source to make sense?

4. Apply a standalone test: could someone who has NOT watched the rest of the source understand \
and enjoy this clip on its own? Set standalone_ok to false if it opens on an unresolved pronoun \
("that's why I told him..."), references something explained earlier that isn't included in the \
clip, or has no clear payoff within the clip's own boundaries. If false, give a one-line \
standalone_issue explaining why.

5. Assign clip_family from exactly this vocabulary: "hook" (a provocative opening claim), \
"insight" (a specific, actionable point), "story" (an anecdote/narrative), "proof" (data/example/ \
evidence), "opinion" (a strong stated position).

6. Maintain diversity: don't cluster every candidate in one section of the source, don't repeat \
the same clip_family more than necessary, don't pick multiple candidates covering the same point.

7. The target clip count range given is a hard ceiling, not a quota to fill — return fewer than \
the minimum if the source genuinely doesn't have enough strong, standalone moments. Do not \
inflate the count to hit a round number. If you return fewer than the minimum, explain why in \
below_target_reason.

8. Every candidate must be traceable to segments you were actually given — never invent content, \
never reference a segment id outside the numbered list.

Output ONLY valid JSON, no markdown, no explanation:
{"source_type": "...", "candidates": [{"clip_family": "...", "start_segment_id": 0, \
"end_segment_id": 5, "hook_text": "the exact or near-exact opening line", "scores": {"hook": 0-10, \
"coherence": 0-10, "value": 0-10, "energy": 0-10, "platform_fit": 0-10}, "standalone_ok": true, \
"standalone_issue": null}, ...], "below_target_reason": null}"""


def select_clips(segments: list[dict], duration_seconds: float, *,
                 workdir: Optional[Path] = None) -> dict:
    """转写 -> 候选片段（未经排序/裁剪，见 rank_and_trim）。

    返回 {"source_type", "candidates", "below_target_reason", "min_clips", "max_clips"}。
    候选片段的 start_seconds/end_seconds 由这里根据 segment id 权威解析出来，
    绝不信任模型自己回显的数字——跟这个代码库其余地方对 LLM 输出数字的
    不信任态度一致（content_planner 的 milestone/timeline 等字段同理）。

    LLM 不可用/调用失败/时长不够 8 分钟：返回 candidates=[]，不抛异常，
    调用方按"这条没成/不适用"处理。
    """
    min_clips, max_clips = yield_target(duration_seconds)
    if max_clips == 0:
        return {"source_type": None, "candidates": [], "min_clips": 0, "max_clips": 0,
                "below_target_reason": "source shorter than 8 minutes — not viable for clip-factory"}

    numbered_text, lookup = _build_numbered_transcript(segments)
    if not numbered_text.strip():
        return {"source_type": None, "candidates": [], "min_clips": min_clips, "max_clips": max_clips,
                "below_target_reason": "no transcribable speech in source"}

    user_message = (
        f"Source duration: {duration_seconds:.0f} seconds (~{duration_seconds / 60:.1f} minutes).\n"
        f"Target clip count: {min_clips}-{max_clips} clips.\n\n"
        f"Numbered transcript segments:\n{numbered_text}"
    )
    config = get_config()
    raw = _call_llm_json("clip-factory 候选片段筛选", _SELECTION_SYSTEM, user_message,
                         temperature=0.2, model=config.llm_model_long_output, workdir=workdir)
    if not raw:
        return {"source_type": None, "candidates": [], "min_clips": min_clips, "max_clips": max_clips,
                "below_target_reason": "LLM unavailable or call failed"}

    resolved: list[dict] = []
    for cand in raw.get("candidates") or []:
        try:
            start_id = int(cand["start_segment_id"])
            end_id = int(cand["end_segment_id"])
            start_seg = lookup[start_id]
            end_seg = lookup[max(start_id, end_id)]
        except (KeyError, ValueError, TypeError):
            logger.warning(f"clip_factory: 候选片段引用了无效的 segment id，丢弃: {cand}")
            continue
        cand = dict(cand)
        cand["start_seconds"] = float(start_seg["start"])
        cand["end_seconds"] = float(end_seg["end"])
        resolved.append(cand)

    return {
        "source_type": raw.get("source_type"),
        "candidates": resolved,
        "below_target_reason": raw.get("below_target_reason"),
        "min_clips": min_clips,
        "max_clips": max_clips,
    }


def rank_and_trim(selection: dict) -> list[dict]:
    """Python 侧硬性把关，独立于模型是否听话：丢掉 standalone_ok=False 和时长
    超出 [MIN_CLIP_SECONDS, MAX_CLIP_SECONDS] 的候选，按总分排序，裁到
    max_clips，赋最终 rank（1 = 最强）。"""
    max_clips = selection.get("max_clips", 15)
    kept: list[dict] = []
    for cand in selection.get("candidates") or []:
        if not cand.get("standalone_ok", False):
            continue
        duration = cand["end_seconds"] - cand["start_seconds"]
        if duration < MIN_CLIP_SECONDS or duration > MAX_CLIP_SECONDS:
            continue
        scores = cand.get("scores") or {}
        total = sum(float(scores.get(k) or 0) for k in
                   ("hook", "coherence", "value", "energy", "platform_fit"))
        cand = dict(cand)
        cand["score_total"] = total
        cand["duration_seconds"] = duration
        kept.append(cand)

    kept.sort(key=lambda c: c["score_total"], reverse=True)
    kept = kept[:max_clips]
    for i, cand in enumerate(kept, 1):
        cand["rank"] = i
    return kept

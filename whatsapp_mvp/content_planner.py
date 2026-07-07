# WhatsApp MVP - Content Planner
# 补上 compose-director.md 要求的"内容判断"步骤：读转写稿，判断该分几个章节、
# 哪些数字值得做成 count-up 数据卡——而不是像 apply_style 最初版本那样直接拿
# 空的 chapters/dataCards 去渲染。对应该文档的 "Data Display Analysis
# (MANDATORY pre-build step)" 和 scene-director 的章节/beat 规划工作，这里是
# 第一次把这步做成真代码，而不是每次靠人工读一遍转写稿。
#
# 输出字段名对齐 contract②（render_props.schema.json，P3 owns）：chapters 用
# atFrame（不是 at），数据卡容器用 data_cards（_op_apply_style 里映射成
# props["dataCards"]）。
#
# 范围：目前 InfoCard 组件只支持 count-up 数字卡（没有日历/仪表盘组件），所以
# 这里只让 LLM 判断"哪些数字值得强调"，不判断日期/百分比该用日历还是仪表盘——
# 那些图形组件还没做，判断了也渲染不出来。

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .llm_client import call_llm_chat

logger = logging.getLogger(__name__)

FPS = 30
# compose-director.md 的 beat anchoring 规则：卡片在关键词被说出之前 50 帧上场。
MOUNT_LEAD_FRAMES = 50
# 数据卡展示完之后，Workflow 模式至少再停留这么久再切回 Dominant，避免切换过快。
HOLD_AFTER_LAST_ROW_FRAMES = 90

SYSTEM_PROMPT = """You analyze a talking-head video's transcript and produce a content plan for the video's chapter markers and any data-worthy numbers, following these rules (from the project's compose-director.md style codex).

**This video could be about absolutely anything** — cooking, fitness, finance, a product review, a story, a tutorial, a rant. Do not default to any one domain or topic. Every chapter label, and whether there are any data points at all, must come ONLY from what THIS specific transcript actually says — never reuse a pattern, label, or topic from any other video you've seen. A video with no notable numbers should get an empty data_points array; that is a completely normal, common outcome, not a failure.

1. Chapters: split the video into 2-5 chapters based on the ACTUAL topic shifts in THIS transcript. Each chapter needs a short 1-2 word UPPERCASE label that names what THAT PART of THIS video is actually about (e.g. a cooking video might get "INGREDIENTS"/"TECHNIQUE"/"PLATING", a workout video might get "WARMUP"/"SETS"/"COOLDOWN", a story might get "SETUP"/"TWIST"/"ENDING" — these are just illustrations of the LABEL STYLE, not topics to look for) and the second it starts.
2. Data points ("Data Display Analysis" rule): scan the transcript for hard numbers worth a visual callout — of ANY kind: money, reps, distances, times, scores, ratings, percentages, quantities, counts, dates — whatever this video actually mentions, not specifically money. Only flag a number if it's genuinely the point of that sentence (a dramatic reveal, a before/after comparison, a key stat) — most videos have zero to two such moments, not one per sentence. A number mentioned in passing that isn't the point of the sentence is NOT a data point.
3. For each data point, group numbers that belong together into ONE card (e.g. a before-value and an after-value of the same metric belong in the SAME card as two rows, not two separate cards) with a short card title, and for each row give: a short UPPERCASE label, the numeric value, the exact second that number is spoken, and a "tone" (accent=neutral highlight, good=positive, bad=negative/cost, normal=no emphasis). Also give prefix (e.g. "$", "" if none), divideBy (e.g. 1000000 to show "1.5" for 1,500,000; omit or use 1 if the raw number is already short), decimals, and unit (e.g. "M", "K", "%", "reps", "kg" — whatever unit this number actually is) so the displayed number is compact and readable in its own domain — never show a raw unformatted number.
4. If the transcript has no genuinely dramatic/comparison-worthy numbers, return an empty data_points array. Do not invent a card for an ordinary number just to fill the response, and do not force financial/before-after framing onto content that isn't about that.

Output ONLY valid JSON matching this shape, no markdown, no prose:
{
  "chapters": [{"at_seconds": 0, "label": "..."}],
  "data_points": [
    {
      "title": "...",
      "rows": [
        {"label": "...", "seconds": 12.3, "value": 42, "prefix": "", "divideBy": 1, "decimals": 0, "unit": "", "tone": "normal"}
      ]
    }
  ]
}"""


def _build_transcript_text(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        lines.append(f"[{seg['start']:.1f}s] {seg['text'].strip()}")
    return "\n".join(lines)


def plan_content(segments: list[dict], duration: float) -> dict[str, Any]:
    """转写分段 -> 章节 + 数据卡计划（已经是 frame 单位，可以直接喂给 XiaojinEditorial）。

    LLM 调用失败或没配 key 时，返回空计划——内容判断本来就是锦上添花，不应该
    因为它失败就搞垮整条剪辑流程。
    """
    empty = {"chapters": [], "data_cards": [], "mode_schedule": [{"frame": 0, "mode": "dominant"}]}

    transcript_text = _build_transcript_text(segments)
    user_message = f"Video duration: {duration:.1f}s\n\nTranscript:\n{transcript_text}"

    content = call_llm_chat(SYSTEM_PROMPT, user_message, temperature=0.2)
    if content is None:
        logger.info("content_planner: 没配 LLM 或调用失败，跳过内容规划（返回空外壳计划）")
        return empty

    try:
        raw = json.loads(content)
    except Exception as e:
        logger.warning(f"content_planner: 解析 LLM 输出失败，跳过内容规划: {e}")
        return empty

    return _to_frame_plan(raw, duration)


def _to_frame_plan(raw: dict, duration: float) -> dict[str, Any]:
    chapters = []
    for c in raw.get("chapters") or []:
        try:
            chapters.append({"atFrame": max(0, round(float(c["at_seconds"]) * FPS)), "label": str(c["label"])[:20]})
        except (KeyError, TypeError, ValueError):
            continue
    chapters.sort(key=lambda c: c["atFrame"])

    data_cards = []
    workflow_ranges: list[tuple[int, int]] = []

    for dp in raw.get("data_points") or []:
        rows_in = dp.get("rows") or []
        if not rows_in:
            continue
        try:
            row_seconds = [float(r["seconds"]) for r in rows_in]
        except (KeyError, TypeError, ValueError):
            continue

        card_mount_frame = max(0, round(min(row_seconds) * FPS) - MOUNT_LEAD_FRAMES)
        rows = []
        for r, sec in zip(rows_in, row_seconds):
            value = _num(r.get("value"))
            if value is None:
                continue  # InfoCard rows are count-up only; skip anything without a real number
            row_frame = round(sec * FPS)
            row: dict[str, Any] = {
                "label": str(r.get("label", ""))[:24],
                "value": value,
                "tone": r.get("tone") if r.get("tone") in ("accent", "good", "bad", "normal") else "normal",
                "mountOffset": max(0, row_frame - card_mount_frame),
            }
            if r.get("prefix"):
                row["prefix"] = r["prefix"]
            if _num(r.get("divideBy")):
                row["divideBy"] = _num(r.get("divideBy"))
            if r.get("decimals") is not None:
                row["decimals"] = r["decimals"]
            if r.get("unit"):
                row["unit"] = r["unit"]
            rows.append(row)
        if not rows:
            continue
        data_cards.append({
            "title": str(dp.get("title", ""))[:40],
            "x": 80, "y": 900, "width": 920,
            "mountFrame": card_mount_frame,
            "rows": rows,
        })
        last_row_frame = round(max(row_seconds) * FPS)
        workflow_ranges.append((card_mount_frame, last_row_frame + HOLD_AFTER_LAST_ROW_FRAMES))

    mode_schedule = [{"frame": 0, "mode": "dominant"}]
    for start, end in sorted(workflow_ranges):
        mode_schedule.append({"frame": max(1, start - 10), "mode": "workflow"})
        if round(end) < round(duration * FPS):
            mode_schedule.append({"frame": end, "mode": "dominant"})
    # interpolate() requires strictly increasing frame numbers.
    dedup: list[dict] = []
    for entry in mode_schedule:
        if dedup and entry["frame"] <= dedup[-1]["frame"]:
            continue
        dedup.append(entry)

    return {"chapters": chapters, "data_cards": data_cards, "mode_schedule": dedup}


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Semantic filler/retake removal — the video-use / edit-director.md approach:
# "No hand-tuned scoring function (highlight score = 0.4*laughter + ...).
# That's an anti-pattern — it overfits to whatever you tuned it on and breaks
# silently on new footage. Instead: an LLM reads the transcript and *judges*
# which moments are cut-worthy, the same way a human editor would."
#
# This is deliberately a different mechanism from remove_silences
# (SilenceCutter), which only detects actual dead air/pauses — it can't
# catch a voiced filler word ("um", "uh", "like") or a false start/retake
# that has no silence gap around it at all.
# ---------------------------------------------------------------------------

FILLER_SYSTEM_PROMPT = """You are a video editor. You are given a talking-head video's transcript as a numbered list of words with timestamps. Decide which words to CUT to make the delivery clean and tight, the way a human editor listening to the raw take would — not a mechanical rule.

Cut:
- Filler words used as verbal padding: "um", "uh", "like" (when not meaningful), "you know", "I mean" (when just a verbal tic), false starts ("we— we should", cut the abandoned "we—").
- Stutters and self-corrections: if the speaker restarts a phrase or repeats themselves to get it right, cut the earlier failed attempt(s) and keep only the final clean version.
- Do NOT cut: meaningful words, correct sentences, or pauses that are just natural speech rhythm (that is a separate, silence-only cleanup step — you are only removing WORDS the speaker didn't mean to leave in, not silence).
- Be conservative: if you're not sure a word is filler, keep it. A clean-but-untouched take is better than an over-aggressive cut that removes real content.
- Cuts must be at word boundaries — you can only cut whole words from the numbered list, never partial words.

Output ONLY valid JSON, no markdown, no prose:
{
  "cut_word_indices": [12, 13, 45]
}
If nothing needs cutting, return {"cut_word_indices": []}."""


def plan_filler_removal(words: list[dict], duration: float) -> list[dict]:
    """转写词级时间戳 -> 保留片段列表（喂给 VideoTrimmer 的 concat 操作）。

    跟 remove_silences（纯静音检测）是两码事：这里判断的是"这个词是不是口误/
    语气词/重录的失败尝试"，静音检测测不到有声的"呃""嗯"，也测不到中间没停顿
    的重录。没配 LLM 或调用失败时返回 None（调用方应该跳过这步，不要因为这个
    可选的精修步骤失败就搞垮整条剪辑流程）。
    """
    if not words:
        logger.info("content_planner: 没有词级时间戳，跳过口误检测")
        return []

    numbered = "\n".join(f"{i}: {w['word']} [{w['start']:.2f}-{w['end']:.2f}]" for i, w in enumerate(words))

    content = call_llm_chat(FILLER_SYSTEM_PROMPT, numbered, temperature=0.1)
    if content is None:
        logger.info("content_planner: 没配 LLM 或调用失败，跳过口误检测")
        return []

    try:
        raw = json.loads(content)
        cut_indices = {int(i) for i in (raw.get("cut_word_indices") or []) if 0 <= int(i) < len(words)}
    except Exception as e:
        logger.warning(f"content_planner: 口误检测结果解析失败，跳过: {e}")
        return []

    if not cut_indices:
        return []

    # Build keep-ranges from the words NOT in cut_indices, merging adjacent
    # kept words into contiguous ranges (word boundaries only, per the rule).
    keep_ranges: list[dict] = []
    cur_start: Optional[float] = None
    cur_end: Optional[float] = None
    for i, w in enumerate(words):
        if i in cut_indices:
            if cur_start is not None:
                keep_ranges.append({"start_seconds": cur_start, "end_seconds": cur_end})
                cur_start = None
            continue
        if cur_start is None:
            cur_start = w["start"]
        cur_end = w["end"]
    if cur_start is not None:
        keep_ranges.append({"start_seconds": cur_start, "end_seconds": cur_end})

    logger.info(f"content_planner: 口误检测 -> 剪掉 {len(cut_indices)} 个词，保留 {len(keep_ranges)} 段")
    return keep_ranges

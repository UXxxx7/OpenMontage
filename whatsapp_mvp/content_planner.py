# WhatsApp MVP - Content Planner
# 补上 compose-director.md 要求的"内容判断"步骤：读转写稿，判断该分几个章节、
# 哪些数字/日期/风险值得做成图形——而不是像 apply_style 最初版本那样直接拿
# 空的 chapters/dataCards 去渲染。对应该文档的 "Data Display Analysis
# (MANDATORY pre-build step)" 和 scene-director 的章节/beat 规划工作。
#
# 输出字段名对齐 contract②（render_props.schema.json，P3 owns）：chapters 用
# atFrame（不是 at），四种图形分别映射成 props["dataCards"] / props["gauges"] /
# props["countdowns"] / props["calendarEvents"]。
#
# 完整版 Data Display Analysis：不再只判断"哪些数字该 count-up"，而是按
# compose-director.md 的表格把每个数据点分类到该用的图形——倒计时用环形进度、
# 具体日期用日历、风险后果用仪表盘、其余数字用 count-up 卡——而不是都塞进
# 同一种卡片里。QR/联系方式不在这里判断：那是"是否提供了真实联系方式"的问题，
# 不是从文字内容里推断的语义判断，由 _op_apply_style 按 op 参数决定。

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Optional

from .llm_client import call_llm_chat

logger = logging.getLogger(__name__)

FPS = 30
# compose-director.md 的 beat anchoring 规则：卡片在关键词被说出之前 50 帧上场。
MOUNT_LEAD_FRAMES = 50
# 前一个数据点结束后至少留这么多帧再上场下一个——避免连续报数据时后一个卡片
# 提前 50 帧的偏移把它顶到前一个卡片还没结束、或者前一个数据点自己的话还没
# 说完的时刻（CLAUDE-v2.md 记录过这个 bug：flat -50 offset 会让下一阶段的数字
# 在上一阶段的话说到一半时就跳出来）。
MIN_GAP_AFTER_PREVIOUS_FRAMES = 10
# 图形展示完之后，Workflow 模式至少再停留这么久再切回 Dominant，避免切换过快。
HOLD_AFTER_LAST_ROW_FRAMES = 90
# 仪表盘/倒计时自身的入场+动画时长（对应组件默认值），决定它们各自的"活跃窗口"。
GAUGE_ANIMATION_FRAMES = 20 + 50  # fillDelayFrames + fillDurationFrames 默认值
COUNTDOWN_ANIMATION_FRAMES = 40  # revealFrames 默认值
CALENDAR_DISPLAY_FRAMES = 150  # 日历没有"动画完成"节点，给一个固定停留时长

SYSTEM_PROMPT = """You analyze a talking-head video's transcript and produce a content plan for the video's chapter markers and any data-worthy moments, following these rules (from the project's compose-director.md style codex, "Data Display Analysis" section).

**This video could be about absolutely anything** — cooking, fitness, finance, a product review, a story, a tutorial, a rant. Do not default to any one domain or topic. Every chapter label, and whether there are any data points at all, must come ONLY from what THIS specific transcript actually says — never reuse a pattern, label, or topic from any other video you've seen. A video with no notable moments should get an empty data_points array; that is a completely normal, common outcome, not a failure.

1. Chapters: split the video into 2-5 chapters based on the ACTUAL topic shifts in THIS transcript. Each chapter needs a short 1-2 word UPPERCASE label that names what THAT PART of THIS video is actually about (e.g. a cooking video might get "INGREDIENTS"/"TECHNIQUE"/"PLATING", a workout video might get "WARMUP"/"SETS"/"COOLDOWN", a story might get "SETUP"/"TWIST"/"ENDING" — these are just illustrations of the LABEL STYLE, not topics to look for) and the second it starts.
2. Data Display Analysis: scan the transcript for hard data points worth a visual callout, of ANY kind this video actually mentions — money, reps, distances, times, scores, percentages, quantities, counts, dates, countdowns, risks. Only flag a moment if it's genuinely the point of that sentence (a dramatic reveal, a before/after comparison, a key stat, a warning) — most videos have zero to two such moments, not one per sentence. A number mentioned in passing that isn't the point of the sentence is NOT a data point.
3. For each flagged moment, classify it into exactly the visual that fits it — do not default everything to count-up:
   - A countdown / time-remaining figure ("30 days left", "2 weeks to go", "one month until...") -> "countdown"
   - A specific calendar date ("July 28th", "by March 3rd", "expires on the 15th") -> "calendar"
   - A risk / negative-consequence framing (something that could go wrong, a warning, a "before it's too late", an "at risk" outcome) -> "gauge"
   - Any other number worth calling out (money, reps, distances, times, scores, percentages, quantities, counts) -> "count_up"
4. Output shape per visual type (all times in seconds, matching when that number/date is actually spoken):
   - count_up: {"visual":"count_up","title":"...","rows":[{"label":"UPPERCASE","seconds":12.3,"value":42,"prefix":"","divideBy":1,"decimals":0,"unit":"","tone":"accent|good|bad|normal"}]} — group values that belong together (e.g. before/after of the same metric) into ONE card as multiple rows, not separate cards. prefix/divideBy/decimals/unit format the number so it's compact and readable (e.g. divideBy 1000000 + decimals 1 -> "1.5" for 1,500,000) — never a raw unformatted number.
   - gauge: {"visual":"gauge","seconds":12.3,"title":"...","leftLabel":"UPPERCASE","rightLabel":"UPPERCASE","value":0-1} — leftLabel is the safe/good end, rightLabel is the risk/bad end, value is how far toward the risk end this moment lands (1.0 = fully at risk).
   - countdown: {"visual":"countdown","seconds":12.3,"value":30,"unitLabel":"DAYS","label":"UPPERCASE short label","headline":"a short sentence","headlineAccent":"optional second line, e.g. the consequence"}
   - calendar: {"visual":"calendar","seconds":12.3,"year":2026,"month":7,"targetDay":28,"eventLabel":"short label"} — if the transcript doesn't state a year explicitly, infer the correct one using the reference date given in the user message (e.g. a date mentioned as still upcoming should resolve to this year or next, not a past year).
5. If the transcript has no genuinely dramatic/comparison-worthy moments, return an empty data_points array. Do not invent one to fill the response, and do not force a domain's framing (financial, fitness, etc.) onto content that isn't actually about that.

Output ONLY valid JSON matching this shape, no markdown, no prose:
{
  "chapters": [{"at_seconds": 0, "label": "..."}],
  "data_points": [ /* each item is exactly one of the 4 shapes above, tagged by "visual" */ ]
}"""


# 超长转写截断：30+ 分钟的上传不能把请求撑爆模型上下文/悄悄变得又慢又贵。
# ~12k 字符对章节/数据点判断（粗读，不是逐词剪辑决策）绰绰有余。
_MAX_TRANSCRIPT_CHARS = 12000


def _build_transcript_text(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        lines.append(f"[{seg['start']:.1f}s] {seg['text'].strip()}")
    text = "\n".join(lines)
    if len(text) > _MAX_TRANSCRIPT_CHARS:
        logger.warning(
            f"content_planner: 转写 {len(text)} 字符超过 {_MAX_TRANSCRIPT_CHARS} 上限，"
            f"截断处理——超出部分的章节/数据点会漏掉（超长视频的已知 MVP 限制，非静默失败）"
        )
        text = text[:_MAX_TRANSCRIPT_CHARS] + "\n[... transcript truncated — video continues past this point ...]"
    return text


def plan_content(segments: list[dict], duration: float) -> dict[str, Any]:
    """转写分段 -> 章节 + 四种图形的计划（已经是 frame 单位，可以直接喂给 XiaojinEditorial）。

    LLM 调用失败或没配 key 时，返回空计划——内容判断本来就是锦上添花，不应该
    因为它失败就搞垮整条剪辑流程。
    """
    empty = {
        "chapters": [], "data_cards": [], "gauges": [], "countdowns": [], "calendar_events": [],
        "mode_schedule": [{"frame": 0, "mode": "dominant"}],
    }

    transcript_text = _build_transcript_text(segments)
    today = date.today().isoformat()
    user_message = (
        f"Reference date (for resolving relative/year-less dates): {today}\n"
        f"Video duration: {duration:.1f}s\n\nTranscript:\n{transcript_text}"
    )

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

    data_cards: list[dict] = []
    gauges: list[dict] = []
    countdowns: list[dict] = []
    calendar_events: list[dict] = []
    # (start_frame, end_frame) windows where the content zone needs room —
    # shared across all 4 visual types, since all of them need the
    # SpeakerCard to be in Workflow (shrunk) mode while they're on screen.
    workflow_ranges: list[tuple[int, int]] = []

    # All 4 visual types share ONE content-zone lane (they all drive the same
    # dominant/workflow mode_schedule below), so process data points in
    # chronological order and floor each one's mount frame against the
    # previous one's end — same rule the manual pipeline documents in
    # CLAUDE-v2.md: "the standard 50-frames-early offset must be floored at
    # previous_beat_end + ~8-10f", otherwise a rapid back-to-back run of data
    # points (little/no pause between them in speech) either overlaps two
    # visuals in the same screen position or pops the next one in mid-
    # sentence on the one before it (confirmed bug, motion/mrbeast-clip).
    data_points = sorted(raw.get("data_points") or [], key=_dp_seconds)
    next_available_frame = 0

    for dp in data_points:
        if not isinstance(dp, dict):
            # LLM 偶发在数组里塞非 dict 条目（实测出过 str）——跳过而不是
            # AttributeError 炸掉整个规划。
            continue
        visual = dp.get("visual") or "count_up"  # backward-compatible default
        try:
            if visual == "gauge":
                entry, target = _plan_gauge(dp, next_available_frame), gauges
            elif visual == "countdown":
                entry, target = _plan_countdown(dp, next_available_frame), countdowns
            elif visual == "calendar":
                entry, target = _plan_calendar(dp, next_available_frame), calendar_events
            else:
                entry, target = _plan_count_up(dp, next_available_frame), data_cards
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"content_planner: 跳过一个解析失败的数据点 (visual={visual}): {e}")
            continue

        if entry is None:
            continue
        target.append(entry)
        workflow_ranges.append((entry["mountFrame"], entry["endFrame"]))
        next_available_frame = entry["endFrame"] + MIN_GAP_AFTER_PREVIOUS_FRAMES

    # 同位置图形的接力钳制（contract② endFrame，merge runbook 的 P2 任务）：
    # 上面的 chronological floor 已经让同坑位图形按顺序不重叠，这里是双重保险——
    # 万一有别的路径（例如显式传入 op["data_cards"]）绕过了上面的排序/floor逻辑，
    # 仍然按 mountFrame 排序后把前者的 endFrame 钳到后者的 mountFrame（组件会
    # 做 15 帧淡出），不会同位置永久叠上（P3 修的 "cards never disappear" bug
    # 的另一半）。
    slotted = sorted(
        (g for g in (data_cards + gauges + countdowns + calendar_events)),
        key=lambda g: g["mountFrame"],
    )
    for cur, nxt in zip(slotted, slotted[1:]):
        if (cur.get("x", 80), cur.get("y", 900)) == (nxt.get("x", 80), nxt.get("y", 900)):
            cur["endFrame"] = min(cur.get("endFrame", nxt["mountFrame"]), nxt["mountFrame"])

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

    return {
        "chapters": chapters, "data_cards": data_cards, "gauges": gauges,
        "countdowns": countdowns, "calendar_events": calendar_events, "mode_schedule": dedup,
    }


def _dp_seconds(dp: dict) -> float:
    """Earliest spoken timestamp for a data point, for chronological sorting."""
    if not isinstance(dp, dict):
        # Same non-dict guard as the main loop — sorted() calls this key
        # function on every element before the loop body ever runs, so a
        # stray non-dict entry has to be handled here too, not just there.
        return float("inf")
    visual = dp.get("visual") or "count_up"
    if visual == "count_up":
        secs: list[float] = []
        for r in dp.get("rows") or []:
            try:
                secs.append(float(r["seconds"]))
            except (KeyError, TypeError, ValueError):
                continue
        return min(secs) if secs else float("inf")
    try:
        return float(dp["seconds"])
    except (KeyError, TypeError, ValueError):
        return float("inf")


def _plan_count_up(dp: dict, min_mount_frame: int) -> Optional[dict]:
    rows_in = dp.get("rows") or []
    if not rows_in:
        return None
    row_seconds = [float(r["seconds"]) for r in rows_in]

    card_mount_frame = max(min_mount_frame, round(min(row_seconds) * FPS) - MOUNT_LEAD_FRAMES)
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
        return None
    last_row_frame = round(max(row_seconds) * FPS)
    end_frame = max(last_row_frame, card_mount_frame) + HOLD_AFTER_LAST_ROW_FRAMES
    return {
        "title": str(dp.get("title", ""))[:40],
        "x": 80, "y": 900, "width": 920,
        "mountFrame": card_mount_frame,
        "endFrame": end_frame,
        "rows": rows,
    }


def _plan_gauge(dp: dict, min_mount_frame: int) -> Optional[dict]:
    sec = float(dp["seconds"])
    value = _num(dp.get("value"))
    if value is None:
        return None
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = mount_frame + GAUGE_ANIMATION_FRAMES + HOLD_AFTER_LAST_ROW_FRAMES
    return {
        "title": str(dp.get("title", ""))[:60],
        "leftLabel": str(dp.get("leftLabel", ""))[:16],
        "rightLabel": str(dp.get("rightLabel", ""))[:16],
        "value": max(0.0, min(1.0, value)),
        "mountFrame": mount_frame,
        "endFrame": end_frame,
    }


def _plan_countdown(dp: dict, min_mount_frame: int) -> Optional[dict]:
    sec = float(dp["seconds"])
    value = _num(dp.get("value"))
    if value is None:
        return None
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = mount_frame + COUNTDOWN_ANIMATION_FRAMES + HOLD_AFTER_LAST_ROW_FRAMES
    entry: dict[str, Any] = {
        "value": value,
        "unitLabel": str(dp.get("unitLabel", ""))[:16],
        "label": str(dp.get("label", ""))[:40],
        "headline": str(dp.get("headline", ""))[:80],
        "mountFrame": mount_frame,
        "endFrame": end_frame,
    }
    if dp.get("headlineAccent"):
        entry["headlineAccent"] = str(dp["headlineAccent"])[:40]
    return entry


def _plan_calendar(dp: dict, min_mount_frame: int) -> Optional[dict]:
    sec = float(dp["seconds"])
    year = dp.get("year")
    month = dp.get("month")
    target_day = dp.get("targetDay")
    if year is None or month is None or target_day is None:
        return None
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = mount_frame + CALENDAR_DISPLAY_FRAMES
    return {
        "year": int(year), "month": int(month), "targetDay": int(target_day),
        "eventLabel": str(dp.get("eventLabel", ""))[:60],
        "mountFrame": mount_frame,
        "endFrame": end_frame,
    }


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

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

from .config import get_config
from .llm_client import call_llm_chat

logger = logging.getLogger(__name__)

FPS = 30
# 口误剪辑保留片段的边界 padding：ASR 词级时间戳是贴着可听见音素的最紧边界，
# 完全按这个边界切会咬掉一个词开头的爆破音或结尾的尾音（choppy cuts 的确认
# 根因之一——不是 video_trimmer 的 afade 不够，是 afade 在从头就被咬掉内容
# 的边界上淡入/淡出）。两侧各留这么多，同时下面会钳制到不超过跟相邻被剪
# 片段之间空隙的一半，保证不会把已判定为口误的词内容重新纳入。
#
# 0.04 -> 0.08：确认过的真实生产 bug——剪掉一处未说完就重说的整句（"Your
# current plan covers you for" 说到一半重说)后，两句话拼接处实测只剩 ~61ms
# 的真静音，加上 video_trimmer 的淡入淡出几乎完全吃掉这点空隙，听感上是
# "赶"、"卡"，而不是一次干净的剪辑。跟 video_trimmer.py 的 fade_d=0.06 配套
# 加大，给拼接点更多喘息空间；仍然远小于一个词的时长，不会咬字。
FILLER_CUT_PAD_SECONDS = 0.08
# 口误复核最多重试几次（不含第一次判断）。确认过的真实 bug：一份转写里同时有
# 3 处遗留重录，单次重试后仍然全部原样播出——现在每轮都把复核返回的全部
# issues 喂回去（而不是只取第一条），2 次重试通常足够收敛；仍不通过就照常
# 交付并打日志，不无限重试卡住整条剪辑流程。
FILLER_VERIFY_MAX_RETRIES = 2
# compose-director.md 的 beat anchoring 规则：卡片在关键词被说出之前 50 帧上场。
MOUNT_LEAD_FRAMES = 50
# 前一个数据点结束后至少留这么多帧再上场下一个——避免连续报数据时后一个卡片
# 提前 50 帧的偏移把它顶到前一个卡片还没结束、或者前一个数据点自己的话还没
# 说完的时刻（CLAUDE-v2.md 记录过这个 bug：flat -50 offset 会让下一阶段的数字
# 在上一阶段的话说到一半时就跳出来）。
MIN_GAP_AFTER_PREVIOUS_FRAMES = 10
# 图形展示完之后，Workflow 模式至少再停留这么久再切回 Dominant，避免切换过快。
HOLD_AFTER_LAST_ROW_FRAMES = 90
# SpeakerCard 提前这么多帧从 Dominant 收进 Workflow，好让图形上场时卡片已经
# 让开位置。
WORKFLOW_SHRINK_LEAD_FRAMES = 10
# 仪表盘/倒计时自身的入场+动画时长（对应组件默认值），决定它们各自的"活跃窗口"。
GAUGE_ANIMATION_FRAMES = 20 + 50  # fillDelayFrames + fillDurationFrames 默认值
COUNTDOWN_ANIMATION_FRAMES = 40  # revealFrames 默认值
CALENDAR_DISPLAY_FRAMES = 150  # 日历没有"动画完成"节点，给一个固定停留时长
BEFORE_AFTER_ANIMATION_FRAMES = 40  # BudgetRevealSection 数值动画时长（组件默认值）
# TimelineSection 每个节点之间至少留这么多帧，避免相邻阶段的点动画/文字入场叠在一起
# （组件自身的 GROW_WINDOW 是 25 帧）。
TIMELINE_NODE_MIN_GAP_FRAMES = 30
# 全画布接管至少要有这么多帧才值得放一个多阶段 timeline 图形（给动画+停留留出空间）。
TIMELINE_MIN_SECTION_FRAMES = 90

# Content zone geometry — the full-width band UNDER the Workflow-mode speaker
# card where every data-display element renders, matching video-studio's
# reference build exactly (vell-renewal-fresh: card 1000x900 at y=100, content
# at CONTENT_TOP=1040, chrome floor ~1800). Keep the three _CONTENT_ZONE_*
# numbers in sync with pipeline_runner.py's copies. y was briefly 844 (card
# h=700) — confirmed real bug: a 960-wide/700-tall box is WIDTH-bound under
# objectFit:cover, so shrinking height only cropped the speaker down to
# head-only; the reference's 900px height is what shows face + chest.
_CONTENT_ZONE_X = 60
_CONTENT_ZONE_Y = 1040
_CONTENT_ZONE_WIDTH = 960
_CONTENT_ZONE_BOTTOM = 1800

# --- Content-zone STACKING (the systemic empty-space fix) -------------------
# The reference build never shows one lonely card in a 760px zone: its
# CoverageSection stacks coverage card -> premium card -> ratio card -> accent
# pill, each landing on its own spoken beat and PERSISTING until the section
# moves on. The old planner serialized visuals (one on screen at a time, each
# vanishing before the next mounted), which read as constant empty space.
# Now: visuals whose beats fall close together in time stack vertically at
# computed y offsets and exit together as one passage.
_STACK_GAP = 20
# If the next visual's natural mount lands within this many frames of the
# current stack's last element's natural end, they're one passage — stack.
_STACK_JOIN_WINDOW_FRAMES = 8 * FPS
# Two stacked elements never pop in on the same frame — small entrance stagger.
_MIN_STACK_STAGGER_FRAMES = 18
# Conservative rendered-height estimates per visual (px at 1080x1920), used
# only to decide how many elements fit in a stack — not sent to the renderer.
_EST_HEIGHT_BY_VISUAL = {
    "gauge": 270, "countdown": 250, "calendar": 430,
    "before_after": 330, "contact_cue": 200,
}
_PILL_EST_HEIGHT = 100


def _est_height(visual: str, entry: dict) -> int:
    if visual == "count_up":
        return 100 + 112 * len(entry.get("rows") or [])
    return _EST_HEIGHT_BY_VISUAL.get(visual, 320)


# 全画布章节接管（sections）：内容占满整个画布。曾经让 SpeakerCard 缩成右下角
# 小 pip，实测哪怕真小 pip 也逼着接管图形偏到左半边躲它（确认过的用户反馈：
# "warning 图标位置很偏"）——现在接管期间 SpeakerCard 直接淡出隐藏
# （pipeline_runner 生成 opacityKeyframes），图形全画布居中。哨兵值保留，
# 语义从"用 pip 框"变成"隐藏卡片"。
SECTION_PIP_SENTINEL = 10_000_000

SYSTEM_PROMPT = """You analyze a talking-head video's transcript and produce a content plan for the video's chapter markers and any data-worthy moments, following these rules (from the project's compose-director.md style codex, "Data Display Analysis" section).

**This video could be about absolutely anything** — cooking, fitness, finance, a product review, a story, a tutorial, a rant. Do not default to any one domain or topic. Every chapter label, and whether there are any data points at all, must come ONLY from what THIS specific transcript actually says — never reuse a pattern, label, or topic from any other video you've seen. A video with no notable moments should get an empty data_points array; that is a completely normal, common outcome, not a failure.

1. Chapters: split the video into 2-5 chapters based on the ACTUAL topic shifts in THIS transcript. Each chapter needs a short label in the video's PRIMARY SPOKEN LANGUAGE (2-4 characters if Chinese, 1-2 UPPERCASE words if English) in "label", plus a 1-2 word UPPERCASE English label in "label_en" (e.g. a Chinese cooking video: {"label":"食材","label_en":"INGREDIENTS"}; an English workout video: {"label":"WARMUP","label_en":"WARMUP"}). These are just illustrations of the LABEL STYLE, not topics to look for. Also give the second each chapter starts.
   Per chapter, also decide its SECTION TREATMENT: "takeover": true when the chapter is a focused explanation moment that deserves a full-canvas section (a warning, a key benefit, a countdown, a data reveal) — chapters that are casual talking should be false; "icon": one of "shield_check" (protection/guarantee/coverage topics) | "warning" (risk/consequence/mistake topics) | "clock" (deadline/time-pressure topics) | null (no fitting icon — do NOT force one); "dark": true for serious/warning/dramatic chapters (renders on a dark background), false for neutral/positive ones; "warn": true only for negative-consequence chapters.
1b. Intro title card: from the transcript, write an opening title card — "eyebrow": a 2-5 word UPPERCASE English tagline of what this video IS (e.g. "POLICY RENEWAL REMINDER", "WORKOUT PLAN", "PRODUCT REVIEW"); "title": the video's one-line headline in its primary spoken language (<=10 Chinese chars or <=6 English words); "subtitle": who/what it's from if the speaker names themselves/company, else a short secondary line. Ground every word in what the transcript actually says.
1c. Outro CTA: a closing call-to-action card matching what the SPEAKER actually asks or the video's natural close — "kicker": short UPPERCASE English, "headline": short line in primary language, "headline_accent": optional second line, "subtext": one supporting sentence, "cta_label": short imperative pill text (e.g. "留言告訴我！" / "Follow for more"). Do NOT invent contact info or promises the speaker never made.
2. Data Display Analysis: scan the transcript for hard data points worth a visual callout, of ANY kind this video actually mentions — money, reps, distances, times, scores, percentages, quantities, counts, dates, countdowns, risks. Only flag a moment if it's genuinely the point of that sentence (a dramatic reveal, a before/after comparison, a key stat, a warning) — most videos have zero to two such moments, not one per sentence. A number mentioned in passing that isn't the point of the sentence is NOT a data point.
3. For each flagged moment, classify it into exactly the visual that fits it — do not default everything to count-up:
   - A countdown / time-remaining figure ("30 days left", "2 weeks to go", "one month until...") -> "countdown"
   - A specific calendar date ("July 28th", "by March 3rd", "expires on the 15th") -> "calendar"
   - A risk / negative-consequence framing (something that could go wrong, a warning, a "before it's too late", an "at risk" outcome) -> "gauge"
   - A single dramatic BEFORE/AFTER comparison of the SAME metric across two points in time (a cost/price/metric/quantity that jumped or dropped, e.g. "we used to spend $X, now we spend $Y", "went from 10 to 100") — genuinely the single richest moment in the video, not a routine number — -> "before_after". Only use this for a moment that really is a dramatic two-point comparison; most videos have zero of these.
   - Any other number worth calling out (money, reps, distances, times, scores, percentages, quantities, counts) -> "count_up"
   - A punchy spoken line worth showing as typography — the thesis, a strong claim, a memorable one-liner ("this changed everything", "never skip this step") -> "quote". This is the PRIMARY visual for videos with no numeric moments at all: a data-less video should still get 1-3 quote moments (its actual best lines, verbatim — never paraphrase or invent). Videos WITH plenty of numeric visuals need 0-1 quotes at most.
3b. Multi-stage process timeline: if (and only if) the transcript describes a genuine multi-step SEQUENTIAL process with 3-5 distinct stages, each with its own duration/quantity ("first we do X for N weeks, then Y for M months, then Z..." — a workflow, a production pipeline, a plan with ordered phases), this deserves a full-canvas timeline graphic instead of a data card. It must correspond to exactly one chapter that you also mark "takeover": true and "dark": true (and "icon": null — the timeline fills that space instead) — set "process_timeline" to describe it, referencing that chapter's exact "label" text. Most videos have none of this; only use it for a real ordered multi-stage process, not a simple list.
4. Output shape per visual type (all times in seconds, matching when that number/date is actually spoken):
   - count_up: {"visual":"count_up","title":"...","rows":[{"label":"short label in the video's primary language","label_en":"1-3 word UPPERCASE ENGLISH","seconds":12.3,"value":42,"prefix":"","divideBy":1,"decimals":0,"unit":"","tone":"accent|good|bad|normal"}]} — group values that belong together into ONE card as multiple rows, not separate cards (but see "before_after" above for a two-point comparison of the SAME metric — that's richer as its own visual, not a two-row count_up card). prefix/divideBy/decimals/unit format the number so it's compact and readable (e.g. divideBy 1000000 + decimals 1 -> "1.5" for 1,500,000) — never a raw unformatted number.
   - gauge: {"visual":"gauge","seconds":12.3,"title":"...","leftLabel":"UPPERCASE","rightLabel":"UPPERCASE","value":0-1} — leftLabel is the safe/good end, rightLabel is the risk/bad end, value is how far toward the risk end this moment lands (1.0 = fully at risk).
   - countdown: {"visual":"countdown","seconds":12.3,"value":30,"unitLabel":"DAYS","label":"UPPERCASE short label","headline":"a short sentence","headlineAccent":"optional second line, e.g. the consequence"}
   - quote: {"visual":"quote","seconds":12.3,"text":"the exact spoken line, verbatim, <=80 chars","attribution":"optional speaker name if they introduce themselves"}
   - calendar: {"visual":"calendar","seconds":12.3,"year":2026,"month":7,"targetDay":28,"eventLabel":"short label"} — if the transcript doesn't state a year explicitly, infer the correct one using the reference date given in the user message (e.g. a date mentioned as still upcoming should resolve to this year or next, not a past year).
   - before_after: {"visual":"before_after","kicker":"short UPPERCASE English label for what's being compared, e.g. \"THE BUDGET\"","leftLabel":"UPPERCASE, e.g. \"2 YEARS AGO\"","leftSeconds":10.2,"leftValue":100,"leftPrefix":"$","leftSuffix":"K","rightLabel":"UPPERCASE, e.g. \"TODAY\"","rightSeconds":14.8,"rightValue":1.5,"rightPrefix":"$","rightSuffix":"M","rightDecimals":1} — leftSeconds/rightSeconds are when EACH value is actually spoken (often several seconds apart); prefix/suffix/rightDecimals format each number for display (e.g. rightDecimals 1 -> "1.5").
   - contact_cue: {"visual":"contact_cue","seconds":34.2} — the moment the speaker actually tells the viewer HOW to reach them (mentions WhatsApp/phone/QR code/email/"message me"/"contact me"). Only emit this if the video genuinely says something like that, at the exact second it's spoken. This does NOT invent or supply the actual contact info (that comes from elsewhere) — it ONLY marks the timing so the contact card appears exactly when it's being talked about, instead of only at a generic outro.
4b. Accent pill: EVERY data point above (count_up/gauge/countdown/calendar/before_after) may additionally carry "pill": a short punchy takeaway line (<=44 chars, in the video's primary spoken language) shown as a full-width accent pill directly under that graphic — e.g. a coverage card's pill might be "Policy Active — Renew in 30 Days". It must be grounded in that exact sentence's content, never invented. Include it whenever the sentence has a natural takeaway (most do); omit only when nothing fits. These pills are how the canvas stays visually full, so prefer including one.
5. If the transcript has no genuinely dramatic/comparison-worthy moments AND no quote-worthy lines, return an empty data_points array. Do not invent one to fill the response, and do not force a domain's framing (financial, fitness, etc.) onto content that isn't actually about that.

Output ONLY valid JSON matching this shape, no markdown, no prose:
{
  "chapters": [{"at_seconds": 0, "label": "...", "label_en": "...", "takeover": false, "icon": null, "dark": false, "warn": false}],
  "intro": {"eyebrow": "...", "title": "...", "subtitle": "..."},
  "outro": {"kicker": "...", "headline": "...", "headline_accent": "...", "subtext": "...", "cta_label": "..."},
  "data_points": [ /* each item is exactly one of the 5 shapes above, tagged by "visual" */ ],
  "process_timeline": null /* or {"chapter_label": "must exactly match one chapter's \"label\" above", "heading": "short UPPERCASE English, e.g. \"FROM IDEA TO UPLOAD\"", "stages": [{"label":"UPPERCASE short stage name","seconds":12.3,"prefix":"","target":3,"unit":"MONTHS","is_total":false}, ...]} */
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


def _call_llm_json(label: str, system_prompt: str, user_message: str, *, temperature: float, model: Optional[str] = None) -> Optional[dict]:
    """call_llm_chat + json.loads, with ONE retry of the whole call if the
    response isn't valid JSON.

    call_llm_chat already retries transient HTTP failures internally; this
    is a different failure mode — the call succeeds but the model doesn't
    return parseable JSON despite being asked to (a real, observed failure
    mode: same prompt succeeded on a later attempt with no code changes).
    Returns the parsed dict, or None if the LLM is unusable or two straight
    attempts both failed to produce valid JSON.
    """
    content = call_llm_chat(system_prompt, user_message, temperature=temperature, model=model)
    if content is None:
        logger.info(f"content_planner: {label} 没配 LLM 或调用失败，跳过")
        return None

    try:
        return json.loads(content)
    except Exception as e:
        logger.warning(f"content_planner: {label} 解析 LLM 输出失败，重试一次: {e}")

    content = call_llm_chat(system_prompt, user_message, temperature=temperature, model=model)
    if content is None:
        logger.warning(f"content_planner: {label} 重试调用 LLM 失败，跳过")
        return None
    try:
        return json.loads(content)
    except Exception as e:
        logger.warning(f"content_planner: {label} 重试后仍解析失败，跳过: {e}")
        return None


def plan_content(segments: list[dict], duration: float, *, feedback: Optional[str] = None) -> dict[str, Any]:
    """转写分段 -> 章节 + 四种图形的计划（已经是 frame 单位，可以直接喂给 XiaojinEditorial）。

    LLM 调用失败或没配 key 时，返回空计划——内容判断本来就是锦上添花，不应该
    因为它失败就搞垮整条剪辑流程。

    feedback: 视觉复审（qa_stills._vision_review）发现问题后，_op_apply_style
    重新规划一次时传入的具体问题描述——喂给同一个 LLM 调用，让它避开已知的
    错误（例如某张数据卡跟另一个元素挤在一起），而不是盲目重跑一次一模一样
    的判断。
    """
    empty = {
        "chapters": [], "data_cards": [], "gauges": [], "countdowns": [], "calendar_events": [],
        "before_after": [],
        "mode_schedule": [{"frame": 0, "mode": "dominant"}],
        "intro": None, "outro": None, "sections": [], "quotes": [], "contact_cue": None,
        "pills": [],
    }

    transcript_text = _build_transcript_text(segments)
    today = date.today().isoformat()
    user_message = (
        f"Reference date (for resolving relative/year-less dates): {today}\n"
        f"Video duration: {duration:.1f}s\n\nTranscript:\n{transcript_text}"
    )
    if feedback:
        user_message = (
            f"NOTE: a previous rendering of this exact plan had the following visual "
            f"problem — adjust the plan so it doesn't recur: {feedback}\n\n{user_message}"
        )

    # 走 _call_llm_json（JSON 解析失败整体重试一次）+ 长输出模型路由（DeepSeek
    # 网关 ~60s 硬时限，v4-pro 写不完整段规划 JSON，v4-flash 37s 完成）。
    raw = _call_llm_json("内容规划", SYSTEM_PROMPT, user_message, temperature=0.2,
                         model=get_config().llm_model_long_output)
    if raw is None:
        return empty

    # count_up 数值为 0 几乎总是提取失败（真实答案很少是 "$0"）——跟
    # gauge/countdown 不同，那两种 0 可能是合法值（如"剩 0 天"）。真实生产 bug：
    # 转写明明说的是 "$1.5 million"，提取结果却是 0。发现了就带着具体问题重试
    # 一次；重试后仍是 0 就整卡丢弃，宁可不显示也不交付一张错误的数字卡片。
    if not feedback:
        bad_titles = _zero_value_titles(raw)
        if bad_titles:
            logger.warning(f"content_planner: 疑似 0 值提取失败，重试一次: {bad_titles}")
            note = ("NOTE: a previous extraction got these value(s) wrong (extracted as 0 "
                    f"instead of the real figure spoken in the transcript): {', '.join(bad_titles)}. "
                    "Re-read the transcript carefully and extract the actual number.")
            retried = _call_llm_json("内容规划(重试0值)", SYSTEM_PROMPT, user_message + "\n\n" + note,
                                     temperature=0.2, model=get_config().llm_model_long_output)
            if retried is not None:
                raw = retried
            still_bad = set(_zero_value_titles(raw))
            if still_bad:
                logger.warning(f"content_planner: 重试后仍是 0，整卡丢弃: {still_bad}")
                raw["data_points"] = [
                    dp for dp in (raw.get("data_points") or [])
                    if not (isinstance(dp, dict) and dp.get("visual") == "count_up"
                            and str(dp.get("title", "")) in still_bad)
                ]

    plan = _to_frame_plan(raw, duration)
    # 视觉密度下限（richness floor）：video-studio CLAUDE-v2 §9 "score before
    # you ship" 的可自动化部分。规划质量不再依赖单次 LLM 判断的心情——不达标
    # 就带反馈重规划一轮，仍不达标就用确定性兜底从转写里挑句子做金句卡。
    return _apply_richness_floor(raw, plan, segments, duration)


def _to_frame_plan(raw: dict, duration: float) -> dict[str, Any]:
    chapters = []
    for c in raw.get("chapters") or []:
        try:
            entry = {"atFrame": max(0, round(float(c["at_seconds"]) * FPS)), "label": str(c["label"])[:20]}
            if c.get("label_en") and str(c["label_en"]).strip().upper() != str(c["label"]).strip().upper():
                entry["labelEn"] = str(c["label_en"])[:24]
            # 段落接管决策随章节走，映射 sections 时再消费（不进 chapters props）
            entry["_takeover"] = bool(c.get("takeover"))
            entry["_icon"] = c.get("icon") if c.get("icon") in ("shield_check", "warning", "clock") else None
            entry["_dark"] = bool(c.get("dark"))
            entry["_warn"] = bool(c.get("warn"))
            chapters.append(entry)
        except (KeyError, TypeError, ValueError):
            continue
    chapters.sort(key=lambda c: c["atFrame"])

    data_cards: list[dict] = []
    quotes: list[dict] = []
    gauges: list[dict] = []
    countdowns: list[dict] = []
    calendar_events: list[dict] = []
    before_afters: list[dict] = []
    contact_cues: list[dict] = []
    # (start_frame, end_frame, content_width) windows where the content zone
    # needs room — shared across all 4 visual types, since all of them need
    # the SpeakerCard to be in Workflow (shrunk) mode while they're on
    # screen. content_width (P3) is how wide that particular visual actually
    # renders (InfoCard's row-count-aware width, or a fixed per-type width
    # for gauge/countdown/calendar/before_after) — pipeline_runner uses it to
    # size the SpeakerCard's Workflow-mode box, so a narrow gauge doesn't
    # force the same aggressive shrink as a wide 5-row InfoCard.
    workflow_ranges: list[tuple[int, int, int]] = []

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
    pills: list[dict] = []

    # --- Stacking state (see _STACK_GAP block comment above) ---
    zone_h = _CONTENT_ZONE_BOTTOM - _CONTENT_ZONE_Y
    stack: list[dict] = []      # entries currently accumulating in the zone
    stack_used_h = 0
    stack_last_end = 0          # latest natural endFrame within the stack
    stack_start = 0

    def _flush_stack(next_mount: Optional[int] = None) -> None:
        """Close the current stack: all elements exit TOGETHER (the reference's
        accumulate-then-clear rhythm, and CLAUDE-v2.md's rule that a zone's
        previous occupants are fully gone before a new one mounts). The shared
        exit extends to just before the next passage starts — capped so a
        stack never lingers absurdly long past its own last beat — which is
        itself part of the empty-space fix: content persists instead of
        vanishing the moment its count-up settles.
        """
        nonlocal stack, stack_used_h, stack_last_end
        if not stack:
            return
        end = stack_last_end
        if next_mount is not None:
            end = min(max(end, next_mount - 5), stack_last_end + _STACK_JOIN_WINDOW_FRAMES)
            end = min(end, next_mount - 5)
        for e in stack:
            e["endFrame"] = end
        workflow_ranges.append((stack_start, end, _CONTENT_ZONE_WIDTH))
        stack, stack_used_h, stack_last_end = [], 0, 0

    for dp in data_points:
        if not isinstance(dp, dict):
            # LLM 偶发在数组里塞非 dict 条目（实测出过 str）——跳过而不是
            # AttributeError 炸掉整个规划。
            continue
        visual = dp.get("visual") or "count_up"  # backward-compatible default
        # Within a stack, only a small entrance stagger separates elements —
        # the old cross-visual serialization (`next end + gap`) is exactly
        # what produced one-lonely-card-at-a-time emptiness.
        min_mount = (stack[-1]["mountFrame"] + _MIN_STACK_STAGGER_FRAMES) if stack else 0
        try:
            if visual == "quote":
                entry, target = _plan_quote(dp, min_mount), quotes
            elif visual == "gauge":
                entry, target = _plan_gauge(dp, min_mount), gauges
            elif visual == "countdown":
                entry, target = _plan_countdown(dp, min_mount), countdowns
            elif visual == "calendar":
                entry, target = _plan_calendar(dp, min_mount), calendar_events
            elif visual == "before_after":
                entry, target = _plan_before_after(dp, min_mount), before_afters
            elif visual == "contact_cue":
                entry, target = _plan_contact_cue(dp, min_mount), contact_cues
            else:
                entry, target = _plan_count_up(dp, min_mount), data_cards
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"content_planner: 跳过一个解析失败的数据点 (visual={visual}): {e}")
            continue

        if entry is None:
            continue

        h = _est_height(visual, entry)
        solo = visual == "quote"  # QuoteCard is full-canvas typography, never stacked
        fits = (
            stack and not solo
            and entry["mountFrame"] <= stack_last_end + _STACK_JOIN_WINDOW_FRAMES
            and stack_used_h + _STACK_GAP + h <= zone_h
        )
        if fits:
            entry_y = _CONTENT_ZONE_Y + stack_used_h + _STACK_GAP
            stack_used_h += _STACK_GAP + h
        else:
            # The outgoing stack must get a minimum on-screen life before the
            # zone clears for this new passage — otherwise a near-immediate
            # follow-up (e.g. a solo quote right after a card mounts) would
            # flush the card after only a few visible frames.
            if stack and entry["mountFrame"] < stack_start + 60:
                entry["mountFrame"] = stack_start + 60
                entry["endFrame"] = max(entry["endFrame"], entry["mountFrame"] + 90)
            _flush_stack(next_mount=entry["mountFrame"])
            stack_start = entry["mountFrame"]
            entry_y = _CONTENT_ZONE_Y
            stack_used_h = zone_h if solo else h
        if "y" in entry:
            entry["y"] = entry_y
        stack.append(entry)
        stack_last_end = max(stack_last_end, entry["endFrame"])
        target.append(entry)

        # Companion accent pill (reference's terracotta "Policy Active —
        # Renew in 30 Days" move): a short takeaway line the LLM grounded in
        # this exact data point's sentence, mounting after the primary
        # graphic's entrance settles and exiting with the same stack.
        pill_text = str(dp.get("pill") or "").strip()
        if pill_text and not solo and stack_used_h + _STACK_GAP + _PILL_EST_HEIGHT <= zone_h:
            pill = {
                "text": pill_text[:48],
                "x": _CONTENT_ZONE_X, "width": _CONTENT_ZONE_WIDTH,
                "y": _CONTENT_ZONE_Y + stack_used_h + _STACK_GAP,
                "mountFrame": entry["mountFrame"] + 45,
                "endFrame": entry["endFrame"],
            }
            stack_used_h += _STACK_GAP + _PILL_EST_HEIGHT
            stack.append(pill)
            stack_last_end = max(stack_last_end, pill["endFrame"])
            pills.append(pill)

    _flush_stack()

    # 全画布章节接管（sections）：takeover 章节的跨度 = 本章 atFrame 到下一章
    # atFrame（或片尾）。接管期卡片应停靠（并入 workflow_ranges）。
    duration_frames = round(duration * FPS)
    timeline_plan = _plan_process_timeline(raw.get("process_timeline"), chapters, duration_frames)
    sections: list[dict] = []
    for idx, ch in enumerate(chapters):
        if not ch.get("_takeover"):
            continue
        start = ch["atFrame"]
        end = chapters[idx + 1]["atFrame"] if idx + 1 < len(chapters) else duration_frames
        if end - start < 60:  # 短于 2s 的章节不值得接管
            continue
        sec: dict[str, Any] = {"fromFrame": start, "toFrame": end}
        if ch.get("label"):
            sec["title"] = ch["label"]
        if ch.get("labelEn"):
            sec["eyebrow"] = ch["labelEn"]
        if ch.get("_icon"):
            sec["icon"] = ch["_icon"]
        if ch.get("_dark"):
            sec["colorMode"] = "dark"
        if ch.get("_warn"):
            sec["warn"] = True
        if timeline_plan and timeline_plan["chapter_index"] == idx:
            sec["timeline"] = {"heading": timeline_plan["heading"], "nodes": timeline_plan["nodes"]}
        sections.append(sec)
        workflow_ranges.append((start, end, SECTION_PIP_SENTINEL))
    for ch in chapters:
        for k in ("_takeover", "_icon", "_dark", "_warn"):
            ch.pop(k, None)

    # 同位置图形的接力钳制（contract② endFrame，merge runbook 的 P2 任务）：
    # 上面的 chronological floor 已经让同坑位图形按顺序不重叠，这里是双重保险——
    # 万一有别的路径（例如显式传入 op["data_cards"]）绕过了上面的排序/floor逻辑，
    # 仍然按 mountFrame 排序后把前者的 endFrame 钳到后者的 mountFrame（组件会
    # 做 15 帧淡出），不会同位置永久叠上（P3 修的 "cards never disappear" bug
    # 的另一半）。
    slotted = sorted(
        (g for g in (data_cards + gauges + countdowns + calendar_events + quotes + before_afters)),
        key=lambda g: g["mountFrame"],
    )
    for cur, nxt in zip(slotted, slotted[1:]):
        # Same-(x,y) occupants only — stacked elements live at different y
        # lanes and are ALLOWED to coexist (that's the whole point of the
        # stacking system above); this backstop is for anything that bypassed
        # it (e.g. explicit op["data_cards"] input) landing on the same slot.
        if (cur.get("x", _CONTENT_ZONE_X), cur.get("y", _CONTENT_ZONE_Y)) == (
                nxt.get("x", _CONTENT_ZONE_X), nxt.get("y", _CONTENT_ZONE_Y)):
            cur["endFrame"] = min(cur.get("endFrame", nxt["mountFrame"]), nxt["mountFrame"])

    dedup = _workflow_mode_schedule(workflow_ranges, round(duration * FPS))

    intro = None
    ri = raw.get("intro")
    if isinstance(ri, dict) and ri.get("title"):
        intro = {"eyebrow": str(ri.get("eyebrow", ""))[:40].upper(),
                 "title": str(ri["title"])[:36],
                 "subtitle": str(ri.get("subtitle", ""))[:40]}
    outro = None
    ro = raw.get("outro")
    if isinstance(ro, dict) and ro.get("headline"):
        outro = {"kicker": str(ro.get("kicker", ""))[:30].upper(),
                 "headline": str(ro["headline"])[:30],
                 "subtext": str(ro.get("subtext", ""))[:80],
                 "ctaLabel": str(ro.get("cta_label", ""))[:24],
                 "footerLabel": str(ro.get("footer_label", ""))[:40]}
        if ro.get("headline_accent"):
            outro["headlineAccent"] = str(ro["headline_accent"])[:30]

    return {
        "chapters": chapters, "data_cards": data_cards, "gauges": gauges,
        "countdowns": countdowns, "calendar_events": calendar_events, "before_after": before_afters,
        "mode_schedule": dedup,
        "intro": intro, "outro": outro, "sections": sections,
        "quotes": quotes, "contact_cue": contact_cues[0] if contact_cues else None,
        "pills": pills,
    }


def _workflow_mode_schedule(ranges: list[tuple[int, int, int]], duration_frames: int) -> list[dict]:
    """workflow_ranges -> mode_schedule，用区间事件扫描而不是"重叠就整段合并"。

    旧实现把重叠/相邻的 range 合并成一段并对 width 取 max——这在纯普通图形
    之间是对的（背靠背图形保持连续 Workflow，避免长大又立刻缩小的抖动，也
    避免 strict-increasing dedup 把丢帧的 shrink 吃掉，见旧注释），但
    SECTION_PIP_SENTINEL（全画布接管=隐藏卡片）一旦跟普通图形段重叠，max()
    会把整个合并段都感染成"隐藏"——确认过的真实 bug（MrBeast 片）：TIMELINE
    接管 100-540 跟 520-710 的 before/after 段重叠合并后，卡片从 100 帧一路
    隐藏到 710，接管早在 540 就结束了，后半段奶油底上只有底部一张预算卡，
    上半屏整个空白。

    事件扫描按帧维护两个覆盖计数（sentinel / normal），状态优先级
    hidden(sentinel) > workflow > dominant，每次状态变化输出一个 entry——
    接管结束但普通图形还在时，状态自然从 hidden 落回 workflow（卡片淡回，
    继续给下方图形让位），谁也不感染谁。
    """
    events: list[tuple[int, int, bool]] = []
    for start, end, width in ranges:
        s = max(1, int(start) - WORKFLOW_SHRINK_LEAD_FRAMES)
        e = max(s + 1, int(end))
        events.append((s, +1, width >= SECTION_PIP_SENTINEL))
        events.append((e, -1, width >= SECTION_PIP_SENTINEL))

    schedule = [{"frame": 0, "mode": "dominant"}]
    if not events:
        return schedule

    n_sent = n_norm = 0
    state = "dominant"  # dominant | workflow | hidden
    # 同一帧的所有事件先全部结算再判断状态，避免同帧先减后加产生假转换。
    events.sort(key=lambda ev: ev[0])
    i = 0
    while i < len(events):
        frame = events[i][0]
        while i < len(events) and events[i][0] == frame:
            _, delta, is_sent = events[i]
            if is_sent:
                n_sent += delta
            else:
                n_norm += delta
            i += 1
        new_state = "hidden" if n_sent > 0 else ("workflow" if n_norm > 0 else "dominant")
        if new_state == state:
            continue
        state = new_state
        if new_state == "dominant":
            if frame < duration_frames:
                schedule.append({"frame": frame, "mode": "dominant"})
        else:
            schedule.append({
                "frame": frame, "mode": "workflow",
                "contentWidth": SECTION_PIP_SENTINEL if new_state == "hidden" else _CONTENT_ZONE_WIDTH,
            })
    # interpolate() requires strictly increasing frames — backstop dedup.
    dedup: list[dict] = []
    for entry in schedule:
        if dedup and entry["frame"] <= dedup[-1]["frame"]:
            dedup[-1] = {**entry, "frame": dedup[-1]["frame"]}
            continue
        dedup.append(entry)
    return dedup


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
    if visual == "before_after":
        try:
            return float(dp.get("leftSeconds", dp.get("seconds")))
        except (TypeError, ValueError):
            return float("inf")
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
        if r.get("label_en") and str(r.get("label_en")).strip().upper() != str(r.get("label", "")).strip().upper():
            row["labelEn"] = str(r["label_en"])[:28]
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
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": card_mount_frame,
        "endFrame": end_frame,
        "rows": rows,
    }


def _plan_contact_cue(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """标记"说到怎么联系我"的那一刻——只决定 qrContact 该何时上场，不决定
    要不要显示（那由 op["qr_contact"] 是否给了真实联系方式决定，见
    pipeline_runner.py 对应注释）。确认过的真实用户反馈：QR 卡之前固定钉在
    片尾附近（duration - N 帧），跟视频里实际提到"WhatsApp 我"的那句话完全
    脱节——这里让它跟其它数据点一样吃 MOUNT_LEAD_FRAMES + 时间顺序钳制，
    在真正说到的时候上场。QRContactCard 组件本身没有 endFrame（挂载后一直
    留到片尾），这里的 endFrame 只是给 workflow_ranges/mode_schedule 算一个
    "至少停留多久"的窗口，不是真的到点就消失。
    """
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = mount_frame + COUNTDOWN_ANIMATION_FRAMES + HOLD_AFTER_LAST_ROW_FRAMES
    # y participates in content-zone stacking like every other visual — the
    # QR card mounts at whatever lane this cue's passage assigns it.
    return {"mountFrame": mount_frame, "endFrame": end_frame, "y": _CONTENT_ZONE_Y}


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
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
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
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
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
        # Calendar component renders at a fixed 460px width — center it in the
        # 960px zone lane rather than leaving it hugging the left edge.
        "x": _CONTENT_ZONE_X + (_CONTENT_ZONE_WIDTH - 460) // 2, "y": _CONTENT_ZONE_Y,
        "mountFrame": mount_frame,
        "endFrame": end_frame,
    }


QUOTE_DISPLAY_FRAMES = 140  # 金句停留 ~4.7s（读两遍的时间）


def _plan_quote(dp: dict, min_mount_frame: int) -> Optional[dict]:
    sec = float(dp["seconds"])
    text = str(dp.get("text", "")).strip()
    if not text:
        return None
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    entry: dict[str, Any] = {
        "text": text[:80],
        "mountFrame": mount_frame,
        "endFrame": mount_frame + QUOTE_DISPLAY_FRAMES,
    }
    if dp.get("attribution"):
        entry["attribution"] = str(dp["attribution"])[:40]
    return entry



def _plan_before_after(dp: dict, min_mount_frame: int) -> Optional[dict]:
    left_value = _num(dp.get("leftValue"))
    right_value = _num(dp.get("rightValue"))
    if left_value is None or right_value is None:
        return None
    try:
        left_sec = float(dp.get("leftSeconds", dp.get("seconds", 0)) or 0)
    except (TypeError, ValueError):
        left_sec = 0.0
    try:
        right_sec = float(dp.get("rightSeconds", left_sec) or left_sec)
    except (TypeError, ValueError):
        right_sec = left_sec

    mount_frame = max(min_mount_frame, round(left_sec * FPS) - MOUNT_LEAD_FRAMES)
    # secondRevealFrame must land on or after the right value's own beat, but
    # never before the card itself has visibly entered (same "floor against
    # the previous/own animation" principle as every other visual type here).
    second_reveal_frame = max(mount_frame + 20, round(right_sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = max(mount_frame, second_reveal_frame) + BEFORE_AFTER_ANIMATION_FRAMES + HOLD_AFTER_LAST_ROW_FRAMES

    entry: dict[str, Any] = {
        "kicker": str(dp.get("kicker", ""))[:40],
        "leftLabel": str(dp.get("leftLabel", ""))[:24],
        "leftValue": left_value,
        "rightLabel": str(dp.get("rightLabel", ""))[:24],
        "rightValue": right_value,
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame,
        "secondRevealFrame": second_reveal_frame,
        "endFrame": end_frame,
    }
    if dp.get("leftPrefix"):
        entry["leftPrefix"] = str(dp["leftPrefix"])
    if dp.get("leftSuffix"):
        entry["leftSuffix"] = str(dp["leftSuffix"])
    if dp.get("leftDecimals") is not None:
        entry["leftDecimals"] = dp["leftDecimals"]
    if dp.get("rightPrefix"):
        entry["rightPrefix"] = str(dp["rightPrefix"])
    if dp.get("rightSuffix"):
        entry["rightSuffix"] = str(dp["rightSuffix"])
    if dp.get("rightDecimals") is not None:
        entry["rightDecimals"] = dp["rightDecimals"]
    return entry


def _plan_process_timeline(raw_pt: Any, chapters: list[dict], duration_frames: int) -> Optional[dict]:
    """Chapter-attached multi-stage timeline (TimelineSection), matched by the
    chapter's exact "label" text since the LLM emits chapters and
    process_timeline in the same response. Also force-marks the matched
    chapter as a dark takeover with no icon (the timeline fills that space
    instead) — overriding whatever takeover/dark/icon booleans the LLM
    itself gave that chapter, since a process_timeline only makes sense atop
    a full dark takeover.
    """
    if not isinstance(raw_pt, dict):
        return None
    chapter_label = str(raw_pt.get("chapter_label", "")).strip()
    stages_in = raw_pt.get("stages") or []
    if not chapter_label or not isinstance(stages_in, list) or len(stages_in) < 2:
        return None

    match_idx = next(
        (i for i, c in enumerate(chapters) if str(c.get("label", "")).strip().lower() == chapter_label.lower()),
        None,
    )
    if match_idx is None:
        return None

    ch = chapters[match_idx]
    start = ch["atFrame"]
    end = chapters[match_idx + 1]["atFrame"] if match_idx + 1 < len(chapters) else duration_frames
    if end - start < TIMELINE_MIN_SECTION_FRAMES:
        return None

    nodes: list[dict] = []
    prev_floor = start
    for s in stages_in:
        if not isinstance(s, dict):
            continue
        target = _num(s.get("target"))
        sec = _num(s.get("seconds"))
        if target is None or sec is None:
            continue
        raw_frame = round(sec * FPS) - MOUNT_LEAD_FRAMES
        reveal_frame = max(raw_frame, prev_floor)
        nodes.append({
            "label": str(s.get("label", ""))[:24],
            "revealFrame": reveal_frame,
            "prefix": str(s.get("prefix", "")),
            "target": target,
            "unit": str(s.get("unit", ""))[:16],
            "isTotal": bool(s.get("is_total")),
        })
        prev_floor = reveal_frame + TIMELINE_NODE_MIN_GAP_FRAMES
    if len(nodes) < 2:
        return None

    # Overrides whatever the LLM said for this specific chapter — a
    # process_timeline requires the dark full-canvas takeover treatment
    # (TimelineSection is hardcoded for a dark canvas, see its doc comment),
    # and it occupies the same space an icon would.
    ch["_takeover"] = True
    ch["_dark"] = True
    ch["_icon"] = None

    return {
        "chapter_index": match_idx,
        "heading": str(raw_pt.get("heading", ""))[:40],
        "nodes": nodes,
    }


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _zero_value_titles(raw: dict) -> list[str]:
    """count_up cards with a row whose value extracted as 0 — titles only,
    used both as retry feedback and as the final "still bad after retry, drop
    the whole card" filter. count_up 0 is treated as always-suspicious, unlike
    gauge/countdown where 0 is a legitimate answer (e.g. "0 days left")."""
    titles = []
    for dp in raw.get("data_points", []) or []:
        if not isinstance(dp, dict) or dp.get("visual") != "count_up":
            continue
        if any(isinstance(r, dict) and _num(r.get("value")) == 0.0 for r in (dp.get("rows") or [])):
            titles.append(str(dp.get("title", "")))
    return titles


# ---------------------------------------------------------------------------
# 视觉密度下限（richness floor）
#
# "每条视频保证有画面节奏"必须是机制而不是运气：任意连续 RICHNESS_WINDOW
# 秒内至少要有一个画布事件（图形/金句/段落接管），否则观感就是"卡片+字幕
# 干坐着"。检查是确定性的；修复分两级——先带着具体空档反馈让 LLM 补一轮
# （最多一轮，对齐 reviewer 协议的轮数上限），还不行就机械地从空档里挑最长
# 的完整转写句做金句卡（原话，不需要任何判断力，保证下限）。
# ---------------------------------------------------------------------------

# 12s -> 8s：确认过的真实用户反馈——12s 的容忍窗口在实际成片里仍然读作"大段
# 空白"，跟 video-studio 参考成片（CoverageSection.tsx 一个章节内连续 4 个
# 卡片首尾相接）比密度明显不够。收紧阈值让下面的空档检测+兜底机制更早介入，
# 而不是等到快 15s 无画面才触发。
RICHNESS_WINDOW_FRAMES = 8 * FPS   # 超过 8s 无画布事件 = 稀疏
_FLOOR_HEAD_SKIP_FRAMES = 90        # 开场有 intro 标题卡罩着
_FLOOR_TAIL_SKIP_FRAMES = 150       # 片尾有 outro CTA 罩着
# 3 -> 8: tightening RICHNESS_WINDOW_FRAMES to 8s means a long, entirely
# uncovered stretch now subdivides into more windows needing their own
# fallback quote (e.g. a bare 60s video's ~52s checked span is ~7 windows,
# not ~4) -- keeping the old cap of 3 would silently leave most of a sparse
# long video unfilled even when transcript sentences ARE available for every
# window.
_FLOOR_MAX_FALLBACK_QUOTES = 8

REPLAN_SYSTEM_PROMPT = """You previously produced a content plan for this talking-head video, but the listed time spans have NO visual event at all (no data graphic, no quote, no section takeover) — on screen it's just the speaker and captions for too long.

From the transcript lines spoken WITHIN those spans only, add visual moments using the same shapes as before (count_up / gauge / countdown / calendar / quote). Prefer "quote" with the exact spoken line, verbatim — never invent or paraphrase. 1 moment per span is enough; skip a span if its lines are genuinely too weak to show (that is acceptable).

Output ONLY valid JSON: {"data_points": [ ... ]}"""


def _coverage_spans(plan: dict) -> list[tuple[int, int]]:
    spans = []
    for g in (plan["data_cards"] + plan["gauges"] + plan["countdowns"]
              + plan["calendar_events"] + plan["quotes"]):
        spans.append((g["mountFrame"], g.get("endFrame", g["mountFrame"] + 90)))
    for sec in plan["sections"]:
        spans.append((sec["fromFrame"], sec["toFrame"]))
    return sorted(spans)


def _sparse_gaps(plan: dict, duration: float) -> list[tuple[int, int]]:
    """无画布事件且长于阈值的帧区间。短视频（intro+outro 已covering）返回空。"""
    dur_frames = round(duration * FPS)
    end_limit = dur_frames - _FLOOR_TAIL_SKIP_FRAMES
    if end_limit - _FLOOR_HEAD_SKIP_FRAMES < RICHNESS_WINDOW_FRAMES:
        return []
    gaps = []
    cursor = _FLOOR_HEAD_SKIP_FRAMES
    for a, b in _coverage_spans(plan):
        if a - cursor > RICHNESS_WINDOW_FRAMES:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if end_limit - cursor > RICHNESS_WINDOW_FRAMES:
        gaps.append((cursor, end_limit))
    return gaps


def _fallback_quotes_for_gaps(gaps: list[tuple[int, int]], segments: list[dict],
                              existing_texts: set) -> list[dict]:
    """确定性兜底：空档按 12s 窗口切开，每个窗口挑档内最长的转写句 -> quote。

    一个空档只放一条是不够的（55s 的空档放一条金句，剩下 40s 还是稀疏）——
    保证的对象是"任意连续窗口都有事件"，所以按窗口逐段放置，直到配额用完
    或该窗口没有可用转写句（没句子的时段机制上无解，接受）。
    """
    out: list[dict] = []
    for a, b in gaps:
        cursor = a
        while cursor < b and len(out) < _FLOOR_MAX_FALLBACK_QUOTES:
            win_end = min(cursor + RICHNESS_WINDOW_FRAMES, b)
            a_s, b_s = cursor / FPS, win_end / FPS
            # Overlap, not containment: a segment merely needs to touch this
            # window, not be fully inside it. Confirmed real bug — a genuine
            # 12s gap (27s-39s) failed to fill ("no usable transcript
            # sentence available") purely because every real sentence
            # spanning that stretch started or ended a moment outside the
            # window's exact edges, so the old start>=a_s/end<=b_s containment
            # check matched nothing even though the gap was full of speech.
            candidates = [
                seg for seg in segments
                if seg.get("text", "").strip()
                and float(seg.get("start", 0)) < b_s
                and float(seg.get("end", seg.get("start", 0))) > a_s
            ]
            candidates.sort(key=lambda seg: len(seg["text"].strip()), reverse=True)
            for c in candidates:
                t = c["text"].strip()[:80]
                if len(t) >= 6 and t not in existing_texts:
                    # Anchor to the later of the segment's own start and this
                    # window's start — a segment that began before the gap
                    # opened shouldn't place its beat earlier than the gap
                    # itself.
                    anchor_s = max(a_s, float(c["start"]))
                    out.append({"visual": "quote", "seconds": anchor_s, "text": t})
                    existing_texts.add(t)
                    break
            cursor = win_end
        if len(out) >= _FLOOR_MAX_FALLBACK_QUOTES:
            break
    return out


def _apply_richness_floor(raw: dict, plan: dict, segments: list[dict],
                          duration: float, allow_replan: bool = True) -> dict:
    gaps = _sparse_gaps(plan, duration)
    if not gaps:
        return plan

    gap_desc = ", ".join(f"{a / FPS:.0f}s-{b / FPS:.0f}s" for a, b in gaps)
    logger.info(f"content_planner: 密度下限触发，空档: {gap_desc}")

    data_points = list(raw.get("data_points") or [])

    if allow_replan and segments:
        user_message = (
            f"Uncovered spans: {gap_desc}\n\nTranscript:\n"
            + _build_transcript_text(segments)
        )
        extra = _call_llm_json("密度补规划", REPLAN_SYSTEM_PROMPT, user_message,
                               temperature=0.2, model=get_config().llm_model_long_output)
        if extra and isinstance(extra.get("data_points"), list):
            data_points += [dp for dp in extra["data_points"] if isinstance(dp, dict)]
            plan = _to_frame_plan({**raw, "data_points": data_points}, duration)
            gaps = _sparse_gaps(plan, duration)
            if not gaps:
                logger.info("content_planner: 补规划一轮后密度达标")
                return plan

    # 仍有空档 -> 机械兜底（原话金句，不依赖判断力）
    existing = {str(dp.get("text", "")) for dp in data_points if isinstance(dp, dict)}
    fallback = _fallback_quotes_for_gaps(gaps, segments, existing)
    if fallback:
        logger.info(f"content_planner: 兜底金句 x{len(fallback)}（空档内最长转写句）")
        plan = _to_frame_plan({**raw, "data_points": data_points + fallback}, duration)
    else:
        logger.info("content_planner: 空档内无可用转写句，保持现计划（已尽机制所能）")
    return plan


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


def _plan_filler_removal_once(words: list[dict], duration: float, *, feedback: Optional[str] = None) -> list[dict]:
    """单次口误/重录判断 -> 保留片段列表。不含事后复核——见 plan_filler_removal。"""
    numbered = "\n".join(f"{i}: {w['word']} [{w['start']:.2f}-{w['end']:.2f}]" for i, w in enumerate(words))
    if feedback:
        numbered = (
            f"NOTE: a previous pass at this exact task missed the following issue — "
            f"make sure it's addressed this time: {feedback}\n\n{numbered}"
        )

    raw = _call_llm_json("口误检测", FILLER_SYSTEM_PROMPT, numbered, temperature=0.1,
                         model=get_config().llm_model_long_output)
    if raw is None:
        return []

    try:
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

    _pad_keep_ranges(keep_ranges, duration)

    logger.info(f"content_planner: 口误检测 -> 剪掉 {len(cut_indices)} 个词，保留 {len(keep_ranges)} 段")
    return keep_ranges


def _pad_keep_ranges(keep_ranges: list[dict], duration: float) -> None:
    """原地给每段保留片段的首尾各加一点 padding，钳制在 [0, duration] 内、且
    钳制到跟相邻片段之间空隙的一半——这样即使某个被剪的口误词很短，padding
    也不会把它的任何部分重新纳入保留范围。
    """
    for i, r in enumerate(keep_ranges):
        gap_before = (
            r["start_seconds"] - keep_ranges[i - 1]["end_seconds"] if i > 0 else r["start_seconds"]
        )
        gap_after = (
            keep_ranges[i + 1]["start_seconds"] - r["end_seconds"]
            if i + 1 < len(keep_ranges)
            else duration - r["end_seconds"]
        )
        pad_before = min(FILLER_CUT_PAD_SECONDS, max(0.0, gap_before) / 2)
        pad_after = min(FILLER_CUT_PAD_SECONDS, max(0.0, gap_after) / 2)
        r["start_seconds"] = max(0.0, r["start_seconds"] - pad_before)
        r["end_seconds"] = min(duration, r["end_seconds"] + pad_after)


def _words_in_keep_ranges(words: list[dict], keep_ranges: list[dict]) -> list[dict]:
    """还原"剪完后实际会播放"的词序列——keep_ranges 是按保留词的起止时间合并出来
    的连续区间，所以用时间戳做包含判断就能精确还原，不会有边界误差。
    """
    kept = []
    for w in words:
        for r in keep_ranges:
            if w["start"] >= r["start_seconds"] - 1e-6 and w["end"] <= r["end_seconds"] + 1e-6:
                kept.append(w)
                break
    return kept


VERIFY_FILLER_SYSTEM_PROMPT = """You are reviewing another editor's filler/retake removal
work on a talking-head video. You are given the transcript AS IT WILL PLAY AFTER their
cuts (word list, in order, with timestamps) — the filler/retake words they identified
have already been removed from this list. Check whether the remaining text still reads
as a clean single take: no leftover stutter, no abandoned false start, no repeated
phrase that should have been replaced by a later clean version, no dangling filler
word ("um"/"uh"/"like" as padding).

List EVERY remaining problem you find, not just the first one — a transcript can have
more than one leftover retake, and each one needs to be named so it can actually be cut.

Output ONLY valid JSON, no markdown, no prose:
{"clean": true} if it reads cleanly, or
{"clean": false, "issues": ["one sentence per distinct remaining problem", "..."]} if not."""


def verify_filler_removal(words: list[dict], keep_ranges: list[dict]) -> Optional[dict]:
    """复核 _plan_filler_removal_once 的输出：喂"剪完后实际会播放的词序列"给 LLM，
    确认真的没有遗留口误/重录。这是抓"漏剪重录"这类 bug 的关键补丁——单次判断
    之前没有任何事后检查。返回 None 表示复核本身不可用（无 LLM/调用失败/解析
    失败）——调用方应把 None 当作"假定通过"处理，跟本文件其余精修步骤一致。
    """
    if not words:
        return None
    kept_words = _words_in_keep_ranges(words, keep_ranges) if keep_ranges else words
    if not kept_words:
        return None
    numbered = "\n".join(f"{w['word']} [{w['start']:.2f}-{w['end']:.2f}]" for w in kept_words)
    return _call_llm_json("口误复核", VERIFY_FILLER_SYSTEM_PROMPT, numbered, temperature=0.1)


def plan_filler_removal(words: list[dict], duration: float) -> list[dict]:
    """转写词级时间戳 -> 保留片段列表（喂给 VideoTrimmer 的 concat 操作）。

    跟 remove_silences（纯静音检测）是两码事：这里判断的是"这个词是不是口误/
    语气词/重录的失败尝试"，静音检测测不到有声的"呃""嗯"，也测不到中间没停顿
    的重录。没配 LLM 或调用失败时返回空列表（调用方应该跳过这步，不要因为这个
    可选的精修步骤失败就搞垮整条剪辑流程）。

    加了一次事后复核（verify_filler_removal）：单次判断可能漏掉真实存在的重录
    （确认过的真实 bug——判断没通过任何检查就直接交付）。复核发现问题就把问题
    喂回去重新判断；最多重试 FILLER_VERIFY_MAX_RETRIES 次，仍不通过就照常返回，
    只打日志——这仍然是可选精修步骤，不该无限重试卡住整条剪辑流程。

    复核 schema 是"issues"（列表），不是单条"issue"——真实生产数据验证过一份
    转写里可以同时有 3 处遗留重录，早先的单条 issue schema 一次只能报一条，
    重试时只喂回其中一条问题，另外两条从没被 LLM 看见过、自然也没被剪掉（确认
    过的真实 bug：3 处都被复核标记出来过，重试一次后 3 处全部原样播出）。这里
    每轮把复核返回的全部 issues 拼接喂回去，而不是只取第一条。
    """
    if not words:
        logger.info("content_planner: 没有词级时间戳，跳过口误检测")
        return []

    keep_ranges = _plan_filler_removal_once(words, duration)

    for attempt in range(FILLER_VERIFY_MAX_RETRIES + 1):
        review = verify_filler_removal(words, keep_ranges)
        if review is None or review.get("clean", True):
            return keep_ranges

        issues = review.get("issues")
        if not issues:
            single = review.get("issue")
            issues = [single] if single else []
        issues = [str(i)[:200] for i in issues if i]

        if attempt >= FILLER_VERIFY_MAX_RETRIES:
            logger.warning(
                f"content_planner: 重试 {FILLER_VERIFY_MAX_RETRIES} 次后口误复核仍不通过，"
                f"按最新结果继续交付而不是无限重试: {issues}"
            )
            return keep_ranges

        feedback = "; ".join(issues) if issues else "unspecified leftover issue"
        logger.warning(f"content_planner: 口误复核发现 {len(issues)} 处遗留问题，重新判断一次: {feedback}")
        keep_ranges = _plan_filler_removal_once(words, duration, feedback=feedback)

    return keep_ranges

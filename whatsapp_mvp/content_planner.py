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
import math
import re
import time
from datetime import date
from typing import Any, Callable, Optional

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
# Fix E3：一个数据点有 _grounded_end_seconds（关键词收尾校准，见
# _ground_end_keyword）时，退场时机 = 那个收尾词的结束时间 + 这么多帧的
# 停留（不是立刻切走，给观众一点反应时间），而不是固定的动画时长常量。
_KEYWORD_EXIT_HOLD_FRAMES = 30
# 即使收尾关键词紧贴着入场关键词（说得很快），退场也不能比这更早——避免
# 图形只在画面上停留零点几秒就消失。
_MIN_VISUAL_HOLD_FRAMES = 45
# InfoCard 每一行自己的数字滚动动画时长（见 InfoCard.tsx 的
# interpolate(rowLocal, [0, 40], ...)）——某一行没有 end_keyword 校准时，
# 用它自己的出场帧 + 这个时长 + _KEYWORD_EXIT_HOLD_FRAMES 作为这一行的
# "自然收尾"，防止另一行（更早）校准过的收尾时间把它顶掉。
_COUNT_UP_ROW_ANIM_FRAMES = 40
# SpeakerCard 提前这么多帧从 Dominant 收进 Workflow，好让图形上场时卡片已经
# 让开位置。
WORKFLOW_SHRINK_LEAD_FRAMES = 10
# Fix C3：两段内容之间的间隙短于这个值(1.5s)时，卡片在间隙里长回 Dominant
# 又立刻缩回去的"抖动"会被合并/桥接掉——确认过的真实场景：两张图形只隔了
# 10 帧，卡片做了一次没有意义的鼓包动画。
_DOMINANT_JITTER_MERGE_FRAMES = 45
# 仪表盘/倒计时自身的入场+动画时长（对应组件默认值），决定它们各自的"活跃窗口"。
GAUGE_ANIMATION_FRAMES = 20 + 50  # fillDelayFrames + fillDurationFrames 默认值
COUNTDOWN_ANIMATION_FRAMES = 40  # revealFrames 默认值
CALENDAR_DISPLAY_FRAMES = 150  # 日历没有"动画完成"节点，给一个固定停留时长
BEFORE_AFTER_ANIMATION_FRAMES = 40  # BudgetRevealSection 数值动画时长（组件默认值）
# Fix E1（2026-07-16）：之前是 30 帧——组件自己的入场动画(GROW_WINDOW)就要
# 25 帧，30 帧的间隔等于每个阶段刚长完线就立刻开始长下一段，观众几乎没有
# "线已经停住、数字定住"的可读时刻，真实生产反馈确认过这个问题（"appears
# and disappears too fast"，job_73e873e4f7e1 真实成片：3 个阶段每 30 帧
# (1s)揭示一个，动画根本来不及沉淀）。改成 60 帧(2s)，25 帧动画结束后留
# ~35 帧真正的静止阅读时间。
TIMELINE_NODE_MIN_GAP_FRAMES = 60
# 全画布接管至少要有这么多帧才值得放一个多阶段 timeline 图形（给动画+停留留出空间）。
TIMELINE_MIN_SECTION_FRAMES = 90
# Fix D1：接管（说话人被隐藏、全画布展示）的强制上限——不管章节本身多长，
# 单次接管最多隐藏这么久。确认过的真实生产 bug（job_e44166eb8c38）：WARNING
# 接管从 771 帧一路延伸到片尾(1526)，说话人被隐藏 25.2s，再也没有恢复。
_TAKEOVER_HARD_CAP_FRAMES = 8 * FPS
# 接管裁剪后，内容结束还额外停留这么久再收起（2s），不是内容一结束就立刻收。
_TAKEOVER_CONTENT_HOLD_FRAMES = 60
# Fix D4：接管的开始时间给第一条真正内容留的入场提前量（2s）——见下面
# _to_frame_plan 里这个常量的用法注释。
_SECTION_HEADER_LEAD_FRAMES = 60
# Fix D2：全片说话人被隐藏（接管+金句）的总时长预算，超过这个比例就摘除
# 时长最长的接管（金句本身很短，从不摘除）。
_HIDDEN_BUDGET_FRACTION = 0.30

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
# Named (was a bare `-5` literal): the outgoing stack's shared exit must land
# this many frames before the next stack's mount — components fade out over
# 15 frames (every xiaojin card's `interpolate(frame,[endFrame-15,endFrame],...)`
# convention) and hard-return null at `frame >= endFrame`, so 20 guarantees the
# previous passage is fully gone, not just fading, before the next one's own
# entrance spring starts — the user's explicit "out fully, then in" ordering.
_STACK_EXIT_BUFFER_FRAMES = 20
# 处理"下一段内容想提前上场，跟前一段自己的自然收尾时间冲突"时，最多愿意
# 推迟下一段上场多少帧来换取前一段完整播完（而不是把前一段直接砍短）——
# 见 _flush_stack 调用前那段"先看能不能推迟，推不动再砍"的逻辑。超过这个
# 上限就放弃推迟，退回旧的砍短行为，避免前一段因为一次校准异常被强行拖到
# 离谱地久（那是另一类被投诉过的问题——数据卡"像一大段文字一样赖着不走"）。
_STACK_HANDOFF_MAX_DELAY_FRAMES = 180  # 6s
# Conservative rendered-height estimates per visual (px at 1080x1920), used
# only to decide how many elements fit in a stack — not sent to the renderer.
# Bumped alongside the matching component size-ups (RiskGauge/CountdownRing/
# Calendar all "biggened" per user feedback — see each component's own diff).
_EST_HEIGHT_BY_VISUAL = {
    "gauge": 330, "countdown": 300, "calendar": 560,
    "before_after": 330, "contact_cue": 200, "topic_card": 180,
    "location_pin": 190, "testimonial": 220,
    "progress_bar": 190, "milestone_track": 210, "bar_chart": 360, "milestone_unlock": 240,
}
_PILL_EST_HEIGHT = 100
# Calendar.tsx's own default `width` prop — kept in sync manually (component
# default bumped 460->720, confirmed real user complaint: "biggen the
# calendar, that's one technique to remove empty space").
_CALENDAR_WIDTH = 720

# Phase 2 (arsenal expansion) constants.
# Reserved vertical space above a non-takeover chapter's content-zone stack
# for ZoneHeader (title+subtitle+divider) — takeover chapters don't need this,
# they get SectionLayer's own big centered header instead.
_ZONE_HEADER_HEIGHT = 130
# topic_card/step_list have no natural "animation complete" event to anchor
# an exit on (unlike gauge/countdown's fill animations) — fixed hold
# durations, same convention as CALENDAR_DISPLAY_FRAMES above.
TOPIC_CARD_DISPLAY_FRAMES = 150
STEP_LIST_STEP_HOLD_FRAMES = 120
CORNER_CARD_DISPLAY_FRAMES = 130
# xiaojin "arsenal" round 2 (2026-07-23): 6 new content-zone card types
# (ComparisonCard/RankedListCard/ChecklistCard/LocationPinCard/TestimonialCard/
# IconClusterCard) wired into the same generic dispatch/stacking machinery as
# topic_card/step_list — see the SYSTEM_PROMPT vocabulary entries and the
# _plan_comparison/_plan_ranked_list/etc. functions below _plan_step_list.
COMPARISON_DISPLAY_FRAMES = 180
RANKED_LIST_DISPLAY_FRAMES = 180
CHECKLIST_ITEM_HOLD_FRAMES = 120
LOCATION_PIN_DISPLAY_FRAMES = 150
TESTIMONIAL_DISPLAY_FRAMES = 180
ICON_CLUSTER_DISPLAY_FRAMES = 150
# xiaojin "arsenal" round 3 (2026-07-23): 6 more content-zone card types
# (ProgressBar/ProsCons/MilestoneTrack/TrustBadge/BarChart/MilestoneUnlock),
# same dispatch/stacking machinery as round 2 above.
PROGRESS_BAR_DISPLAY_FRAMES = 150
PROS_CONS_DISPLAY_FRAMES = 180
MILESTONE_TRACK_HOLD_FRAMES = 120
TRUST_BADGE_DISPLAY_FRAMES = 180
BAR_CHART_DISPLAY_FRAMES = 180
MILESTONE_UNLOCK_DISPLAY_FRAMES = 150


def _est_height(visual: str, entry: dict) -> int:
    if visual == "count_up":
        return 100 + 112 * len(entry.get("rows") or [])
    if visual == "step_list":
        return 40 + 95 * len(entry.get("steps") or [])
    if visual == "comparison":
        cols = entry.get("columns") or []
        max_items = max((len(c.get("items") or []) for c in cols), default=0)
        return 60 + 40 * max_items
    if visual == "ranked_list":
        return 50 + 68 * len(entry.get("items") or [])
    if visual == "checklist":
        return 30 + 68 * len(entry.get("items") or [])
    if visual == "icon_cluster":
        return 70 + 60 * math.ceil(len(entry.get("items") or []) / 3)
    if visual == "pros_cons":
        max_items = max(len(entry.get("pros") or []), len(entry.get("cons") or []), 0)
        return 50 + 34 * max_items
    if visual == "trust_badge":
        return 30 + 68 * len(entry.get("badges") or [])
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
1b. Intro title card: from the transcript, write an opening title card — "eyebrow": a 2-5 word UPPERCASE English tagline of what this video IS (e.g. "POLICY RENEWAL REMINDER", "WORKOUT PLAN", "PRODUCT REVIEW"); "title": the video's one-line headline in its primary spoken language (<=10 Chinese chars or <=6 English words); "subtitle": who/what it's from if the speaker names themselves/company, else a short secondary line. Ground every word in what the transcript actually says. Also pick "variant" — one of 4 confirmed intro treatments, chosen by topic tone (not a fixed default; vary it across jobs when the content genuinely calls for a different one):
   - "title_card" (safe default): a translucent dark scrim with the title over the speaker footage. Use for most videos, or when nothing below clearly fits better.
   - "stats_hook": dark full-bleed opener, "title" IS the giant impact stat itself (e.g. "100,000+" or "1.5x FASTER" — not a sentence), "eyebrow"/"subtitle" become two small supporting pill lines below it. Only when the video's actual hook is a striking number.
   - "title_impact": opaque cream/dark card, the single largest headline in the build. Use for a tips/list/"one big claim" video where one punchy line deserves to be the visual peak. Set "brand_label" (short, e.g. the speaker's channel/company name) if one is actually named; omit otherwise.
   - "chips": no title card at all — opens straight on the speaker, chapter labels build as small chips. Use for quick, casual, low-ceremony videos where a title card would feel like too much ceremony.
1c. Outro CTA: a closing call-to-action card matching what the SPEAKER actually asks or the video's natural close — "kicker": short UPPERCASE English, "headline": short line in primary language, "headline_accent": optional second line, "subtext": one supporting sentence, "cta_label": short imperative pill text (e.g. "留言告訴我！" / "Follow for more"). Do NOT invent contact info or promises the speaker never made.
2. Data Display Analysis: scan the transcript for visual-worthy moments — **this is NOT limited to numbers.** Flag BOTH of these categories, not just the first:
   (a) Hard data points, of ANY kind this video actually mentions — money, reps, distances, times, scores, percentages, quantities, counts, dates, countdowns, risks.
   (b) Non-numeric moments that are just as visualizable: a named app/tool/platform being used or referenced (e.g. "open WhatsApp", "send it to an AI Agent", "in this app"), an in-progress action (uploading, processing, generating, converting), a sequential process/workflow described step-by-step (even 2-3 steps — "you write X, send it to Y, get back Z"), or a claim/tip worth a graphic beat.
   A transcript can be ENTIRELY non-numeric (a product walkthrough, a workflow explanation, a tool demo) and still deserve several flagged moments — do not let "no numbers in this video" become "nothing to flag here." Only flag a moment if it's genuinely the point of that sentence (a dramatic reveal, a before/after comparison, a key stat, a warning, a concrete step in a process) — most videos have a handful of such moments, not one per sentence, but a video whose entire content IS a described workflow (like "send your script to an AI Agent, it builds your digital human, you upload the result") should have most of its steps flagged, via corner_card/step_list (see 3) — that content does not need a single number to be worth several visual beats. A number mentioned in passing that isn't the point of the sentence is NOT a data point, but this exemption does NOT apply to named tools/apps or process steps — those get flagged even without any numbers attached.
3. For each flagged moment, classify it into exactly the visual that fits it — do not default everything to count-up:
   - A countdown / time-remaining figure ("30 days left", "2 weeks to go", "one month until...") -> "countdown"
   - A specific calendar date ("July 28th", "by March 3rd", "expires on the 15th") -> "calendar"
   - A risk / negative-consequence framing (something that could go wrong, a warning, a "before it's too late", an "at risk" outcome) -> "gauge"
   - A single dramatic BEFORE/AFTER comparison of the SAME metric across two points in time (a cost/price/metric/quantity that jumped or dropped, e.g. "we used to spend $X, now we spend $Y", "went from 10 to 100") — genuinely the single richest moment in the video, not a routine number — -> "before_after". Only use this for a moment that really is a dramatic two-point comparison; most videos have zero of these.
   - Any other number worth calling out (money, reps, distances, times, scores, percentages, quantities, counts) -> "count_up"
   - A punchy spoken line worth showing as typography — the thesis, a strong claim, a memorable one-liner ("this changed everything", "never skip this step") -> "quote". Use this SPARINGLY and only for a line that's genuinely striking on its own — it takes over the full canvas and hides the speaker entirely, so it costs more than any other visual. NEVER use it for a greeting, self-introduction, or any routine/transitional sentence ("Hi there, it's David from...", "I've put the full breakdown in this video") — those are not quote-worthy just because they're the first thing said; skip them or use "topic_card" instead, which keeps the speaker visible. A data-less video should still get 1-2 quote moments at most (its actual best line, verbatim — never paraphrase or invent); videos with plenty of numeric visuals need 0-1.
   - A short supporting statement worth a graphic beat but with no hard data to visualize (a claim, a tip, an observation) -> "topic_card". This is the fallback for a moment that deserves SOME visual but isn't a number/date/risk/quote-worthy line — use it sparingly, only when the sentence is genuinely a distinct point, not for every line of dialogue.
   - A genuine sequential how-to/process with 2-5 distinct steps that ISN'T substantial enough to deserve the full-canvas animated timeline (3b below) — a quick routine, a short setup, an ordered list actually spoken as steps -> "step_list".
   - A moment referencing a specific named app/tool ("open WhatsApp", "in this app") or an in-progress action (uploading, processing, generating) where the speaker should stay visible and dominant rather than the canvas cutting to a full data reveal -> "corner_card".
   - Several distinct DIFFERENCES listed side by side across 2-3 named options ("old way vs new way", "plan A vs plan B", several attributes compared at once) -> "comparison". Distinct from before_after, which is only ONE metric across two points in time, not several attributes across options.
   - Several numbers/quantities being compared or ranked against EACH OTHER in the same beat ("top 3", "best X", "Y beats Z by...") -> "ranked_list". If it's just one number on its own with no explicit comparison, that's "count_up" instead.
   - A short list of items being confirmed/included/met one at a time (requirements met, features included, steps confirmed done) -> "checklist". Distinct from step_list, which reads as an ordered how-to ("first do X, then Y"), not a confirmation.
   - A specific place/city/country/region being named -> "location_pin".
   - A THIRD PARTY's words being quoted (a client, a reviewer, a colleague's remark — NOT the main speaker's own words) -> "testimonial". Distinct from "quote", which is reserved for the main speaker's own striking line.
   - Several related named things mentioned together with no ordering and no hard numbers (works with X/Y/Z, supports A/B/C) -> "icon_cluster". Distinct from step_list (ordered) and topic_card (one single statement).
   - A plain "how far along" completion figure with no risk or time-remaining framing ("we're 80% done", "4 of 5 steps complete") -> "progress_bar". Distinct from gauge (risk-framed) and countdown (time-remaining framed).
   - A decision or warning moment where BOTH the upside of one choice AND the downside of the other are spoken together (renew vs. lapse, do this vs. don't) -> "pros_cons". Distinct from comparison, which is a neutral side-by-side of attributes with no good/bad framing.
   - A short spoken history of 2-4 key dates/events for the same subject ("bought it in 2023, filed a claim in 2024, renewal's now") -> "milestone_track". Distinct from the full-canvas process_timeline (3b below), which is for a multi-stage PROCESS being explained, not a short past history.
   - The speaker states a credential, license, years of experience, or a trust-building stat about themselves/their business ("licensed agent", "8 years in the business", "10,000 clients served") -> "trust_badge". Only for moments building the SPEAKER'S OWN authority, not a client testimonial (that's "testimonial") or a generic count-up.
   - Multiple numbers being compared across categories that would naturally read as a CHART, not a ranked list (spending by year, scores by category) -> "bar_chart". Use "ranked_list" instead when the point is explicitly the RANKING (top 3, best X); use "bar_chart" when it's just magnitude-by-category with no ranking implied.
   - A single big number stated as a genuine achievement/scale moment worth celebrating ("we've protected 1,000 families", "reached 50,000 users") -> "milestone_unlock". Reserve this for a real celebratory beat, not a routine figure — most numbers are still "count_up".
3b. Multi-stage process timeline: if (and only if) the transcript describes a genuine multi-step SEQUENTIAL process with 3-5 distinct stages, each with its own duration/quantity ("first we do X for N weeks, then Y for M months, then Z..." — a workflow, a production pipeline, a plan with ordered phases), this deserves a full-canvas timeline graphic instead of a data card. It must correspond to exactly one chapter that you also mark "takeover": true (and "icon": null — the timeline fills that space instead) — set "process_timeline" to describe it, referencing that chapter's exact "label" text. This graphic now renders correctly in BOTH color modes (Fix D6, 2026-07-16) — set "dark" on that chapter based on the content's actual tone (a warning/dramatic process -> dark: true, a normal upbeat/neutral one -> dark: false), not as a requirement for the graphic to work. Most videos have none of this; only use it for a real ordered multi-stage process, not a simple list.
4. Output shape per visual type (all times in seconds, matching when that number/date is actually spoken):
   - count_up: {"visual":"count_up","title":"...","rows":[{"label":"short label in the video's primary language","label_en":"1-3 word UPPERCASE ENGLISH","seconds":12.3,"value":42,"prefix":"","divideBy":1,"decimals":0,"unit":"","tone":"accent|good|bad|normal","end_keyword":"the exact word/short phrase spoken right where this number's sentence finishes, e.g. \"8,400\" or \"premium\""}]} — group values that belong together into ONE card as multiple rows, not separate cards (but see "before_after" above for a two-point comparison of the SAME metric — that's richer as its own visual, not a two-row count_up card). prefix/divideBy/decimals/unit format the number so it's compact and readable (e.g. divideBy 1000000 + decimals 1 -> "1.5" for 1,500,000) — never a raw unformatted number.
   - gauge: {"visual":"gauge","seconds":12.3,"title":"...","leftLabel":"UPPERCASE","rightLabel":"UPPERCASE","value":0-1,"keyword":"the exact trigger word spoken right where this risk/consequence moment begins, e.g. \"policy\" from \"if your policy lapses\"","end_keyword":"the exact word spoken where that sentence/thought finishes, e.g. \"rate\" from \"...affect both your coverage and your rate\""} — leftLabel is the safe/good end, rightLabel is the risk/bad end, value is how far toward the risk end this moment lands (1.0 = fully at risk). keyword/end_keyword are what let the gauge appear and disappear exactly on the words, instead of an estimated timestamp — always include them for gauge.
   - countdown: {"visual":"countdown","seconds":12.3,"value":30,"unitLabel":"DAYS","label":"UPPERCASE short label","headline":"a short sentence","headlineAccent":"optional second line, e.g. the consequence","end_keyword":"the exact word spoken where this countdown's own sentence finishes, e.g. \"days\" from \"...renewal in 30 days\""}
   - quote: {"visual":"quote","seconds":12.3,"text":"the exact spoken line, verbatim, <=80 chars","attribution":"optional speaker name if they introduce themselves"}
   - calendar: {"visual":"calendar","seconds":12.3,"year":2026,"month":7,"targetDay":28,"eventLabel":"short label","end_keyword":"the exact word spoken where the date's own sentence finishes, e.g. \"July\" from \"...28th of July\""} — if the transcript doesn't state a year explicitly, infer the correct one using the reference date given in the user message (e.g. a date mentioned as still upcoming should resolve to this year or next, not a past year).
   - before_after: {"visual":"before_after","kicker":"short UPPERCASE English label for what's being compared, e.g. \"THE BUDGET\"","leftLabel":"UPPERCASE, e.g. \"2 YEARS AGO\"","leftSeconds":10.2,"leftValue":100,"leftPrefix":"$","leftSuffix":"K","rightLabel":"UPPERCASE, e.g. \"TODAY\"","rightSeconds":14.8,"rightValue":1.5,"rightPrefix":"$","rightSuffix":"M","rightDecimals":1} — leftSeconds/rightSeconds are when EACH value is actually spoken (often several seconds apart); prefix/suffix/rightDecimals format each number for display (e.g. rightDecimals 1 -> "1.5").
   - contact_cue: {"visual":"contact_cue","seconds":34.2} — the moment the speaker actually tells the viewer HOW to reach them (mentions WhatsApp/phone/QR code/email/"message me"/"contact me"). Only emit this if the video genuinely says something like that, at the exact second it's spoken. This does NOT invent or supply the actual contact info (that comes from elsewhere) — it ONLY marks the timing so the contact card appears exactly when it's being talked about, instead of only at a generic outro.
   - topic_card: {"visual":"topic_card","icon":"chat|lightbulb|check|sparkle","headline":"short statement in the video's primary language, <=60 chars","sub":"optional one-line supporting detail, <=80 chars","seconds":12.3,"keyword":"the exact trigger word this statement's moment begins on","end_keyword":"the exact word where this statement's sentence finishes"} — icon should loosely match the statement's nature (chat=communication/social, lightbulb=idea/insight, check=confirmation/good practice, sparkle=highlight/standout moment); default to "sparkle" if nothing fits better.
   - step_list: {"visual":"step_list","title":"optional short UPPERCASE label for the whole list","steps":[{"label":"short step name in the video's primary language","label_en":"optional 1-3 word UPPERCASE ENGLISH","seconds":12.3,"keyword":"the exact trigger word this specific step begins on"}, ...]} — 2-5 steps, each "seconds" is when THAT specific step is actually spoken (not all the same timestamp).
   - corner_card: {"visual":"corner_card","variant":"chat","seconds":12.3,"appName":"the actual app named, e.g. \"WhatsApp\"","message":"short mock message matching what's being discussed, <=60 chars"} for an app/messaging reference, OR {"visual":"corner_card","variant":"progress","seconds":12.3,"label":"short label, e.g. \"Uploading\"","percent":0-100} for an in-progress action.
   - comparison: {"visual":"comparison","seconds":12.3,"title":"optional short UPPERCASE label","columns":[{"label":"short label in the video's primary language","label_en":"1-3 word UPPERCASE ENGLISH","accent":"good|bad|neutral","items":["short item 1","short item 2", ...]}, ...]} — 2-3 columns, 2-4 items each, grounded in what's actually being compared.
   - ranked_list: {"visual":"ranked_list","seconds":12.3,"title":"...","items":[{"label":"short label in the video's primary language","label_en":"1-3 word UPPERCASE ENGLISH","value":42,"suffix":"%|"}, ...]} — 2-5 items, each a real number actually spoken, ordered largest/best first.
   - checklist: {"visual":"checklist","title":"optional short UPPERCASE label for the whole list","items":[{"label":"short item name in the video's primary language","label_en":"optional 1-3 word UPPERCASE ENGLISH","seconds":12.3}, ...]} — 2-6 items, each "seconds" is when THAT specific item is actually confirmed/mentioned (not all the same timestamp).
   - location_pin: {"visual":"location_pin","seconds":12.3,"place":"the place name in the video's primary language","place_en":"optional English form","sub":"optional one-line context, <=60 chars"}
   - testimonial: {"visual":"testimonial","seconds":12.3,"quote":"the exact quoted words, verbatim, <=100 chars","name":"who said it","role":"optional short role/context, e.g. \"Pacific Life client\""}
   - icon_cluster: {"visual":"icon_cluster","seconds":12.3,"title":"optional short UPPERCASE label","items":[{"icon":"chat|camera|play|star|bolt|heart","label":"short label in the video's primary language","label_en":"optional 1-3 word UPPERCASE ENGLISH"}, ...]} — 2-6 items; icon should loosely match each item's nature (chat=messaging/social, camera=recording/photo, play=media/video, star=highlight/favorite, bolt=speed/power/automation, heart=care/community) — default to "star" if nothing fits better.
   - progress_bar: {"visual":"progress_bar","seconds":12.3,"title":"optional short UPPERCASE label","label":"short label, e.g. \"Document review\"","percent":0-100,"sub":"optional supporting detail, <=60 chars"}
   - pros_cons: {"visual":"pros_cons","seconds":12.3,"title":"optional short label","pros_label":"short UPPERCASE label for the positive column, e.g. \"RENEW\"","cons_label":"short UPPERCASE label for the negative column, e.g. \"LAPSE\"","pros":["short item, grounded in what was actually said", ...],"cons":["short item, grounded in what was actually said", ...]} — 2-4 items per side.
   - milestone_track: {"visual":"milestone_track","seconds":12.3,"title":"optional short label","milestones":[{"label":"short label","sublabel":"optional short detail, e.g. a year","seconds":12.3}, ...]} — 2-4 milestones, each "seconds" is when THAT specific milestone is actually spoken.
   - trust_badge: {"visual":"trust_badge","seconds":12.3,"title":"optional short label","badges":[{"icon":"shield|star|award|clock","primary":"short credential/stat text","secondary":"short supporting label"}, ...]} — 1-3 badges.
   - bar_chart: {"visual":"bar_chart","seconds":12.3,"title":"...","items":[{"label":"short category name","value":42,"display_value":"optional pre-formatted, e.g. \"$40K\""}, ...]} — 2-4 categories, real numbers actually spoken.
   - milestone_unlock: {"visual":"milestone_unlock","seconds":12.3,"value":1000,"prefix":"optional, e.g. \"$\"","suffix":"optional, e.g. \"+\"","label":"short label, e.g. \"Families Protected\"","icon":"award|star|heart|bolt"}
4a. keyword/end_keyword: wherever a shape above lists them, they are optional but STRONGLY preferred whenever the moment has a clear trigger/conclusion word in the transcript — they anchor the graphic's entrance/exit precisely to the words being spoken (checked against the transcript's own timestamps) instead of relying on your estimated "seconds" being exactly right. Always copy the word exactly as it appears in the transcript (same capitalization/punctuation is fine, just don't paraphrase it). Omit only when there's genuinely no single clear word to point to.
4b. Accent pill: EVERY data point above (count_up/gauge/countdown/calendar/before_after) may additionally carry "pill": a short punchy takeaway line (<=44 chars, in the video's primary spoken language) shown as a full-width accent pill directly under that graphic — e.g. a coverage card's pill might be "Policy Active — Renew in 30 Days". It must be grounded in that exact sentence's content, never invented. Include it whenever the sentence has a natural takeaway (most do); omit only when nothing fits. These pills are how the canvas stays visually full, so prefer including one.
5. If the transcript has no genuinely dramatic/comparison-worthy moments AND no quote-worthy lines, return an empty data_points array. Do not invent one to fill the response, and do not force a domain's framing (financial, fitness, etc.) onto content that isn't actually about that.

Output ONLY valid JSON matching this shape, no markdown, no prose:
{
  "chapters": [{"at_seconds": 0, "label": "...", "label_en": "...", "takeover": false, "icon": null, "dark": false, "warn": false}],
  "intro": {"eyebrow": "...", "title": "...", "subtitle": "...", "variant": "title_card|stats_hook|title_impact|chips", "brand_label": "..." /* only for title_impact, omit otherwise */},
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


_PIPELINE_INTENT_SYSTEM = """You classify a user's WhatsApp message (attached to a video upload)
into exactly one of two categories. Respond with JSON only: {"intent": "clip-factory"} or
{"intent": "talking-head"}.

"clip-factory" = the user wants MULTIPLE independent short clips extracted from one long recording
for social posting — e.g. "cut this into some short clips", "give me a few TikTok clips from this",
"turn this webinar into shorts", "repurpose this for social media", "剪幾條短片", "幫我剪出幾條片",
"整幾條reels", "拆返幾條片出嚟". The signal is an explicit MULTIPLICITY / repurposing intent, not
just "make it shorter" — a bare "make it shorter" / "剪短一點" is a normal single-edit trim request,
NOT clip-factory, even though it also implies less footage.

"talking-head" = everything else: a normal single-video edit (trim, subtitles, filler removal,
music, b-roll, style/branding, or ambiguous/underspecified requests).

If genuinely ambiguous, answer "talking-head" — it is the existing, safer, cheaper default; a
clip-factory false positive costs the user a long wait and an unwanted batch of videos."""


def classify_pipeline_intent(edit_request: str) -> str:
    """用户发视频时附带的文字 -> "clip-factory" 还是 "talking-head"。

    失败关闭（fail closed）到 "talking-head"——这里"talking-head"才是那个便宜、
    快、符合预期的既有默认路径，误判成 clip-factory 会让用户平白等上大半小时、
    收到一堆不想要的视频；误判成 talking-head 顶多是用户需要把话说得更明确
    一点再试一次。
    """
    raw = _call_llm_json("管线意图分类", _PIPELINE_INTENT_SYSTEM, edit_request or "",
                         temperature=0.0)
    if isinstance(raw, dict) and raw.get("intent") == "clip-factory":
        return "clip-factory"
    return "talking-head"


def plan_content(segments: list[dict], duration: float, *, feedback: Optional[str] = None,
                  word_timestamps: Optional[list[dict]] = None,
                  deadline: Optional[float] = None) -> dict[str, Any]:
    """转写分段 -> 章节 + 四种图形的计划（已经是 frame 单位，可以直接喂给 XiaojinEditorial）。

    LLM 调用失败或没配 key 时，返回空计划——内容判断本来就是锦上添花，不应该
    因为它失败就搞垮整条剪辑流程。

    feedback: 视觉复审（qa_stills._vision_review）发现问题后，_op_apply_style
    重新规划一次时传入的具体问题描述——喂给同一个 LLM 调用，让它避开已知的
    错误（例如某张数据卡跟另一个元素挤在一起），而不是盲目重跑一次一模一样
    的判断。

    word_timestamps: 词级时间戳（可选）——给 Fix B/E 的入场/收尾关键词校准用
    （_ground_data_point_seconds），把 LLM 估计的 seconds 对齐到真正说出对应
    数字/关键词的那个词。没有词级时间戳时校准整体跳过，规划仍然产出。

    deadline: time.monotonic() 截止时间（可选，架构复审后新增，2026-07-24）。
    这个 criterion loop 本身跟 _op_apply_style 的 props_lint 循环、vision-QA
    触发的重规划是三层嵌套的（3×3=9 次 LLM 调用起步），真实事故实测过撞上
    DeepSeek 响应慢时能拖到 33 分钟。deadline 不改变任何质量判断逻辑——每轮
    该跑的检查一次不少——只是在轮次开始前先看一眼："还有没有时间做下一轮"，
    没有就直接走后面本来就有的 best-of 交付（跟轮数正常用尽时完全同一条路
    径），不会新引入任何质量下降，只是不再无止境地等一个已经很慢的外部 API。
    """
    empty = {
        "chapters": [], "data_cards": [], "gauges": [], "countdowns": [], "calendar_events": [],
        "before_after": [],
        "mode_schedule": [{"frame": 0, "mode": "dominant"}],
        "intro": None, "outro": None, "sections": [], "quotes": [], "contact_cue": None,
        "pills": [], "zone_headers": [], "step_lists": [], "topic_cards": [], "corner_cards": [],
        "comparisons": [], "ranked_lists": [], "checklists": [], "location_pins": [],
        "testimonials": [], "icon_clusters": [],
        "progress_bars": [], "pros_cons": [], "milestone_tracks": [], "trust_badges": [],
        "bar_charts": [], "milestone_unlocks": [],
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

    # ══════════════════════════════════════════════════════════════════════
    # 规划质量标准循环（criterion loop）——确认过的真实用户要求："ALL THE
    # VIDEOS SENT WILL HAVE A PLANNING TOWARDS HOW THE ANIMATIONS WILL GO,
    # NOT ONLY FOR THE DAVID VIDEO... KEEP LOOPING AND EXITING WHEN YOU'VE
    # FULFILLED THE CRITERION"。此前的结构是"单次 LLM 规划 + 零散的一次性
    # 补丁"（0 值单独重试一次、密度不足单独补规划一次），每类失败各管各的，
    # 一轮没修好就直接交付。现在统一成一个循环：每轮规划完成后跑同一组
    # *确定性*质量标准（_plan_quality_failures，纯函数、不靠 LLM 自评），
    # 全部通过才提前退出；没通过就把失败项原文喂给下一轮 LLM 重规划；
    # 轮数用尽则交付失败项最少的一轮（Fix A2 的 best-of 交付模式），并把
    # 0 值卡这种"宁缺勿错"的硬伤在交付前兜底剔除。
    #
    # 成本上限：每次 plan_content 最多 _PLAN_MAX_ATTEMPTS 次 LLM 调用——
    # 跟旧结构的最坏情况（主规划+0值重试+密度补规划 = 3 次）持平，只是把
    # 三次机会从"各自修一类问题"变成"每次都检查所有标准"。
    # ══════════════════════════════════════════════════════════════════════
    base_user_message = user_message
    best_plan: Optional[dict] = None
    best_raw: Optional[dict] = None
    best_failures: Optional[list[str]] = None
    for attempt in range(1, _PLAN_MAX_ATTEMPTS + 1):
        if deadline is not None and attempt > 1 and time.monotonic() >= deadline:
            logger.warning(
                f"content_planner: 总时长预算已用完，跳过第 {attempt}/{_PLAN_MAX_ATTEMPTS} 轮"
                f"（交付已有的最佳版本，而不是继续等外部 LLM）"
            )
            break
        raw = _call_llm_json(f"内容规划(第{attempt}轮)", SYSTEM_PROMPT, user_message,
                             temperature=0.2, model=get_config().llm_model_long_output)
        logger.debug(f"content_planner: 第{attempt}轮原始 data_points = "
                     f"{json.dumps((raw or {}).get('data_points'), ensure_ascii=False)}")
        if raw is None:
            continue  # LLM/JSON 整体失败（_call_llm_json 内部已重试过一次）——下一轮重来

        _ground_data_point_seconds(raw, word_timestamps)
        plan = _to_frame_plan(raw, duration)
        failures = _plan_quality_failures(raw, plan, duration, segments)
        if not failures:
            logger.info(f"content_planner: 规划质量标准全部通过（第 {attempt}/{_PLAN_MAX_ATTEMPTS} 轮）")
            return plan
        logger.warning(
            f"content_planner: 第 {attempt}/{_PLAN_MAX_ATTEMPTS} 轮未达标（{len(failures)} 项）: "
            + " | ".join(failures)
        )
        improved = best_failures is None or len(failures) < len(best_failures)
        if improved:
            best_plan, best_raw, best_failures = plan, raw, failures
        # 早退（架构复审后新增，2026-07-28，延迟优化）：跟 pipeline_runner 的
        # props_lint 早退是同一个判断——本轮反馈喂回去之后，失败项数量没有
        # 比已知最佳更少，说明 LLM 没有真正吸收反馈收敛，继续跑大概率是
        # 确定性空转（真实案例：job_7a33f9a80af8 第 3 轮原样复现了第 1 轮的
        # 失败项，第 2 轮已经换了别的失败项——第 3 轮没有新增任何价值，
        # best-of 交付结果跟提前在第 2 轮结束完全一样，白烧了一整轮 LLM
        # 调用，正是内容规划占掉整条流水线 70%+ 时间的主因之一）。best_plan
        # 已经保留，交付版本不变，只省掉注定拿不到更好结果的后续调用；仍在
        # 改进时（improved=True）不受影响，继续跑到轮数用尽或全部达标为止。
        if not improved and attempt < _PLAN_MAX_ATTEMPTS:
            logger.info(
                f"content_planner: 第 {attempt} 轮重规划未改进（仍是 {len(failures)} 项失败，"
                f"不少于已知最佳的 {len(best_failures)} 项）——提前结束重试，交付已知最佳版本"
            )
            break
        user_message = (
            "NOTE: your previous plan FAILED these quality criteria — you MUST fix ALL of them "
            "in this attempt (each one is checked mechanically, not judged):\n- "
            + "\n- ".join(failures)
            + "\n\n" + base_user_message
        )

    if best_plan is None or best_raw is None:
        return empty
    # 交付前兜底：0 值卡"宁缺勿错"，循环没修好也绝不带着 $0.0M 上屏（确认过
    # 的真实生产 bug，见 _zero_value_titles 的文档）。丢卡后重新映射一次
    # （raw 里的 seconds 已经在该轮循环内校准过，直接重映射即可）。
    still_bad = set(_zero_value_titles(best_raw))
    if still_bad:
        logger.warning(f"content_planner: 交付前剔除仍显示为 0 的卡片: {still_bad}")
        best_raw["data_points"] = [
            dp for dp in (best_raw.get("data_points") or [])
            if not (isinstance(dp, dict) and dp.get("visual") == "count_up"
                    and str(dp.get("title", "")) in still_bad)
        ]
        best_plan = _to_frame_plan(best_raw, duration)
    logger.warning(
        f"content_planner: {_PLAN_MAX_ATTEMPTS} 轮后仍未全部达标，交付失败项最少的一轮"
        f"（剩余 {len(best_failures or [])} 项）: " + " | ".join(best_failures or [])
    )

    # Fix C28：criterion 2（稀疏空档）如果还留着，试一次确定性兜底——见
    # _sparse_gap_quote_candidates 的完整说明。只在这里触发一次，不追加任何
    # LLM 调用；候选为空或没能真的减少失败项就原样放弃，不强行插入。
    gaps = _sparse_gaps(best_plan, duration)
    if gaps:
        quote_candidates = _sparse_gap_quote_candidates(gaps, segments)
        if quote_candidates:
            candidate_raw = dict(best_raw)
            candidate_raw["data_points"] = [*(best_raw.get("data_points") or []), *quote_candidates]
            _ground_data_point_seconds(candidate_raw, word_timestamps)
            candidate_plan = _to_frame_plan(candidate_raw, duration)
            candidate_failures = _plan_quality_failures(candidate_raw, candidate_plan, duration, segments)
            if len(candidate_failures) < len(best_failures or []):
                logger.info(
                    f"content_planner: 稀疏空档确定性兜底插入 {len(quote_candidates)} 条 quote"
                    f"（失败项 {len(best_failures or [])} -> {len(candidate_failures)}）（Fix C28）"
                )
                best_plan = candidate_plan
            else:
                logger.info("content_planner: 稀疏空档兜底候选未能减少失败项，放弃插入（Fix C28）")
        else:
            logger.info("content_planner: 稀疏空档里没有任何一句转写塞得进 QuoteCard 字数上限，放弃兜底（Fix C28）")

    return best_plan


# 数据点的 seconds 校准到实际说话时刻（timing FROM the dialogue）——确认过的
# 真实生产 bug（David 视频真实渲染）：countdown 卡片说的是"30 days"，LLM 给的
# seconds 估计是 4.7（对应还没说到"30"这个词就已经上场），但转写里"30"这个词
# 真正的时间戳是 7.5s——差了将近 3 秒，倒计时读起来像是凭空冒出来，而不是跟着
# 台词走。数字经过格式化转换后（如 1,500,000 -> "$1.5M"）LLM 对"这句话大概
# 几秒说的"这类估计尤其不准，但转写本身已经有精确到词的时间戳——没道理不用。
_GROUND_MATCH_WINDOW_SECONDS = 8.0


def _normalize_number_word(w: str) -> str:
    """去掉货币符号/千分位逗号/标点，只留数字和小数点，供跟候选数字字符串比较。

    句末的数字词经常自带结尾句号（转写里"$8,400.00."这样的词很常见——
    真实生产数据里出现过），只保留数字和小数点会留下一个多余的结尾句号
    （"8400.00."），导致后面 float() 解析失败、数值比较兜底完全用不上。
    句末句号 rstrip 掉——真正的小数点后面一定跟着数字，不会是字符串的
    最后一个字符，所以这个 rstrip 不会误伤合法小数。
    """
    return re.sub(r"[^\d.]", "", str(w or "")).rstrip(".")


def _number_candidates(value: Any, divide_by: Any = None, decimals: Any = None) -> set[str]:
    """一个数值可能在转写里以哪几种形式被说出/写出——生成候选字符串集合。

    既包含原始数值本身（LLM 有时候 seconds 估计得准，但也可能把原始数值和
    格式化后的显示数值搞混），也包含 divideBy/decimals 格式化后的显示值
    （例如 1500000 配 divideBy=1000000, decimals=1 -> "1.5"，对应转写里实际
    说的"one point five million"被 ASR 识别成的"1.5"）。
    """
    candidates: set[str] = set()
    try:
        v = float(value)
    except (TypeError, ValueError):
        return candidates
    if v == int(v):
        candidates.add(str(int(v)))
    candidates.add(str(v))
    div = None
    try:
        div = float(divide_by) if divide_by else None
    except (TypeError, ValueError):
        div = None
    if div:
        try:
            divided = v / div
            d = int(decimals) if isinstance(decimals, (int, float)) else 0
            formatted = f"{divided:.{d}f}"
            candidates.add(formatted)
            if d > 0:
                stripped = formatted.rstrip("0").rstrip(".")
                if stripped:
                    candidates.add(stripped)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return {c for c in candidates if c}


def _find_grounded_word(
    claimed_seconds: float,
    matches: Callable[[str], bool],
    word_timestamps: list[dict],
    window: float = _GROUND_MATCH_WINDOW_SECONDS,
    min_start: Optional[float] = None,
) -> Optional[dict]:
    """在 claimed_seconds 前后 window 秒内找一个跟 matches 谓词匹配的词，
    返回离 claimed_seconds 最近的那个词本身（整个 dict，含 start/end）——
    end_keyword 场景需要用到匹配词的结束时间，不只是起始时间。

    min_start（可选）：只考虑 start >= min_start 的候选词——收尾关键词
    (end_keyword) 的搜索必须锚定在入场时间"之后"，不然可能误配到转写里
    更早处出现的同一个词。入场关键词/数字候选的搜索不设这个下限（沿用
    Fix B 原本的双向最近匹配）。
    找不到就返回 None（调用方保持原值不变，不是报错）。
    """
    if not word_timestamps:
        return None
    best: Optional[dict] = None
    best_dist: Optional[float] = None
    for w in word_timestamps:
        try:
            start = float(w["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if min_start is not None and start < min_start:
            continue
        if not matches(str(w.get("word", ""))):
            continue
        dist = abs(start - claimed_seconds)
        if dist > window:
            continue
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = w
    return best


def _numeric_word_matches(norm_word: str, candidate_floats: set[float]) -> bool:
    """数字词的宽松匹配：转写里的数字词经常带小数点尾巴（ASR 把"$8,400"
    转出来常是"$8,400.00."），跟候选值做纯字符串比较会因为这几个多出来的
    ".00" 永远配不上——数值上明明是同一个数。转成 float 按数值比较一次
    作为兜底，不影响原本就能字符串匹配上的情况。"""
    if not norm_word or not candidate_floats:
        return False
    try:
        return float(norm_word) in candidate_floats
    except ValueError:
        return False


def _find_grounded_seconds(
    claimed_seconds: float, candidates: set[str], word_timestamps: list[dict]
) -> Optional[float]:
    """数字候选匹配——_find_grounded_word 的一个特化，只关心匹配词的起始
    时间。签名保持不变，Fix B 既有调用方/测试不用改。"""
    if not candidates:
        return None
    candidate_floats: set[float] = set()
    for c in candidates:
        try:
            candidate_floats.add(float(c))
        except ValueError:
            pass

    def _matches(w: str) -> bool:
        norm = _normalize_number_word(w)
        return norm in candidates or _numeric_word_matches(norm, candidate_floats)

    match = _find_grounded_word(claimed_seconds, _matches, word_timestamps)
    return match["start"] if match else None


def _keyword_search_word(keyword: Any, take: str = "first") -> Optional[str]:
    """keyword/end_keyword 字段可能是单个词，也可能是 LLM 给的短语（例如
    "if your policy"）——短语按空格切开后取其中一个词来搜，因为直接对整个
    短语做 _normalize_word_for_dup 会把空格也刮掉、粘成一个转写里根本不存在
    的长字符串，永远匹配不上。take="first" 给入场关键词用（越早越好，取
    短语的第一个词）；take="last" 给收尾关键词用（标记这句话说完，取短语的
    最后一个词）。"""
    if not keyword:
        return None
    parts = str(keyword).split()
    if not parts:
        return None
    raw = parts[0] if take == "first" else parts[-1]
    norm = _normalize_word_for_dup(raw)
    return norm or None


def _keyword_matches(keyword: Any, take: str = "first") -> Optional[Callable[[str], bool]]:
    """keyword/end_keyword 可能本身就是个数字（LLM 例子里的 end_keyword
    "8,400"）——这种情况下也顺带按数值比较兜底一次（见 _numeric_word_matches
    的理由：转写里的数字词常带小数点尾巴，纯文本匹配配不上）。"""
    target = _keyword_search_word(keyword, take=take)
    if not target:
        return None
    raw = str(keyword).split()
    raw_token = raw[0] if take == "first" else raw[-1] if raw else ""
    target_float: Optional[float] = None
    try:
        target_float = float(_normalize_number_word(raw_token))
    except ValueError:
        target_float = None
    candidate_floats = {target_float} if target_float is not None else set()

    def _match(w: str) -> bool:
        if _normalize_word_for_dup(w) == target:
            return True
        return _numeric_word_matches(_normalize_number_word(w), candidate_floats)

    return _match


def _ground_end_keyword(target: dict, end_keyword: Any, near_seconds: float, word_timestamps: list[dict]) -> None:
    """在 near_seconds（这个数据点已经校准过的入场时间）之后找 end_keyword
    对应的词，找到就把它的结束时间存进 target["_grounded_end_seconds"]——
    内部字段，不属于 LLM 输出 schema、也不会进最终 props（各 _plan_* 函数
    读它来决定"这段信息真正说完的时间"，取代固定时长的退场，见 Fix E3）。
    找不到就什么都不做，调用方沿用既有的固定时长退场逻辑。"""
    matcher = _keyword_matches(end_keyword, take="last")
    if not matcher:
        return
    w = _find_grounded_word(near_seconds, matcher, word_timestamps, min_start=near_seconds - 0.5)
    if w:
        target["_grounded_end_seconds"] = w["end"]


def _ground_data_point_seconds(raw: dict, word_timestamps: Optional[list[dict]]) -> None:
    """原地校准 raw["data_points"] 里每个数据点的 seconds（以及 count_up 各行
    自己的 seconds、before_after 的 leftSeconds/rightSeconds）到词级时间戳里
    真正说出对应数字/关键词的那个词——而不是全靠 LLM 自己估计的时间戳。没有
    词级时间戳时整体跳过（调用方允许 word_timestamps 为空/None）。

    数字类型（countdown/calendar/count_up/before_after）继续用数字候选匹配
    入场（Fix B，不变）。Fix E 在此基础上扩展：
    - gauge/topic_card/step_list 的每一步：有 LLM 提供的显式 "keyword" 字段时，
      用它匹配入场（这几类没有数字可以拿来对，之前完全没校准——确认过的
      真实用户反馈："if your policy lapses" 的风险仪表盘弹出得太晚）。
    - quote：不需要新字段，直接用它自己逐字照抄的 "text" 的第一个词/最后
      一个词分别校准入场/收尾——text 本来就是转写原句，天然就是锚点。
    - 任何带 "end_keyword" 的类型（countdown/calendar/count_up 各行/gauge/
      topic_card）：找到就把收尾词的结束时间记录到内部字段
      "_grounded_end_seconds"，供 _plan_* 函数决定真正的退场时机（Fix E3）
      ——取代"数字说完 90/150/160 帧之后再固定收起"的盲目做法（确认过的
      真实用户反馈：数据卡/倒计时/日历的停留时长要么远超实际信息说完的
      时刻，要么被相邻内容挤压到只剩一两秒）。
    """
    if not word_timestamps:
        return
    for dp in raw.get("data_points") or []:
        if not isinstance(dp, dict):
            continue
        visual = dp.get("visual") or "count_up"
        try:
            if visual == "countdown":
                claimed = float(dp["seconds"])
                grounded = _find_grounded_seconds(claimed, _number_candidates(dp.get("value")), word_timestamps)
                if grounded is not None:
                    dp["seconds"] = grounded
                _ground_end_keyword(dp, dp.get("end_keyword"), dp["seconds"], word_timestamps)
            elif visual == "calendar":
                claimed = float(dp["seconds"])
                grounded = _find_grounded_seconds(claimed, _number_candidates(dp.get("targetDay")), word_timestamps)
                if grounded is not None:
                    dp["seconds"] = grounded
                _ground_end_keyword(dp, dp.get("end_keyword"), dp["seconds"], word_timestamps)
            elif visual == "count_up":
                for row in dp.get("rows") or []:
                    if not isinstance(row, dict) or "seconds" not in row:
                        continue
                    claimed = float(row["seconds"])
                    cands = _number_candidates(row.get("value"), row.get("divideBy"), row.get("decimals"))
                    grounded = _find_grounded_seconds(claimed, cands, word_timestamps)
                    if grounded is not None:
                        row["seconds"] = grounded
                    _ground_end_keyword(row, row.get("end_keyword"), row["seconds"], word_timestamps)
            elif visual == "before_after":
                if "leftSeconds" in dp:
                    claimed = float(dp["leftSeconds"])
                    grounded = _find_grounded_seconds(claimed, _number_candidates(dp.get("leftValue")), word_timestamps)
                    if grounded is not None:
                        dp["leftSeconds"] = grounded
                if "rightSeconds" in dp:
                    claimed = float(dp["rightSeconds"])
                    grounded = _find_grounded_seconds(claimed, _number_candidates(dp.get("rightValue")), word_timestamps)
                    if grounded is not None:
                        dp["rightSeconds"] = grounded
            elif visual in ("gauge", "topic_card"):
                claimed = float(dp["seconds"])
                matcher = _keyword_matches(dp.get("keyword"), take="first")
                if matcher:
                    w = _find_grounded_word(claimed, matcher, word_timestamps)
                    if w:
                        dp["seconds"] = w["start"]
                _ground_end_keyword(dp, dp.get("end_keyword"), dp["seconds"], word_timestamps)
            elif visual == "step_list":
                for step in dp.get("steps") or []:
                    if not isinstance(step, dict) or "seconds" not in step:
                        continue
                    claimed = float(step["seconds"])
                    matcher = _keyword_matches(step.get("keyword"), take="first")
                    if matcher:
                        w = _find_grounded_word(claimed, matcher, word_timestamps)
                        if w:
                            step["seconds"] = w["start"]
            elif visual == "quote":
                text = str(dp.get("text", "")).strip()
                if text:
                    claimed = float(dp.get("seconds", 0) or 0)
                    first_matcher = _keyword_matches(text, take="first")
                    if first_matcher:
                        w = _find_grounded_word(claimed, first_matcher, word_timestamps)
                        if w:
                            dp["seconds"] = w["start"]
                    last_matcher = _keyword_matches(text, take="last")
                    if last_matcher:
                        ew = _find_grounded_word(
                            dp["seconds"] + 1.0, last_matcher, word_timestamps, min_start=dp["seconds"] - 0.5
                        )
                        if ew:
                            dp["_grounded_end_seconds"] = ew["end"]
            logger.debug(
                "content_planner: keyword grounding — visual=%s keyword=%r end_keyword=%r "
                "seconds=%.2f grounded_end=%s",
                visual, dp.get("keyword"), dp.get("end_keyword"), dp.get("seconds"),
                dp.get("_grounded_end_seconds"),
            )
        except (KeyError, TypeError, ValueError):
            continue


def _resolve_same_slot_overlaps(*element_groups: list[dict]) -> None:
    """Fix E2：见调用处的完整案例说明。对每一对(x,y)完全相同、mount/end 时间
    区间又重叠的元素，把后 mount 的那个顺延到前一个的 endFrame(+缓冲)之后，
    同时保持它自己原来的展示时长不变（只平移，不压缩）——mutates in place。
    只处理"完全同坑位"（精确同 x,y），不是任意矩形相交（那类更宽的重叠交给
    下游 props_lint 的 element_overlap 诊断，这里只处理"确定会被完全盖住"
    这一种最坏情况，可以无脑确定性修，不需要判断到底该谁让谁）。
    """
    elements = [
        e for group in element_groups for e in group
        if isinstance(e, dict) and "x" in e and "y" in e
        and "mountFrame" in e and "endFrame" in e
    ]
    elements.sort(key=lambda e: e["mountFrame"])
    for i, a in enumerate(elements):
        for b in elements[i + 1:]:
            if (a["x"], a["y"]) != (b["x"], b["y"]):
                continue
            if not (a["mountFrame"] < b["endFrame"] and b["mountFrame"] < a["endFrame"]):
                continue
            # a mounts no later than b (elements sorted by mountFrame) — b is
            # the one that would render on top and hide a, so b is the one
            # that yields.
            delay = (a["endFrame"] + _STACK_EXIT_BUFFER_FRAMES) - b["mountFrame"]
            if delay <= 0:
                continue
            duration_frames = b["endFrame"] - b["mountFrame"]
            b["mountFrame"] += delay
            b["endFrame"] = b["mountFrame"] + duration_frames
            if "secondRevealFrame" in b:
                b["secondRevealFrame"] += delay


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
    duration_frames = round(duration * FPS)

    def _chapter_end_for(raw_frame: int) -> int:
        """The end frame (next chapter's atFrame, or the video's own end) of
        whichever chapter `raw_frame` (a RAW, pre-MOUNT_LEAD spoken frame —
        not an already-adjusted mountFrame) falls inside. Mirrors
        `_plan_process_timeline`'s own section-span math exactly — that
        function's `chapter.atFrame -> next_chapter.atFrame` anchoring is
        the one visual type the user singled out as timed correctly
        ("the timeline was great"); this generalizes the same idea to every
        other visual so a graphic can't outlive the topic it illustrates.
        """
        if not chapters:
            return duration_frames
        idx = 0
        for i, ch in enumerate(chapters):
            if ch["atFrame"] <= raw_frame:
                idx = i
            else:
                break
        return chapters[idx + 1]["atFrame"] if idx + 1 < len(chapters) else duration_frames

    def _chapter_idx_for(raw_frame: int) -> int:
        if not chapters:
            return 0
        idx = 0
        for i, ch in enumerate(chapters):
            if ch["atFrame"] <= raw_frame:
                idx = i
            else:
                break
        return idx

    data_cards: list[dict] = []
    quotes: list[dict] = []
    gauges: list[dict] = []
    countdowns: list[dict] = []
    calendar_events: list[dict] = []
    before_afters: list[dict] = []
    contact_cues: list[dict] = []
    step_lists: list[dict] = []
    topic_cards: list[dict] = []
    corner_cards: list[dict] = []
    comparisons: list[dict] = []
    ranked_lists: list[dict] = []
    checklists: list[dict] = []
    location_pins: list[dict] = []
    testimonials: list[dict] = []
    icon_clusters: list[dict] = []
    progress_bars: list[dict] = []
    pros_cons: list[dict] = []
    milestone_tracks: list[dict] = []
    trust_badges: list[dict] = []
    bar_charts: list[dict] = []
    milestone_unlocks: list[dict] = []
    # One (chapter_idx, start, end) window per content-zone stack that
    # reserved header space (see _ZONE_HEADER_HEIGHT/stack_header_offset
    # below) — used after the main loop to emit one ZoneHeader PER STACK,
    # not one spanning the whole chapter. Confirmed real bug via a real
    # render (job_e44166eb8c38): a header spanning chapter-start ->
    # next-chapter-start stayed on screen long after its own stack's content
    # had exited, including through a later stretch where the SpeakerCard
    # had regrown to Dominant size (no workflow_range active there) — the
    # header rendered directly on top of the now-large, still-visible
    # facecam for ~7.7s. A header must live and die with the SAME stack
    # window that reserved its space, never longer.
    header_windows: list[tuple[int, int, int]] = []
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
    stack_chapter_idx = -1      # -1 = no open stack yet; never equals a real chapter index
    stack_chapter_end = duration_frames  # topic boundary the open stack must not outlive
    stack_is_quote = False      # quote passages hide the SpeakerCard (see _flush_stack)
    # Vertical space reserved above THIS stack for a ZoneHeader (0 for quote
    # passages and takeover chapters — see where this is set, below).
    stack_header_offset = 0

    def _flush_stack(next_mount: Optional[int] = None) -> None:
        """Close the current stack: all elements exit TOGETHER (the reference's
        accumulate-then-clear rhythm, and CLAUDE-v2.md's rule that a zone's
        previous occupants are fully gone before a new one mounts). The shared
        exit extends to just before the next passage starts — capped so a
        stack never lingers absurdly long past its own last beat — which is
        itself part of the empty-space fix: content persists instead of
        vanishing the moment its count-up settles.

        Also capped at `stack_chapter_end`: confirmed real bug (every visual
        type except the process-timeline one) — a fixed mountFrame+constant
        duration has zero awareness of the transcript, so a graphic could
        keep showing well after the dialogue had already moved to a new
        topic ("doesn't capture the transcription... couldn't time when to
        pull out"). Capping at the owning chapter's own end frame generalizes
        `_plan_process_timeline`'s already-correct `atFrame -> next atFrame`
        anchoring to every visual type. Ceiling only — never extends a stack
        past where it would already naturally end.
        """
        nonlocal stack, stack_used_h, stack_last_end
        if not stack:
            return
        end = min(stack_last_end, stack_chapter_end)
        if next_mount is not None:
            end = min(end, next_mount - _STACK_EXIT_BUFFER_FRAMES)
        # Fix C27（2026-07-20，真实生产复现——job_452ef6c48100，用户反馈"日历
        # 消失得太快"）：一个数据点如果恰好在自己章节快结束时才被关键词校准
        # 挂载（这条 calendar 的"28th of July"就是 DEADLINE 章节最后一句话），
        # 上面的章节边界裁剪会把它砍到只剩十几帧——比这份代码自己在别处认定
        # 的"最短可读时长"(_MIN_VISUAL_HOLD_FRAMES，45 帧/1.5s) 还短得多，观众
        # 根本来不及看清。章节边界裁剪本身是对的（不能让图形播到下一个话题
        # 里），这里只是补一条下限：裁剪结果绝不能比 _MIN_VISUAL_HOLD_FRAMES
        # 还短——真的没有 45 帧空间时（下一段紧跟着立刻要用这块地盘）才会
        # 无可奈何地保持原样，不会反过来抢占下一段本该有的地盘。
        floor = min(stack[0]["mountFrame"] + _MIN_VISUAL_HOLD_FRAMES, stack_last_end)
        if next_mount is not None:
            floor = min(floor, next_mount - _STACK_EXIT_BUFFER_FRAMES)
        end = max(end, floor)
        end = max(end, stack[0]["mountFrame"] + 1)  # never a non-positive/inverted duration
        for e in stack:
            e["endFrame"] = end
        # A quote passage is a full-canvas typographic moment (matches
        # motion/chris-quote): tag its workflow_ranges entry with the
        # section-pip sentinel so the existing event-scan mode-schedule
        # (_workflow_mode_schedule, pipeline_runner.py) hides the SpeakerCard
        # for exactly this span — confirmed real bug otherwise: a quote's
        # words rendered directly on top of the still-visible, still-large
        # speaker's face (job_24450b1eacfd / "preview(6)").
        width = SECTION_PIP_SENTINEL if stack_is_quote else _CONTENT_ZONE_WIDTH
        workflow_ranges.append((stack_start, end, width))
        if stack_header_offset:
            header_windows.append((stack_chapter_idx, stack_start, end))
        stack, stack_used_h, stack_last_end = [], 0, 0

    for dp in data_points:
        if not isinstance(dp, dict):
            # LLM 偶发在数组里塞非 dict 条目（实测出过 str）——跳过而不是
            # AttributeError 炸掉整个规划。
            continue
        visual = dp.get("visual") or "count_up"  # backward-compatible default
        dp_secs = _dp_seconds(dp)
        if not math.isfinite(dp_secs):
            # No usable "seconds"/rows[].seconds anywhere on this data point —
            # every _plan_* below needs that same field and would fail (and
            # get skipped) the same way via the try/except a few lines down.
            # Confirmed real production bug (David render, job_f0c3412a1694):
            # this used to fall straight into `round(_dp_seconds(dp) * FPS)`
            # unconditionally; `round(float("inf") * FPS)` raises OverflowError
            # (not KeyError/TypeError/ValueError), which isn't caught by that
            # try/except and crashed the entire apply_style op, degrading
            # delivery all the way down to remove_filler-only (zero
            # captions/graphics) instead of just dropping this one bad point.
            logger.warning(f"content_planner: 跳过一个没有可用时间戳的数据点 (visual={visual})")
            continue
        raw_frame = round(dp_secs * FPS)  # pre-MOUNT_LEAD spoken time, for chapter lookup
        dp_chapter_idx = _chapter_idx_for(raw_frame)
        if visual == "quote" and chapters and chapters[dp_chapter_idx].get("_takeover"):
            # Confirmed real bug (real David render): a quote landing inside
            # an already-active full-canvas section takeover rendered its own
            # typography moment (QuoteCard, default y=480) directly on top of
            # SectionLayer's title/icon — two different "own the whole
            # canvas" treatments fighting for the same space. The section
            # already IS this moment's visual (its own reveal + the captions
            # underneath carry the same spoken words); a quote card adds
            # nothing but a collision. Skip it entirely rather than render it.
            continue
        if visual == "corner_card":
            # Renders INSIDE the SpeakerCard (see CornerCard.tsx/SpeakerCard's
            # `children`), never in the content zone — doesn't participate in
            # the stacking system at all. Skip entirely during a takeover
            # chapter: the card is fully hidden then (opacityKeyframes), so
            # anything anchored to it would be invisible/wasted.
            if chapters and chapters[dp_chapter_idx].get("_takeover"):
                continue
            try:
                cc_entry = _plan_corner_card(dp)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(f"content_planner: 跳过一个解析失败的数据点 (visual=corner_card): {e}")
                continue
            if cc_entry is not None:
                corner_cards.append(cc_entry)
            continue
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
            elif visual == "step_list":
                entry, target = _plan_step_list(dp, min_mount), step_lists
            elif visual == "topic_card":
                entry, target = _plan_topic_card(dp, min_mount), topic_cards
            elif visual == "comparison":
                entry, target = _plan_comparison(dp, min_mount), comparisons
            elif visual == "ranked_list":
                entry, target = _plan_ranked_list(dp, min_mount), ranked_lists
            elif visual == "checklist":
                entry, target = _plan_checklist(dp, min_mount), checklists
            elif visual == "location_pin":
                entry, target = _plan_location_pin(dp, min_mount), location_pins
            elif visual == "testimonial":
                entry, target = _plan_testimonial(dp, min_mount), testimonials
            elif visual == "icon_cluster":
                entry, target = _plan_icon_cluster(dp, min_mount), icon_clusters
            elif visual == "progress_bar":
                entry, target = _plan_progress_bar(dp, min_mount), progress_bars
            elif visual == "pros_cons":
                entry, target = _plan_pros_cons(dp, min_mount), pros_cons
            elif visual == "milestone_track":
                entry, target = _plan_milestone_track(dp, min_mount), milestone_tracks
            elif visual == "trust_badge":
                entry, target = _plan_trust_badge(dp, min_mount), trust_badges
            elif visual == "bar_chart":
                entry, target = _plan_bar_chart(dp, min_mount), bar_charts
            elif visual == "milestone_unlock":
                entry, target = _plan_milestone_unlock(dp, min_mount), milestone_unlocks
            else:
                entry, target = _plan_count_up(dp, min_mount), data_cards
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"content_planner: 跳过一个解析失败的数据点 (visual={visual}): {e}")
            continue

        if entry is None:
            continue

        h = _est_height(visual, entry)
        solo = visual == "quote"  # QuoteCard is full-canvas typography, never stacked
        # 密度下限的补规划(_apply_richness_floor 里的 REPLAN 步骤，标记为
        # "_gap_fill")产出的数据点绝不能悄悄拼进前一段已有的堆叠——确认过的
        # 真实生产 bug(job_1237c9c59bc0)：LLM 这一轮没规划出日历/数据卡(0 值
        # 被丢卡+日历没生成)，countdown 就孤零零地留在堆叠里没人挤它退场，
        # 直到 11s 后补规划的内容因为在 8s 的 JOIN WINDOW 内、高度也够，直接
        # "拼"进了 countdown 的堆叠——两者被绑定共享同一个退场时间，倒计时
        # 活活多播了 12 秒，跟自己已经毫不相关的台词一起挂在画面上。补规划
        # 存在的意义就是"前面的内容已经讲完、这里本来是空档"——拼进上一段
        # 等于承认它跟上一段是同一件事，这在语义上就是错的：应该强制让前一段
        # 先完整退场，再开一段新的。
        is_gap_fill = bool(dp.get("_gap_fill"))
        # raw_frame/dp_chapter_idx already computed above (needed there for
        # the takeover-quote skip check before dispatch).
        # A new topic never joins the previous stack even if it would
        # otherwise fit in time/height — this is the mechanical form of
        # "follow the dialogue": a chapter change always starts a fresh
        # passage, matching how _plan_process_timeline is already scoped
        # to exactly one chapter.
        same_chapter = bool(stack) and dp_chapter_idx == stack_chapter_idx
        fits = (
            stack and not solo and not is_gap_fill and same_chapter
            and entry["mountFrame"] <= stack_last_end + _STACK_JOIN_WINDOW_FRAMES
            and stack_used_h + _STACK_GAP + h <= zone_h - stack_header_offset
        )
        if fits:
            entry_y = _CONTENT_ZONE_Y + stack_header_offset + stack_used_h + _STACK_GAP
            stack_used_h += _STACK_GAP + h
        else:
            # The outgoing stack must get a minimum on-screen life before the
            # zone clears for this new passage — otherwise a near-immediate
            # follow-up (e.g. a solo quote right after a card mounts) would
            # flush the card after only a few visible frames.
            if stack and entry["mountFrame"] < stack_start + 60:
                entry["mountFrame"] = stack_start + 60
                entry["endFrame"] = max(entry["endFrame"], entry["mountFrame"] + 90)
            # 确认过的真实用户反馈（反复出现，倒计时/日历/仪表盘/数据卡全中过）：
            # 前一段自己的收尾时间已经是关键词校准过的、跟着台词走的真实值了
            # (Fix E)，但下面 _flush_stack 一直是无脑地拿 next_mount-缓冲 去
            # 砍它，完全不管前一段本来想播到什么时候——"appeared it nicely,
            # but disappearing too fast"。既然两边现在都是从台词校准来的真实
            # 时间，冲突时优先保前一段播完，代价是让下一段稍微晚一点上场，而
            # 不是反过来砍前一段——但只在需要推迟的量不离谱时才这么做（见
            # _STACK_HANDOFF_MAX_DELAY_FRAMES 的注释），且只在这么做真的有效
            # 时才做（如果前一段自己已经会被章节边界砍短，推迟下一段毫无意义）。
            if stack and stack_last_end <= stack_chapter_end:
                needed_next_mount = stack_last_end + _STACK_EXIT_BUFFER_FRAMES
                delay = needed_next_mount - entry["mountFrame"]
                if 0 < delay <= _STACK_HANDOFF_MAX_DELAY_FRAMES:
                    entry_duration = entry["endFrame"] - entry["mountFrame"]
                    entry["mountFrame"] = needed_next_mount
                    entry["endFrame"] = entry["mountFrame"] + entry_duration
                    if "secondRevealFrame" in entry:  # before_after's own second-value beat
                        entry["secondRevealFrame"] += delay
            _flush_stack(next_mount=entry["mountFrame"])
            stack_start = entry["mountFrame"]
            stack_chapter_idx = dp_chapter_idx
            stack_chapter_end = _chapter_end_for(raw_frame)
            stack_is_quote = solo
            # Non-takeover chapters reserve header space for a ZoneHeader
            # (takeover chapters get SectionLayer's own big header instead;
            # quote passages are full-canvas typography, no header either).
            is_takeover_chapter = bool(chapters) and chapters[dp_chapter_idx].get("_takeover")
            stack_header_offset = 0 if (solo or is_takeover_chapter) else _ZONE_HEADER_HEIGHT
            entry_y = _CONTENT_ZONE_Y + stack_header_offset
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
            # 确认过的真实 bug（真实一跑 job_9923d959512d，props_lint 抓到）：这里
            # 漏加了 stack_header_offset——跟上面"fits"分支里 entry_y 的算法
            # (_CONTENT_ZONE_Y + stack_header_offset + stack_used_h + _STACK_GAP)
            # 不一致，导致 pill 少偏移了一整个 ZoneHeader 的高度(130px)，直接落
            # 在主图形自己的矩形里面，而不是主图形下方——日历/数据卡的配套
            # 文案条实际上跟自己的主图形重叠在一起。
            pill = {
                "text": pill_text[:48],
                "x": _CONTENT_ZONE_X, "width": _CONTENT_ZONE_WIDTH,
                "y": _CONTENT_ZONE_Y + stack_header_offset + stack_used_h + _STACK_GAP,
                "mountFrame": entry["mountFrame"] + 45,
                "endFrame": entry["endFrame"],
            }
            stack_used_h += _STACK_GAP + _PILL_EST_HEIGHT
            stack.append(pill)
            stack_last_end = max(stack_last_end, pill["endFrame"])
            pills.append(pill)

    _flush_stack()

    # 防御性清理：确认过的真实生产 bug——一个 stack 被下一个 stack 的
    # entrance 逼着提前收尾时（_STACK_EXIT_BUFFER_FRAMES 那道钳制），如果配
    # 套的 accent pill 自己的 mountFrame（主图形 mountFrame+45）恰好比这个被
    # 提前的共享 endFrame 还晚，就会产出 endFrame < mountFrame 的倒挂条目
    # （真实案例：日历图形后紧跟一张数据卡，两者间隔只有 63 帧，不够 45 帧
    # 的 pill 延迟 + 20 帧退场缓冲的 65 帧）。而不是精确预判每一种时序边界
    # 情况，这里做一次统一兜底：任何 endFrame<=mountFrame 的条目（不只是
    # pill，任何图形理论上都可能撞到同一类边界问题）直接整条丢弃——总比交付
    # 一张实际上零/负时长、渲染层直接隐形或报错的图形安全。
    for target_list in (data_cards, quotes, gauges, countdowns, calendar_events,
                        before_afters, contact_cues, pills, step_lists, topic_cards,
                        corner_cards, comparisons, ranked_lists, checklists,
                        location_pins, testimonials, icon_clusters,
                        progress_bars, pros_cons, milestone_tracks, trust_badges,
                        bar_charts, milestone_unlocks):
        target_list[:] = [
            e for e in target_list
            if e.get("endFrame", e.get("mountFrame", 0) + 1) > e.get("mountFrame", 0)
        ]

    # 全画布章节接管（sections）：takeover 章节的跨度默认是本章 atFrame 到下一章
    # atFrame（或片尾），但 Fix D1 会把它裁到"内容结束+2s 停留"和 8s 硬上限
    # 之内——章节本身可以很长，但说话人被隐藏的时间不能。接管期卡片停靠
    # （并入 workflow_ranges）。
    # duration_frames computed earlier (needed by _chapter_end_for above).
    timeline_plan = _plan_process_timeline(raw.get("process_timeline"), chapters, duration_frames)
    sections: list[dict] = []
    for idx, ch in enumerate(chapters):
        if not ch.get("_takeover"):
            continue
        start = ch["atFrame"]
        natural_end = chapters[idx + 1]["atFrame"] if idx + 1 < len(chapters) else duration_frames
        if natural_end - start < 60:  # 短于 2s 的章节不值得接管
            continue

        has_timeline = bool(timeline_plan and timeline_plan["chapter_index"] == idx)
        if has_timeline:
            # Timeline 图形本身就是这个接管唯一、贯穿全程的可视内容——保持
            # 章节原有跨度，不额外裁剪（_plan_process_timeline 自己已经是
            # atFrame -> next atFrame 锚定，跟这里裁剪的目的一致）。
            #
            # Fix E1（2026-07-16）：但"保持原有跨度"只是下限，不是上限——如果
            # 章节本身的自然跨度比"最后一个阶段揭示完 + 停留时间"还短，最后
            # 一个节点会在还没来得及被看清楚就被整个接管收走（真实反馈："if
            # you don't have time to put[the content in], then don't put and
            # just extend the previous section"）。改成够长就用自然跨度，
            # 不够长就顺延到最后一个节点有完整停留时间为止——只会往后延，
            # 从不往前砍，不影响本来就够长的正常情况。
            last_reveal = max((n["revealFrame"] for n in timeline_plan["nodes"]), default=start)
            end = max(natural_end, last_reveal + _TAKEOVER_CONTENT_HOLD_FRAMES)
        else:
            # Fix D1：确认过的真实生产 bug——WARNING 接管从 771 帧一路延伸到
            # 片尾(1526)，说话人被隐藏 25.2s 且再也没有恢复，而接管里唯一的
            # 图形(gauge)早在 1157 帧就已经结束，后面 12.3s 是纯黑背景配一个
            # 静止图标。接管跨度不该由章节长度决定，该由"这段时间里真正有
            # 图形内容"决定：内容结束后再停留 2s，同时不超过 8s 硬上限，也
            # 不超过下一章开始，三者取最小。完全没有图形内容的纯图标接管
            # （last_content_end 保持等于 start）退化成 start+2s 的短暂展示。
            last_content_end = start
            first_content_mount: Optional[int] = None
            for group in (gauges, countdowns, calendar_events, data_cards,
                          before_afters, pills, step_lists, topic_cards,
                          comparisons, ranked_lists, checklists, location_pins,
                          testimonials, icon_clusters,
                          progress_bars, pros_cons, milestone_tracks, trust_badges,
                          bar_charts, milestone_unlocks):
                for g in group:
                    g_end = g.get("endFrame", g.get("mountFrame", start))
                    if g["mountFrame"] < natural_end and g_end > start:
                        last_content_end = max(last_content_end, g_end)
                        if first_content_mount is None or g["mountFrame"] < first_content_mount:
                            first_content_mount = g["mountFrame"]

            # Fix D4（2026-07-16）：D1 只管接管什么时候收，没管接管什么时候开——
            # `start` 直接等于章节边界(atFrame)，但章节边界锚定的是"话题整体
            # 切换"的时刻，跟"第一条具体内容真正 mount"的时刻是两回事。真实
            # 生产 bug（dajaai-walking-fresh 测试实跑抓到）：WORKFLOW 接管
            # 174-414 帧，stepList 第一条 mountFrame=391——中间 217 帧(7.2s)
            # 说话人已经被接管隐藏，画面只有一个"WORKFLOW"标题，没有任何图形，
            # 纯死区。跟 last_content_end 用的是同一套"以真实内容为准"的逻辑，
            # 对称地处理 start：第一条内容比章节边界晚太多才出现时，把接管本身
            # 的开始往后推到"第一条内容 mount 前留 _SECTION_HEADER_LEAD_FRAMES
            # (2s) 入场时间"，而不是从章节边界就傻等。注意这只改变这个 section
            # 的 fromFrame（决定全画布接管/说话人隐藏几时开始），不改 chapters
            # 列表里的 atFrame——ChapterNav 该在原来的时刻高亮"WORKFLOW"依然
            # 高亮，只是那几秒画面还是正常 workflow 模式（说话人可见），不会
            # 提前跳进空荡荡的全画布接管。
            if first_content_mount is not None and first_content_mount - start > _SECTION_HEADER_LEAD_FRAMES:
                start = first_content_mount - _SECTION_HEADER_LEAD_FRAMES

            end = min(
                natural_end,
                last_content_end + _TAKEOVER_CONTENT_HOLD_FRAMES,
                start + _TAKEOVER_HARD_CAP_FRAMES,
            )
            if end - start < 60:
                continue  # 裁剪后短于 2s——保留章节本身，图形照常按普通 workflow 模式显示，不做接管

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
        if has_timeline:
            sec["timeline"] = {"heading": timeline_plan["heading"], "nodes": timeline_plan["nodes"]}
        sections.append(sec)
        workflow_ranges.append((start, end, SECTION_PIP_SENTINEL))

    # Fix D2：全片说话人被隐藏（接管+金句）的总时长预算——超过 30% 就摘除
    # 时长最长的接管，直到回到预算内（金句本身很短，从不摘除）。已知的小
    # 权衡：被摘除的章节此时已经错过了堆叠阶段的 ZoneHeader 预留（当时按
    # takeover 处理，没有预留 header 空间），摘除后这个章节会没有 ZoneHeader
    # ——比起说话人被隐藏一半时长，这是好得多的结果，缺一个装饰性标题不影响
    # 内容本身正常显示。
    #
    # 上面这条"权衡"假设了一件事：被摘除之后，这段时间的实际内容（数据卡/
    # 仪表盘等）还能在别处正常显示，只是少了个标题——对大多数 takeover 成立，
    # 因为那些图形本来就活在 gauges/data_cards 等独立列表里，接管只是给它们
    # 套了个全画布外壳。但 process_timeline 不是这样：它的可视内容(sec
    # ["timeline"])直接焊死在这个 section 对象本身上，sections.remove(...)
    # 整段删掉时，多阶段时间线的每一个阶段就跟着一起消失了，不像其它图形
    # 那样"还能在别处正常显示"——真实生产 bug (job_88b957f807b9 /
    # job_0aaef74e8865，MrBeast 视频)：一段讲"从立项到上线要五个月"的多阶段
    # 时间线占了全片 79% 的时长，超预算后被整段摘除，观众看到的 18 秒里
    # 只有字幕，连一个阶段的数字都没露出来过。时间线的每个节点本来就是
    # 按自己的 revealFrame 逐步揭示的，裁到跟普通接管一样的 8s 硬上限（而不
    # 是整段摘除）仍然能看到开头几个阶段自然地播出来，这比什么都不剩要好
    # 得多；只有裁到硬上限仍然超预算（意味着这一段已经不能再短了）才退回
    # 整段摘除。
    quote_hidden_total = sum(q["endFrame"] - q["mountFrame"] for q in quotes)
    while sections and duration_frames > 0:
        section_hidden_total = sum(s["toFrame"] - s["fromFrame"] for s in sections)
        if (quote_hidden_total + section_hidden_total) / duration_frames <= _HIDDEN_BUDGET_FRACTION:
            break
        longest = max(sections, key=lambda s: s["toFrame"] - s["fromFrame"])
        # Fix D5（2026-07-16）：之前这里无论视频多长都把 timeline 接管先裁到
        # 固定的 8s 硬上限，再检查预算——但 8s 本身就已经是 26.7s 以内任何
        # 视频总时长的 30%+，裁完第一轮仍然超预算，第二轮 capped_end 跟
        # toFrame 相等（8s 裁无可裁），直接落到下面的整段摘除。真实生产 bug
        # （MrBeast backtest, job_95e1e08b0995, 23.6s 视频）：TIMELINE 接管
        # 在全部 4 次 content_planner 调用里都被摘除，从未播出过一次——不是
        # 运气不好，是 8s 硬上限在这个时长的视频上数学上不可能进 30% 预算，
        # 不管重规划多少轮结果都一样。改成不先假设 8s，直接算这一段实际还
        # 有多少预算可用（总预算减去其它接管+金句已经占用的部分），只要不
        # 低于 TIMELINE_MIN_SECTION_FRAMES（够放 2 个阶段）就用这个真实可用
        # 长度，短视频也能留下几秒真正的时间线动画，而不是要么完整 8s 要么
        # 一帧没有。
        if "timeline" in longest:
            other_hidden = quote_hidden_total + sum(
                s["toFrame"] - s["fromFrame"] for s in sections if s is not longest
            )
            budget_frames = round(_HIDDEN_BUDGET_FRACTION * duration_frames) - other_hidden
            allowed = min(
                longest["toFrame"] - longest["fromFrame"],
                max(TIMELINE_MIN_SECTION_FRAMES, budget_frames),
            )
            capped_end = longest["fromFrame"] + allowed
        else:
            capped_end = longest["fromFrame"] + _TAKEOVER_HARD_CAP_FRAMES
        if "timeline" in longest and capped_end < longest["toFrame"]:
            old_end = longest["toFrame"]
            longest["toFrame"] = capped_end
            for i, r in enumerate(workflow_ranges):
                if r[0] == longest["fromFrame"] and r[1] == old_end and r[2] >= SECTION_PIP_SENTINEL:
                    workflow_ranges[i] = (r[0], capped_end, r[2])
            logger.warning(
                f"content_planner: 隐藏时长预算超限(>{_HIDDEN_BUDGET_FRACTION:.0%})，"
                f"时间线接管 '{longest.get('title')}' 裁到预算允许的实际长度而不整段摘除 "
                f"({longest['fromFrame']}-{old_end} -> {longest['fromFrame']}-{capped_end})"
            )
            continue
        sections.remove(longest)
        workflow_ranges[:] = [
            r for r in workflow_ranges
            if not (r[0] == longest["fromFrame"] and r[1] == longest["toFrame"] and r[2] >= SECTION_PIP_SENTINEL)
        ]
        logger.warning(
            f"content_planner: 隐藏时长预算超限(>{_HIDDEN_BUDGET_FRACTION:.0%})，"
            f"摘除接管 '{longest.get('title')}' ({longest['fromFrame']}-{longest['toFrame']})"
        )

    # Fix D3（2026-07-16）：D1/D2 只管"总隐藏时长占比"和"单段接管别拖太长"，
    # 都没有专门检查"片尾前有没有把说话人放回来"——一个接管即使总时长在
    # 30% 预算内、单段也没超过 8s 硬上限，仍然可能刚好卡到视频最后一帧结束，
    # 观众看到的结尾永远是接管画面，说话人再也没露过面。这类 bug 一直是
    # props_lint 的 facecam_never_restored 在真实 job 上抓到的（job_88b957f807b9
    # / job_0aaef74e8865，MrBeast 视频：最后一个接管从第 478 帧一路延伸到片尾
    # 第 685 帧，占了全部隐藏时长），但 props_lint 只是下游诊断，这里补上根因
    # 修复：预算循环结束后，任何一路延伸到片尾（留 1 帧容差，跟 props_lint 判
    # 定口径一致）的接管，都把 toFrame 往回收 _TAKEOVER_CONTENT_HOLD_FRAMES
    # （2s，复用 D1 收内容尾巴的同一个 hold 常量，不是新发明数字），让说话人
    # 在结尾前有恢复的余地。接管本来就短、收不出 2s 恢复窗口的（<60 帧）保持
    # 原样——跟 D1 的短接管保留判断同一个门槛，收窄一个已经很短的区间没有
    # 意义。金句(quotes)不受影响：金句短、作为收尾陈述留到片尾是预期效果，
    # D2 的预算循环也从不摘除它，这里保持同样的例外。
    for sec in sections:
        if sec["toFrame"] < duration_frames - 1:
            continue
        old_end = sec["toFrame"]
        new_end = duration_frames - _TAKEOVER_CONTENT_HOLD_FRAMES
        if new_end - sec["fromFrame"] < 60:
            continue
        sec["toFrame"] = new_end
        for i, r in enumerate(workflow_ranges):
            if r[0] == sec["fromFrame"] and r[1] == old_end and r[2] >= SECTION_PIP_SENTINEL:
                workflow_ranges[i] = (r[0], new_end, r[2])
        logger.warning(
            f"content_planner: 接管 '{sec.get('title')}' 一路延伸到片尾，说话人从未恢复，"
            f"收回 {_TAKEOVER_CONTENT_HOLD_FRAMES / FPS:.0f}s 露出结尾 "
            f"({sec['fromFrame']}-{old_end} -> {sec['fromFrame']}-{new_end})"
        )

    # ZoneHeader: the content-zone equivalent of SectionLayer's big header —
    # one entry PER STACK WINDOW that reserved header space (header_windows,
    # populated by _flush_stack while stacking), not one per chapter. A
    # header only exists while its OWN stack's content is on screen; when a
    # chapter has multiple separate content passages (a gap forced a
    # flush-and-restart), each passage gets its own header window, and the
    # header is simply absent during the gap between them — including any
    # stretch where the SpeakerCard has regrown to Dominant size.
    zone_headers: list[dict] = []
    for idx, start, end in header_windows:
        if idx >= len(chapters):
            continue
        ch = chapters[idx]
        if not ch.get("label"):
            continue
        # Floored at the chapter's own atFrame and capped at the next
        # chapter's: confirmed real bug via a real David render — the first
        # stack's mountFrame already gets pulled MOUNT_LEAD_FRAMES earlier
        # than its raw spoken frame, and when that raw frame sits close
        # enough to the chapter boundary, the adjusted mountFrame lands
        # BEFORE atFrame — i.e. still inside the PREVIOUS chapter. When that
        # previous chapter is a takeover, its own SectionLayer is still fully
        # on screen and this chapter's header popped in on top of it
        # ("CONTACT" appearing while "RISK"'s dark takeover icon was still
        # showing). A header can never make sense before its own chapter has
        # technically started, or after the next one has.
        next_atframe = chapters[idx + 1]["atFrame"] if idx + 1 < len(chapters) else duration_frames
        zh_start = max(start, ch["atFrame"])
        zh_end = min(end, next_atframe)
        if zh_end <= zh_start:
            continue
        # y = _CONTENT_ZONE_Y exactly (not further up): the shrunk Workflow
        # card ends at y=1004, only 36px above _CONTENT_ZONE_Y(1040) — there's
        # no room for a header ABOVE 1040 without painting over the still-
        # visible card (confirmed via stills: an earlier y=910 default
        # rendered "STEPS" directly on top of the docked card/CornerCard).
        # The header occupies the FIRST _ZONE_HEADER_HEIGHT px of the zone;
        # stack_header_offset already pushes the actual cards below that.
        zh: dict[str, Any] = {
            "title": ch["label"], "fromFrame": zh_start, "toFrame": zh_end,
            "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y,
        }
        if ch.get("labelEn"):
            zh["titleEn"] = ch["labelEn"]
        zone_headers.append(zh)

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
        (g for g in (data_cards + gauges + countdowns + calendar_events + quotes + before_afters
                      + step_lists + topic_cards + comparisons + ranked_lists + checklists
                      + location_pins + testimonials + icon_clusters
                      + progress_bars + pros_cons + milestone_tracks + trust_badges
                      + bar_charts + milestone_unlocks)),
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

    intro = None
    ri = raw.get("intro")
    if isinstance(ri, dict) and ri.get("title"):
        intro = {"eyebrow": str(ri.get("eyebrow", ""))[:40].upper(),
                 "title": str(ri["title"])[:36],
                 "subtitle": str(ri.get("subtitle", ""))[:40]}
        # Fix F1（2026-07-16）：intro 的 4 种视觉变体（video-studio
        # CLAUDE-xiaojin-editorial.md 的 4 个 intro pattern，之前只有 Pattern
        # 2/title_card 被移植过来）——LLM 说了个不认识的值就退回默认，不让
        # 一个格式错误的字段直接让整条 render props 校验失败。
        variant = str(ri.get("variant", "title_card")).strip().lower()
        if variant not in ("title_card", "stats_hook", "title_impact", "chips"):
            variant = "title_card"
        intro["variant"] = variant
        if variant == "title_impact" and ri.get("brand_label"):
            intro["brandLabel"] = str(ri["brand_label"])[:24]
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

    # Fix E2（2026-07-16）：确定性的"同坑位撞车"兜底——真实生产 bug
    # (job_73e873e4f7e1)：一张 dataCard("Total Duration: 5 months") 跟一张
    # topicCard 的 x/y 完全相同、存活区间也重叠，而 topicCard 在组件渲染顺序
    # 里排在 dataCard 后面（画在上面），dataCard 整个存活期间完全不可见——
    # 不是"挤在一起看着乱"，是这张卡的内容观众从头到尾一次都没看到过。
    # 堆叠系统（上面的 _flush_stack 逻辑）理论上不该让这种情况发生，但真实
    # 数据证明它确实发生了——不去继续深挖堆叠系统内部为什么在这个具体案例
    # 里失手，而是加一道谁都绕不过去的最终兜底：扫一遍所有内容区元素，
    # 精确同一个 (x,y) 坐标、时间又重叠的两个，一定是后来者完全盖住前者，
    # 把后来者顺延到前者退场之后——用户的原话就是这个原则："if you don't
    # have time to put[content in], then don't put and just extend the
    # previous section"，这里反过来说：宁可把新内容往后推，也不要让旧内容
    # 被悄无声息地盖住。这是最后一道保险，跟 props_lint 的 element_overlap
    # 诊断互补：那边发现问题喂回 LLM 重规划（可能好也可能不好），这里直接
    # 确定性地修好，不依赖任何一轮重规划恰好避开这个坑。
    _resolve_same_slot_overlaps(
        data_cards, gauges, countdowns, calendar_events, before_afters,
        pills, step_lists, topic_cards,
        comparisons, ranked_lists, checklists, location_pins, testimonials, icon_clusters,
        progress_bars, pros_cons, milestone_tracks, trust_badges, bar_charts, milestone_unlocks,
    )

    # Fix C31（2026-07-20，真实生产复现——用户直接从交付的成片里抓到：WORKFLOW
    # 分区标题和一张 WhatsApp corner_card 都被一个长回 Dominant 尺寸的
    # SpeakerCard 压在下面/背后）：mode_schedule 本该在这里用 workflow_ranges
    # 算一次，但 _resolve_same_slot_overlaps 会顺延 topic_card/step_list 的
    # mountFrame（E2 的"后来者让路"规则）——如果在这一步之前就把 mode_schedule
    # 冻结住，被顺延卡片真正上屏的窗口会比 workflow_ranges 当初算的更晚开始，
    # 中间空出一段"内容其实已经在画面上，但 mode_schedule 还以为没有"的间隙，
    # SpeakerCard 就在这段间隙里长回满尺寸，正好压在标题/卡片下面。在这里
    # （所有顺延都结束之后）用每个内容区图形*当前*（最终）的 mountFrame/
    # endFrame 重新扫一遍，跟原始 workflow_ranges（保留 sections/quote 的
    # 隐藏语义，那部分不是从这些 list 来的）取并集再重算——不管是哪一步顺延
    # 导致的错位，这里都能补上，跟 Fix C24/C30 同一个"给保证，不只是查"的原则。
    final_workflow_ranges = list(workflow_ranges)
    for _items in (data_cards, gauges, countdowns, calendar_events, before_afters,
                   pills, step_lists, topic_cards,
                   comparisons, ranked_lists, checklists, location_pins, testimonials, icon_clusters,
                   progress_bars, pros_cons, milestone_tracks, trust_badges, bar_charts, milestone_unlocks):
        for _it in _items:
            if "mountFrame" in _it and "endFrame" in _it:
                final_workflow_ranges.append((_it["mountFrame"], _it["endFrame"], _CONTENT_ZONE_WIDTH))
    dedup = _workflow_mode_schedule(final_workflow_ranges, round(duration * FPS))

    return {
        "chapters": chapters, "data_cards": data_cards, "gauges": gauges,
        "countdowns": countdowns, "calendar_events": calendar_events, "before_after": before_afters,
        "mode_schedule": dedup,
        "intro": intro, "outro": outro, "sections": sections,
        "quotes": quotes, "contact_cue": contact_cues[0] if contact_cues else None,
        "pills": pills, "zone_headers": zone_headers, "step_lists": step_lists,
        "topic_cards": topic_cards, "corner_cards": corner_cards,
        "comparisons": comparisons, "ranked_lists": ranked_lists, "checklists": checklists,
        "location_pins": location_pins, "testimonials": testimonials, "icon_clusters": icon_clusters,
        "progress_bars": progress_bars, "pros_cons": pros_cons, "milestone_tracks": milestone_tracks,
        "trust_badges": trust_badges, "bar_charts": bar_charts, "milestone_unlocks": milestone_unlocks,
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

    # Fix C3：合并短暂的 dominant "抖动"。两段内容之间隔得不够近，没能被上面
    # 的堆叠系统合并成一个连续段落，但也没远到需要卡片真的长回 Dominant 再
    # 缩回去——卡片会在这段极短间隙里长大又立刻缩小，画面上是一次没有意义的
    # 鼓包（确认过的真实场景：两张图形之间只隔了 10 帧）。把短于
    # _DOMINANT_JITTER_MERGE_FRAMES 的 dominant 间隙桥接掉，卡片保持连续收起。
    merged: list[dict] = [dedup[0]] if dedup else []
    i = 1
    while i < len(dedup):
        entry = dedup[i]
        is_short_blip = (
            entry["mode"] == "dominant"
            and i + 1 < len(dedup)
            and (dedup[i + 1]["frame"] - entry["frame"]) < _DOMINANT_JITTER_MERGE_FRAMES
        )
        if is_short_blip:
            i += 1  # 跳过这次抖动本身，不放进最终结果
            nxt = dedup[i]
            if nxt["mode"] == merged[-1]["mode"] and nxt.get("contentWidth") == merged[-1].get("contentWidth"):
                i += 1  # 桥接后如果紧接着那段跟抖动前完全一样，一并跳过，避免冗余 entry
            continue
        merged.append(entry)
        i += 1
    return merged


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
    if visual == "step_list":
        # step_list has no top-level "seconds" — its timing lives entirely in
        # each step's own "seconds" (see _plan_step_list).
        secs: list[float] = []
        for s in dp.get("steps") or []:
            try:
                secs.append(float(s["seconds"]))
            except (KeyError, TypeError, ValueError):
                continue
        return min(secs) if secs else float("inf")
    if visual == "checklist":
        # Same shape as step_list — no top-level "seconds", each item ticks
        # on its own spoken beat (see _plan_checklist).
        secs: list[float] = []
        for it in dp.get("items") or []:
            try:
                secs.append(float(it["seconds"]))
            except (KeyError, TypeError, ValueError):
                continue
        return min(secs) if secs else float("inf")
    if visual == "milestone_track":
        # Same shape as step_list/checklist — no top-level "seconds", each
        # milestone has its own spoken beat (see _plan_milestone_track).
        secs: list[float] = []
        for it in dp.get("milestones") or []:
            try:
                secs.append(float(it["seconds"]))
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
    grounded_ends = []
    ungrounded_row_frames = []
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
        # Fix E3 (+regression fix): 任何一行有关键词校准过的收尾时间时，用它
        # （+ 停留缓冲）参与决定退场，而不是"最后一行说完再固定停留 90 帧"的
        # 盲目做法——确认过的真实用户反馈：coverage/premium 这张卡片停留的
        # 时间比"$8,400"说完之后应有的还要长得多。
        # 但是：只用"有校准的几行"取 max 会漏掉"没校准的那一行"——真实一跑
        # 就翻车了（job_b943ce1d3606）：Coverage 行校准收尾很早，Premium 行
        # 没有拿到 end_keyword，结果卡片在 Premium 这一行自己的出场帧之前就
        # 已经 endFrame 判定退场，数字还没滚动就被判定该收起来了。所以没校
        # 准的行也要用"它自己的出场帧 + 动画时长 + 停留"参与同一个 max，防
        # 止被更早收尾的另一行顶掉。
        if isinstance(r, dict) and r.get("_grounded_end_seconds") is not None:
            # Fix C26（2026-07-20，真实生产复现——job_452ef6c48100，用户截图
            # 抓到 $8,400 这一行数字滚动到一半、还没滚完就被卡片收起切掉）：
            # 校准过的收尾时间只反映"这个词说完的那一帧"+ 停留缓冲，跟这一行
            # 自己的动画有没有播完是两回事——"$8,400"这种短语音说得很快，
            # 说完 + 停留缓冲常常比它自己从 mountOffset 起跳需要的
            # _COUNT_UP_ROW_ANIM_FRAMES(40 帧) 更早。未校准的行已经用
            # `row_frame + _COUNT_UP_ROW_ANIM_FRAMES` 兜底过这一点（见下面
            # ungrounded 分支），但校准过的行反而没有这层保护，白白让"说得快"
            # 的行提前把整张卡片收走。这里两个下限都算，取较大值。
            grounded_ends.append(max(
                round(r["_grounded_end_seconds"] * FPS) + _KEYWORD_EXIT_HOLD_FRAMES,
                row_frame + _COUNT_UP_ROW_ANIM_FRAMES,
            ))
        else:
            ungrounded_row_frames.append(row_frame)
    if not rows:
        return None
    last_row_frame = round(max(row_seconds) * FPS)
    if grounded_ends:
        candidates = list(grounded_ends)
        if ungrounded_row_frames:
            candidates.append(max(ungrounded_row_frames) + _COUNT_UP_ROW_ANIM_FRAMES + _KEYWORD_EXIT_HOLD_FRAMES)
        end_frame = max(card_mount_frame + _MIN_VISUAL_HOLD_FRAMES, max(candidates))
    else:
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


def _plan_topic_card(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Icon + short statement card — the "arsenal" fallback for a spoken
    supporting line with no hard data to visualize (no number/date/risk),
    so it still gets a graphic beat instead of sitting as a plain caption."""
    headline = str(dp.get("headline", "")).strip()
    if not headline:
        return None
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    # Fix E3 补漏：E1(prompt)/E2(_ground_data_point_seconds) 早就把 topic_card
    # 当成会校准 keyword/end_keyword 的类型在处理了(_grounded_end_seconds 也
    # 确实算出来了)，但当时 E3 这一步漏掉了 _plan_topic_card 本身——校准结果
    # 算了却没人读，白算。topic_card 现在还要接手机械兜底的填空档职责（原来
    # 用 quote，用户反馈说太像"没诚意的大段文字"且会挡脸，见 _fallback_
    # quotes_for_gaps 的调用方），时机精度更要紧了，照其它 _plan_* 函数的
    # 样子接上。
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + TOPIC_CARD_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "headline": headline[:60],
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("icon") in ("chat", "lightbulb", "check", "sparkle"):
        entry["icon"] = dp["icon"]
    if dp.get("sub"):
        entry["sub"] = str(dp["sub"])[:80]
    return entry


def _plan_step_list(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Ghosted numbered step skeleton — each row activates at its own spoken
    beat (`activateOffset`, relative to the card's own mountFrame), matching
    dajaai-walking-fresh's reference vocabulary. For a genuine sequential
    how-to/process too light for the full-canvas dark timeline
    (_plan_process_timeline)."""
    steps_in = dp.get("steps") or []
    if len(steps_in) < 2:
        return None
    step_seconds: list[float] = []
    for s in steps_in:
        if not isinstance(s, dict):
            return None
        step_seconds.append(float(s["seconds"]))
    card_mount_frame = max(min_mount_frame, round(min(step_seconds) * FPS) - MOUNT_LEAD_FRAMES)
    steps = []
    for s, sec in zip(steps_in, step_seconds):
        step_frame = round(sec * FPS)
        step: dict[str, Any] = {
            "label": str(s.get("label", ""))[:24],
            "activateOffset": max(0, step_frame - card_mount_frame),
        }
        if s.get("label_en"):
            step["labelEn"] = str(s["label_en"])[:28]
        elif s.get("sub"):
            step["sub"] = str(s["sub"])[:32]
        steps.append(step)
    steps = steps[:5]
    if len(steps) < 2:
        return None
    last_step_frame = round(max(step_seconds) * FPS)
    end_frame = max(last_step_frame, card_mount_frame) + STEP_LIST_STEP_HOLD_FRAMES
    entry: dict[str, Any] = {
        "steps": steps,
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": card_mount_frame, "endFrame": end_frame,
    }
    if dp.get("title"):
        entry["title"] = str(dp["title"])[:40]
    return entry


def _plan_comparison(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Multi-column side-by-side comparison (ComparisonCard) — several
    DIFFERENCES listed across 2-3 named options at once. Distinct from
    before_after (BudgetRevealSection), which is exactly ONE metric across
    two points in time, not several attributes across options."""
    cols_in = dp.get("columns") or []
    columns: list[dict] = []
    for c in cols_in:
        if not isinstance(c, dict):
            continue
        items = [str(it).strip()[:60] for it in (c.get("items") or []) if str(it).strip()]
        label = str(c.get("label", "")).strip()
        if not label or not items:
            continue
        col: dict[str, Any] = {"label": label[:24], "items": items[:4]}
        if c.get("label_en"):
            col["labelEn"] = str(c["label_en"])[:16]
        if c.get("accent") in ("good", "bad", "neutral"):
            col["accent"] = c["accent"]
        columns.append(col)
    if len(columns) < 2:
        return None
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + COMPARISON_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "columns": columns[:3],
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("title"):
        entry["title"] = str(dp["title"])[:40]
    return entry


def _plan_ranked_list(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Several numbers compared/ranked against EACH OTHER in the same beat
    (RankedListCard) — distinct from count_up (independent rows on one card,
    not explicitly compared) and before_after (one metric, two points in
    time)."""
    items_in = dp.get("items") or []
    items: list[dict] = []
    for it in items_in:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label", "")).strip()
        value = _num(it.get("value"))
        if not label or value is None:
            continue
        item: dict[str, Any] = {"label": label[:24], "value": value}
        if it.get("label_en"):
            item["labelEn"] = str(it["label_en"])[:16]
        if it.get("suffix"):
            item["suffix"] = str(it["suffix"])[:8]
        items.append(item)
    if len(items) < 2:
        return None
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + RANKED_LIST_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "items": items[:5],
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("title"):
        entry["title"] = str(dp["title"])[:40]
    return entry


def _plan_checklist(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Items ticking on one by one, each on its own spoken beat (ChecklistCard)
    — a confirmation/requirements-met beat, distinct from step_list's ordered
    how-to reading (same "each row has its own seconds" shape as step_list,
    see _plan_step_list)."""
    items_in = dp.get("items") or []
    item_seconds: list[float] = []
    for it in items_in:
        if not isinstance(it, dict):
            return None
        try:
            item_seconds.append(float(it["seconds"]))
        except (KeyError, TypeError, ValueError):
            return None
    if len(item_seconds) < 2:
        return None
    card_mount_frame = max(min_mount_frame, round(min(item_seconds) * FPS) - MOUNT_LEAD_FRAMES)
    items: list[dict] = []
    for it, sec in zip(items_in, item_seconds):
        label = str(it.get("label", "")).strip()
        if not label:
            continue
        item_frame = round(sec * FPS)
        item: dict[str, Any] = {
            "label": label[:24],
            "activateOffset": max(0, item_frame - card_mount_frame),
        }
        if it.get("label_en"):
            item["labelEn"] = str(it["label_en"])[:28]
        items.append(item)
    items = items[:6]
    if len(items) < 2:
        return None
    last_item_frame = round(max(item_seconds) * FPS)
    end_frame = max(last_item_frame, card_mount_frame) + CHECKLIST_ITEM_HOLD_FRAMES
    entry: dict[str, Any] = {
        "items": items,
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": card_mount_frame, "endFrame": end_frame,
    }
    if dp.get("title"):
        entry["title"] = str(dp["title"])[:40]
    return entry


def _plan_location_pin(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """A named place — a map pin drop (LocationPinCard). Nothing else in the
    library visualizes place; every other card is a number, a claim, or a
    process step."""
    place = str(dp.get("place", "")).strip()
    if not place:
        return None
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + LOCATION_PIN_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "place": place[:24],
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("place_en"):
        entry["placeEn"] = str(dp["place_en"])[:20]
    if dp.get("sub"):
        entry["sub"] = str(dp["sub"])[:60]
    return entry


def _plan_testimonial(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """A THIRD PARTY's quoted words (TestimonialCard) — distinct from `quote`
    (QuoteCard), which is reserved for the main speaker's own line and takes
    over the full canvas; this stays in the content zone like any other beat."""
    quote = str(dp.get("quote", "")).strip()
    name = str(dp.get("name", "")).strip()
    if not quote or not name:
        return None
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + TESTIMONIAL_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "quote": quote[:100], "name": name[:32],
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("role"):
        entry["role"] = str(dp["role"])[:40]
    return entry


def _plan_icon_cluster(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """An unordered SET of named things mentioned together (IconClusterCard)
    — distinct from step_list (ordered sequence) and topic_card (one single
    statement)."""
    valid_icons = ("chat", "camera", "play", "star", "bolt", "heart")
    items_in = dp.get("items") or []
    items: list[dict] = []
    for it in items_in:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label", "")).strip()
        if not label:
            continue
        icon = it.get("icon") if it.get("icon") in valid_icons else "star"
        item: dict[str, Any] = {"icon": icon, "label": label[:20]}
        if it.get("label_en"):
            item["labelEn"] = str(it["label_en"])[:16]
        items.append(item)
    if len(items) < 2:
        return None
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + ICON_CLUSTER_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "items": items[:6],
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("title"):
        entry["title"] = str(dp["title"])[:40]
    return entry


def _plan_progress_bar(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Straight linear completion bar (ProgressBarCard) — for a plain "how
    far along is this" moment, distinct from gauge (risk-framed) and
    countdown (time-remaining framed)."""
    label = str(dp.get("label", "")).strip()
    percent = _num(dp.get("percent"))
    if not label or percent is None:
        return None
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + PROGRESS_BAR_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "label": label[:40], "percent": max(0.0, min(100.0, percent)),
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("title"):
        entry["title"] = str(dp["title"])[:40]
    if dp.get("sub"):
        entry["subtext"] = str(dp["sub"])[:60]
    return entry


def _plan_pros_cons(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Polarized two-column pros/cons (ProsConsCard) — distinct from
    comparison (neutral labeled columns): baked-in good/bad framing for a
    decision or warning moment."""
    pros = [str(it).strip()[:60] for it in (dp.get("pros") or []) if str(it).strip()]
    cons = [str(it).strip()[:60] for it in (dp.get("cons") or []) if str(it).strip()]
    pros_label = str(dp.get("pros_label", "")).strip()
    cons_label = str(dp.get("cons_label", "")).strip()
    if not pros or not cons or not pros_label or not cons_label:
        return None
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + PROS_CONS_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "prosLabel": pros_label[:16], "consLabel": cons_label[:16],
        "pros": pros[:4], "cons": cons[:4],
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("title"):
        entry["title"] = str(dp["title"])[:40]
    return entry


def _plan_milestone_track(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Lightweight inline history dot-track (MilestoneTrackCard) — distinct
    from process_timeline (full-canvas dark takeover): stays in the normal
    content zone for a quick journey/history beat that doesn't warrant
    taking over the screen (same "each row has its own seconds" shape as
    step_list/checklist, see _plan_step_list)."""
    items_in = dp.get("milestones") or []
    item_seconds: list[float] = []
    for it in items_in:
        if not isinstance(it, dict):
            return None
        try:
            item_seconds.append(float(it["seconds"]))
        except (KeyError, TypeError, ValueError):
            return None
    if len(item_seconds) < 2:
        return None
    card_mount_frame = max(min_mount_frame, round(min(item_seconds) * FPS) - MOUNT_LEAD_FRAMES)
    milestones: list[dict] = []
    for it, sec in zip(items_in, item_seconds):
        label = str(it.get("label", "")).strip()
        if not label:
            continue
        m: dict[str, Any] = {"label": label[:20]}
        if it.get("sublabel"):
            m["sublabel"] = str(it["sublabel"])[:16]
        milestones.append(m)
    milestones = milestones[:4]
    if len(milestones) < 2:
        return None
    last_frame = round(max(item_seconds) * FPS)
    end_frame = max(last_frame, card_mount_frame) + MILESTONE_TRACK_HOLD_FRAMES
    entry: dict[str, Any] = {
        "milestones": milestones,
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": card_mount_frame, "endFrame": end_frame,
    }
    if dp.get("title"):
        entry["title"] = str(dp["title"])[:40]
    return entry


def _plan_trust_badge(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Credential/authority stack (TrustBadgeCard) — icon + stat/credential
    rows, for a moment building the speaker's own trust rather than
    conveying a number/date. Distinct from testimonial (a third party's
    words)."""
    valid_icons = ("shield", "star", "award", "clock")
    items_in = dp.get("badges") or []
    badges: list[dict] = []
    for it in items_in:
        if not isinstance(it, dict):
            continue
        primary = str(it.get("primary", "")).strip()
        secondary = str(it.get("secondary", "")).strip()
        if not primary or not secondary:
            continue
        icon = it.get("icon") if it.get("icon") in valid_icons else "shield"
        badges.append({"icon": icon, "primary": primary[:28], "secondary": secondary[:32]})
    if not badges:
        return None
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + TRUST_BADGE_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "badges": badges[:3],
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("title"):
        entry["title"] = str(dp["title"])[:40]
    return entry


def _plan_bar_chart(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Real axis-based column chart (BarChartCard) — for genuinely
    chart-shaped multi-value data, distinct from ranked_list (a ranked list
    of single values with rank badges, not a chart)."""
    items_in = dp.get("items") or []
    items: list[dict] = []
    for it in items_in:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label", "")).strip()
        value = _num(it.get("value"))
        if not label or value is None:
            continue
        item: dict[str, Any] = {"label": label[:16], "value": value}
        if it.get("display_value"):
            item["displayValue"] = str(it["display_value"])[:12]
        items.append(item)
    if len(items) < 2:
        return None
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + BAR_CHART_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "items": items[:4],
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("title"):
        entry["title"] = str(dp["title"])[:40]
    return entry


def _plan_milestone_unlock(dp: dict, min_mount_frame: int) -> Optional[dict]:
    """Celebratory single-number reveal (MilestoneUnlockCard) — reserved for
    a genuine scale/achievement moment, distinct from a routine count_up
    row."""
    value = _num(dp.get("value"))
    label = str(dp.get("label", "")).strip()
    if value is None or not label:
        return None
    valid_icons = ("award", "star", "heart", "bolt")
    icon = dp.get("icon") if dp.get("icon") in valid_icons else "award"
    sec = float(dp["seconds"])
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + MILESTONE_UNLOCK_DISPLAY_FRAMES)
    entry: dict[str, Any] = {
        "value": value, "label": label[:28], "icon": icon,
        "x": _CONTENT_ZONE_X, "y": _CONTENT_ZONE_Y, "width": _CONTENT_ZONE_WIDTH,
        "mountFrame": mount_frame, "endFrame": end_frame,
    }
    if dp.get("suffix"):
        entry["suffix"] = str(dp["suffix"])[:4]
    if dp.get("prefix"):
        entry["prefix"] = str(dp["prefix"])[:4]
    return entry


def _plan_corner_card(dp: dict) -> Optional[dict]:
    """Compact illustration overlay anchored inside the SpeakerCard (see
    CornerCard.tsx) — never enters the content-zone stacking system, so
    unlike every other _plan_* function this has no min_mount_frame floor
    (nothing else in the zone competes with it for space)."""
    variant = dp.get("variant")
    if variant not in ("chat", "progress"):
        return None
    sec = float(dp["seconds"])
    mount_frame = max(0, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = mount_frame + CORNER_CARD_DISPLAY_FRAMES
    entry: dict[str, Any] = {"variant": variant, "mountFrame": mount_frame, "endFrame": end_frame}
    if variant == "chat":
        message = str(dp.get("message", ""))[:60]
        if not message:
            return None
        entry["message"] = message
        entry["appName"] = str(dp.get("appName") or "WhatsApp")[:24]
    else:
        pct = _num(dp.get("percent"))
        entry["percent"] = max(0.0, min(100.0, pct if pct is not None else 0.0))
        if dp.get("label"):
            entry["label"] = str(dp["label"])[:24]
    return entry


def _grounded_or_fallback_end(dp: dict, mount_frame: int, fallback_end_frame: int) -> int:
    """Fix E3：dp 有关键词校准过的收尾时间(_grounded_end_seconds，见
    _ground_end_keyword)时，用它(+ 停留缓冲)决定退场，取代固定时长的
    fallback_end_frame。没有就原样返回 fallback_end_frame——完全不影响没有
    end_keyword 的既有行为。"""
    grounded = dp.get("_grounded_end_seconds")
    if grounded is None:
        return fallback_end_frame
    grounded_end = round(grounded * FPS) + _KEYWORD_EXIT_HOLD_FRAMES
    return max(mount_frame + _MIN_VISUAL_HOLD_FRAMES, grounded_end)


def _plan_gauge(dp: dict, min_mount_frame: int) -> Optional[dict]:
    sec = float(dp["seconds"])
    value = _num(dp.get("value"))
    if value is None:
        return None
    # Fix C40（2026-07-21，真实生产复现——job_452ef6c48100 真实交付的 gauge
    # title 是空字符串，GaugeCard 顶部标题栏整条留白）：SYSTEM_PROMPT 的 gauge
    # schema 明确要求 "title"，但 LLM 偶尔就是不给——不像 headline/message 那样
    # 已有"缺了就整卡跳过"的保护。跟 _plan_topic_card/_plan_corner_card 同一个
    # 原则：schema 里标了必填的文字字段，缺了就不产出这张卡，不留一个视觉上
    # 半成品的卡片进最终 props。
    if not str(dp.get("title", "")).strip():
        return None
    mount_frame = max(min_mount_frame, round(sec * FPS) - MOUNT_LEAD_FRAMES)
    end_frame = _grounded_or_fallback_end(
        dp, mount_frame, mount_frame + GAUGE_ANIMATION_FRAMES + HOLD_AFTER_LAST_ROW_FRAMES
    )
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
    end_frame = _grounded_or_fallback_end(
        dp, mount_frame, mount_frame + COUNTDOWN_ANIMATION_FRAMES + HOLD_AFTER_LAST_ROW_FRAMES
    )
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
    end_frame = _grounded_or_fallback_end(dp, mount_frame, mount_frame + CALENDAR_DISPLAY_FRAMES)
    return {
        "year": int(year), "month": int(month), "targetDay": int(target_day),
        "eventLabel": str(dp.get("eventLabel", ""))[:60],
        # Calendar component renders at a fixed _CALENDAR_WIDTH — center it in
        # the zone lane rather than leaving it hugging the left edge.
        "x": _CONTENT_ZONE_X + (_CONTENT_ZONE_WIDTH - _CALENDAR_WIDTH) // 2, "y": _CONTENT_ZONE_Y,
        "width": _CALENDAR_WIDTH,
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
        "endFrame": _grounded_or_fallback_end(dp, mount_frame, mount_frame + QUOTE_DISPLAY_FRAMES),
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
        # Fix C42（2026-07-21，怀疑真实生产复现——job_73e873e4f7e1，MrBeast
        # 视频，用户反馈"很喜欢 timeline 但看到的是弹窗卡"）：chapters[].label
        # 和 process_timeline.chapter_label 是同一次 LLM 响应里两个独立的
        # JSON 字段，结构上完全没有"两边写的字一定一模一样"的保证——哪怕只是
        # 多了个空格、大小写、标点，精确匹配就直接失手，整段 timeline 数据
        # 被静默丢弃，返回 None。丢了 timeline 之后，这个本来该是 process_
        # timeline 载体的章节，D2 的预算检查（见下面 `"timeline" in longest`）
        # 会把它当成普通接管处理，超预算时整段摘除而不是裁剪保留——观众该看到
        # 的多阶段动画一次都没播出来过，只剩泛泛的弹窗卡填空。SYSTEM_PROMPT
        # 明确说 process_timeline "必须对应恰好一个标了 takeover:true 的章节"
        # ——如果精确匹配失败但全部章节里*恰好只有一个*被标了 takeover，几乎
        # 可以确定就是这个，用它兜底，而不是直接放弃整段数据。
        takeover_chapters = [i for i, c in enumerate(chapters) if c.get("_takeover")]
        if len(takeover_chapters) == 1:
            match_idx = takeover_chapters[0]
            logger.info(
                f"content_planner: process_timeline 的 chapter_label('{chapter_label}') "
                f"没有精确匹配到任何章节标题，退回到唯一被标记 takeover 的章节"
                f"（Fix C42，避免整段 timeline 数据被白白丢弃）"
            )
        else:
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

    # Overrides whatever the LLM said for "takeover" on this specific
    # chapter — a process_timeline always requires the full-canvas takeover
    # treatment, and it occupies the same space an icon would. Does NOT
    # force "_dark" anymore (Fix D6, 2026-07-16): TimelineSection now renders
    # correctly in both color modes, so the chapter's own dark/light
    # classification (already parsed into ch["_dark"] above) is respected
    # instead of always being flipped to dark — confirmed real bug this
    # caused: forcing dark on a normal-tone "how our process works" chapter
    # inside an otherwise warm-mode video is a jarring, unmotivated color
    # flash, and was part of why this treatment was under-used for content
    # that would otherwise benefit from it.
    ch["_takeover"] = True
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
    """count_up cards with a row that DISPLAYS as zero — titles only, used both
    as retry feedback and as the final "still bad after retry, drop the whole
    card" filter. count_up 0 is treated as always-suspicious, unlike
    gauge/countdown where 0 is a legitimate answer (e.g. "0 days left").

    Checks the value AFTER divideBy+decimals, not just the raw extracted
    number — real production bug (David render): LLM extracted value=1.5
    (already "in millions") but also attached divideBy=1000000 per the
    prompt's own formatting instructions meant for raw dollar amounts,
    producing 1.5/1,000,000 rounded to 1 decimal = "0.0" ("Coverage: $0.0M")
    even though the raw value was non-zero and passed the old raw-only check."""
    titles = []
    for dp in raw.get("data_points", []) or []:
        if not isinstance(dp, dict) or dp.get("visual") != "count_up":
            continue
        bad = False
        for r in (dp.get("rows") or []):
            if not isinstance(r, dict):
                continue
            value = _num(r.get("value"))
            if value is None:
                continue
            divide_by = _num(r.get("divideBy")) or 1.0
            decimals = r.get("decimals")
            decimals = decimals if isinstance(decimals, int) else 0
            if round(value / divide_by, decimals) == 0.0:
                bad = True
                break
        if bad:
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

# 规划质量标准循环（见 plan_content）：每次规划最多几轮 LLM 调用、以及
# "说话视频必须有画面节奏"标准生效的最短时长——太短的视频（<15s）intro/
# outro 本身就够撑住画面，不强求额外图形。
_PLAN_MAX_ATTEMPTS = 3
_MIN_DURATION_FOR_VISUALS_S = 15.0


def _spoken_dollar_amounts(segments: list[dict]) -> list[str]:
    """Every distinct '$<amount>' substring actually spoken in the transcript
    (verbatim, as ASR rendered it — no unit parsing, just presence)."""
    seen: list[str] = []
    for seg in segments or []:
        for m in _DOLLAR_AMOUNT_RE.finditer(str(seg.get("text", ""))):
            text = m.group(0).strip()
            if text not in seen:
                seen.append(text)
    return seen


_DOLLAR_AMOUNT_RE = re.compile(r"\$\s*[\d,]+(?:\.\d+)?\s*(?:million|thousand|k|m)?", re.IGNORECASE)
_NUMERIC_VISUAL_TYPES = {"count_up", "gauge", "countdown", "before_after"}

_DOLLAR_SUFFIX_MULTIPLIER = {"k": 1_000, "m": 1_000_000, "million": 1_000_000, "thousand": 1_000}


def _dollar_amount_to_float(text: str) -> Optional[float]:
    """'$8,400' -> 8400.0, '$1.5 million' -> 1_500_000.0, '$500k' -> 500_000.0."""
    m = re.match(r"\$\s*([\d,]+(?:\.\d+)?)\s*(million|thousand|k|m)?", text.strip(), re.IGNORECASE)
    if not m:
        return None
    base = _num(m.group(1).replace(",", ""))
    if base is None:
        return None
    suffix = (m.group(2) or "").lower()
    return base * _DOLLAR_SUFFIX_MULTIPLIER.get(suffix, 1)


# 口播里 "$8,400" 这类数字形式的金额，_DOLLAR_AMOUNT_RE 已经能抓到——但像
# "one and a half million" 这种纯词面的大数字（真实案例：job_452ef6c48100
# 的 coverage 金额，全程转写里从没出现过任何数字形式），原本完全没有任何
# 检测覆盖。范围刻意收窄：只处理 "<数字词>[ and a <分数词>] <量级词>" 这一
# 种口语金融叙述里常见的模式，不是通用英语数字解析器。
_UNIT_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TEN_WORDS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALE_WORDS = {"hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_FRACTION_WORDS = {"half": 0.5, "quarter": 0.25}
_WORD_TOKEN_RE = re.compile(r"[a-z]+")


def _spoken_word_numbers(segments: list[dict]) -> list[float]:
    """Best-effort extraction of large numbers spoken purely as words
    ('one and a half million', 'one point five million', 'two hundred
    thousand') — see module note above on scope. Returns raw numeric values,
    same units as a count_up row's "value" field (pre-divideBy)."""
    found: list[float] = []
    for seg in segments or []:
        tokens = _WORD_TOKEN_RE.findall(str(seg.get("text", "")).lower())
        n = len(tokens)
        i = 0
        while i < n:
            base = _UNIT_WORDS.get(tokens[i], _TEN_WORDS.get(tokens[i]))
            if base is None:
                i += 1
                continue
            j = i + 1
            frac = 0.0
            consumed_extra = False
            if j + 2 < n and tokens[j] == "and" and tokens[j + 1] == "a" \
                    and tokens[j + 2] in _FRACTION_WORDS:
                frac = _FRACTION_WORDS[tokens[j + 2]]
                j += 3
                consumed_extra = True
            elif j < n and tokens[j] == "point":
                digits = []
                k = j + 1
                while k < n and _UNIT_WORDS.get(tokens[k], 10) < 10:
                    digits.append(str(_UNIT_WORDS[tokens[k]]))
                    k += 1
                if digits:
                    frac = float("0." + "".join(digits))
                    j = k
                    consumed_extra = True
            if j < n and tokens[j] in _SCALE_WORDS:
                found.append((base + frac) * _SCALE_WORDS[tokens[j]])
                i = j + 1
                continue
            # No scale word followed — this wasn't a big-number phrase (e.g.
            # "twelve percent", or a bare decimal like "one point five" with
            # no magnitude word). Skip past whatever was tentatively consumed
            # so "five" in "one point five" doesn't get re-scanned on its own
            # and misread as an unrelated standalone number.
            i = j if consumed_extra else i + 1
    return found


# 确认过的真实 bug（job_5b0ec0b914ee，2026-07-27）：同一句话
# ("...covers you for $1.5 million and your annual premium is $8,400.")
# 在 input_transcript.json（用于生成 script.json 的第一次转写）里带着 $
# 号，但 apply_style 自己内部为字幕做的第二次转写（增强链之后重新跑一遍
# faster-whisper）把同一段音频转成了没有 $ 号的 "1.5 million"——同一段音频、
# 同一个模型，纯粹是 ASR 输出格式的运行间抖动。LLM 规划的 Coverage=1.5
# 完全正确（转录里明明白白说了这个数），但 _ungrounded_count_up_rows 判定
# "没有任何依据"——因为提取函数只认 "$<数字>" 和纯词面数字（"one and a
# half million"）两种形式，"<数字> million" 这种数字紧跟量级词、没有货币
# 符号的第三种口语形式两边都没覆盖。检查本身没错，是覆盖面不够；结果是
# LLM 每一轮都被错误驳回，白白烧光 apply_style 一整段内容规划预算，最终
# 触发降级交付——模板没套上，根因根本不在内容规划或视觉复审，而在这条
# grounding 检查自己的正则覆盖不全。
_DIGIT_SCALE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(million|thousand|billion)\b", re.IGNORECASE)


def _spoken_bare_scale_numbers(segments: list[dict]) -> list[float]:
    """'1.5 million' / '500 thousand'（数字直接跟量级词，没有 $ 前缀，数字
    本身也不是拼出来的词）——范围跟 `_spoken_word_numbers` 一样刻意收窄，
    只处理这一种口语金融叙述里常见的混合形式，不是通用数字解析器。"""
    found: list[float] = []
    for seg in segments or []:
        for m in _DIGIT_SCALE_RE.finditer(str(seg.get("text", ""))):
            base = _num(m.group(1))
            if base is not None:
                found.append(base * _SCALE_WORDS[m.group(2).lower()])
    return found


def _grounded_spoken_values(segments: list[dict]) -> list[float]:
    """Every number actually spoken in the transcript, in the units a count_up
    row's raw "value" would use — combines digit-form ('$8,400'), word-form
    ('one and a half million'), and bare digit+scale-word ('1.5 million',
    no currency symbol) amounts."""
    values: list[float] = []
    for amt in _spoken_dollar_amounts(segments):
        v = _dollar_amount_to_float(amt)
        if v is not None:
            values.append(v)
    values.extend(_spoken_word_numbers(segments))
    values.extend(_spoken_bare_scale_numbers(segments))
    return values


def _ungrounded_count_up_rows(raw: dict, segments: list[dict]) -> list[str]:
    """count_up rows whose raw value matches NONE of the numbers actually
    spoken in the transcript — catches the LLM inventing a plausible-looking
    but wrong figure. Confirmed real bug (job_452ef6c48100): delivered
    "$6,300"/"$1.1M" count_up rows on some replan rounds when the transcript
    only ever said "$8,400"/"one and a half million" — not a retake/dedup
    artifact (verified against both the raw and filler-removed transcripts),
    a straight hallucination the presence-only C41 check can't catch since
    *a* numeric card did exist, just with the wrong number in it.

    Only fires when there's at least one grounded candidate for this
    transcript, so a row this function can't parse against (percentages, small
    counts, a quantity with no digit or big-word-number form) is left alone
    rather than false-flagged.

    A row's "value" is supposed to be the RAW spoken figure per SYSTEM_PROMPT
    (e.g. 1500000, with divideBy=1000000 formatting it to "1.5" for display) —
    but real recorded LLM responses also legitimately write the already-scaled
    number directly (value=1.5, divideBy=1.0, unit="M"), the same convention
    ambiguity `_zero_value_titles` exists to handle on the "displays as zero"
    side. So each candidate is checked at its raw scale AND divided by 1e3/1e6,
    not just matched exactly — a hallucinated value has to miss all of those
    to get flagged."""
    candidates = _grounded_spoken_values(segments)
    if not candidates:
        return []
    expanded = [scaled for c in candidates for scaled in (c, c / 1_000, c / 1_000_000)]
    bad: list[str] = []
    for dp in raw.get("data_points", []) or []:
        if not isinstance(dp, dict) or dp.get("visual") != "count_up":
            continue
        for r in (dp.get("rows") or []):
            if not isinstance(r, dict):
                continue
            value = _num(r.get("value"))
            if value is None or value == 0:
                continue
            if any(abs(value - c) <= max(abs(c), 1.0) * 0.01 for c in expanded):
                continue
            bad.append(f"{dp.get('title', '')} row '{r.get('label', '')}' = {value:g}")
    return bad


def _uncovered_spoken_values(raw: dict, segments: list[dict]) -> list[str]:
    """Spoken numeric figures (dollar amounts and word-form big numbers) with
    NO matching count_up row or before_after value anywhere in the plan —
    the mirror image of `_ungrounded_count_up_rows` (that one catches a card
    value with no matching spoken figure; this one catches a spoken figure
    with no matching card).

    Confirmed real bug (2026-07-23, WhatsApp-delivered preview of the David/
    Pacific Life renewal video): the same sentence ("your current plan covers
    you for one and a half million and your annual premium is $8,400")
    produced a plan with ONLY the Premium row — Coverage's $1.5M was silently
    dropped even though $8,400 survived from the exact same sentence. C41
    (`has_numeric_card`, criterion 4 below) only checks "does at least one
    numeric card exist anywhere" — that check passes here because the
    Premium card exists; it has no opinion on whether EVERY distinct spoken
    figure got covered, so a plan that keeps one figure and drops another
    sails through clean.

    Only checks count_up rows and before_after left/right values — gauge's
    "value" is a 0-1 risk fraction and countdown's is a day-count, neither is
    a monetary figure, so including them would risk a coincidental numeric
    match against a semantically unrelated field. Same scale-ambiguity
    tolerance as `_ungrounded_count_up_rows` (a plan value may legitimately
    be raw OR pre-scaled per SYSTEM_PROMPT's two conventions), just applied
    in the opposite direction — expand the PLAN's values up through the
    scale factors instead of the candidates down."""
    candidates = _grounded_spoken_values(segments)
    if not candidates:
        return []
    plan_values: list[float] = []
    for dp in raw.get("data_points", []) or []:
        if not isinstance(dp, dict):
            continue
        if dp.get("visual") == "count_up":
            for r in (dp.get("rows") or []):
                if isinstance(r, dict):
                    v = _num(r.get("value"))
                    if v is not None:
                        plan_values.append(v)
        elif dp.get("visual") == "before_after":
            for key in ("leftValue", "rightValue"):
                v = _num(dp.get(key))
                if v is not None:
                    plan_values.append(v)
    expanded_plan = [scaled for v in plan_values for scaled in (v, v * 1_000, v * 1_000_000)]
    missing: list[str] = []
    seen: set = set()
    for c in candidates:
        if any(abs(c - p) <= max(abs(c), 1.0) * 0.01 for p in expanded_plan):
            continue
        label = f"{c:g}"
        if label not in seen:
            seen.add(label)
            missing.append(label)
    return missing


# 一句话里同时出现"还剩 N 天/周/月"式的倒计时措辞和一个具体月份名——续保/到期
# 类句子的典型形状("renewal in 30 days on the 28th of July")。范围刻意收窄到
# 这个具体组合，不是"凡是提到天数就该有倒计时"（那会在描述时长的无关句子上
# 大量误报，例如"我做这行十年了"、分步流程里的"用了三天完成"）。
_COUNTDOWN_PHRASE_RE = re.compile(r"\b\d{1,3}\s*(?:day|days|week|weeks|month|months)\b", re.IGNORECASE)
_MONTH_NAME_RE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)


def _spoken_countdown_and_date_together(segments: list[dict]) -> bool:
    """True when the SAME transcript segment names both a time-remaining
    figure ("30 days") and a calendar month — the shape of a renewal-style
    sentence describing one event two ways. Confirmed real bug (2026-07-23,
    same job as `_uncovered_spoken_values`): "30 days on the 28th of July"
    landed in one ASR segment (verified against the real transcript), the
    plan produced a calendar card for July 28th but zero countdown cards for
    the 30-day figure — content_planner's own SYSTEM_PROMPT treats countdown
    and calendar as two independently flaggable moments for exactly this
    kind of sentence, but nothing enforced that both actually get emitted."""
    for seg in segments or []:
        text = str(seg.get("text", ""))
        if _COUNTDOWN_PHRASE_RE.search(text) and _MONTH_NAME_RE.search(text):
            return True
    return False


# 片尾 CTA 只在视频够长时才有意义（跟 pipeline_runner.py 自己的
# `duration_frames >= 360` 门槛保持一致——12s 以下的视频 outro 会跟正文内容
# 抢屏，pipeline_runner 会整段跳过，规划阶段没必要为这种视频强求 outro）。
_MIN_DURATION_FOR_OUTRO_S = 12.0
# 收尾/告别/联系方式类措辞——出现在转写末段时，说话人明显在做结束语，这种
# 情况下"没有 outro"几乎总是规划漏了，不是这条视频真的没有收尾。范围包含
# "常见告别语" + "联系方式 CTA"（SYSTEM_PROMPT 1c 本来就要求 outro 抓这类内容），
# 不含泛用词，避免在视频中段的普通句子上误报。
_CLOSING_LANGUAGE_RE = re.compile(
    r"\b(take care|talk (?:to you )?soon|thanks? for watching|see you (?:next|soon|again)|"
    r"looking forward to|reach out|get back to you|contact (?:me|us)|whatsapp me|"
    r"scan the qr|any questions|take it easy|until next time)\b",
    re.IGNORECASE,
)


def _transcript_has_closing_language(segments: list[dict], duration: float) -> bool:
    """True when a segment landing in the last quarter of the video contains
    closing/farewell/contact-CTA language — the shape of a speaker wrapping
    up, which is exactly what an outro card is meant to capture (SYSTEM_PROMPT
    1c). Scoped to the tail of the video specifically so an early-video
    mention of "contact me" (e.g. inside the main body, unrelated to closing)
    doesn't trigger this."""
    if not segments or duration <= 0:
        return False
    cutoff = duration * 0.75
    for seg in segments:
        start = seg.get("start")
        if start is None or start < cutoff:
            continue
        if _CLOSING_LANGUAGE_RE.search(str(seg.get("text", ""))):
            return True
    return False


def _plan_quality_failures(raw: dict, plan: dict, duration: float, segments: Optional[list[dict]] = None) -> list[str]:
    """规划质量标准——纯函数、确定性、不依赖 LLM 自评。plan_content 的
    criterion loop 每轮规划后跑一遍：返回空列表 = 全部达标（提前退出循环）；
    非空 = 每一条都是喂给下一轮 LLM 的具体失败描述（英文，因为 SYSTEM_PROMPT
    是英文，反馈要跟提示词同语言才最有效）。

    每条标准都对应一类确认过的真实生产失败：
    1. 零图形——job_1dd6e4748b31/job_d3239b81fd1c（Dickson 视频）：整条 35s
       口播只有字幕，一个图形都没规划出来。说话的视频永远有东西可以可视化
       （工具名/步骤/数字/观点），空 data_points 是规划失败，不是"这条视频
       没料"。
    2. 长空档——同上：就算规划了 1-2 个图形，中段 20 多秒完全没有画面事件
       也是不达标（_sparse_gaps 的既有标准，此前只补救一次就放弃）。
    3. 0 值卡——job_acefec8b1c82 等多次复现的 "$0.0M"：格式化后显示为 0 的
       数字卡是提取错误，必须修正或删除。
    4. 说了具体金额却一张数字卡都没有——job_452ef6c48100（真实用户反馈）：
       同一支视频、同样的转写，某几轮重规划把 "one and a half million"/
       "$8,400" 正确变成 count_up/gauge 卡，另几轮却完全没有任何数字卡，
       只剩几张泛泛的 topic_card——SYSTEM_PROMPT 的分类指引本身没问题（已经
       写明"任何数字都该是 count_up/gauge/countdown/before_after，不要默认
       套 topic_card"），是 LLM 每轮的执行不稳定，属于"reprompt 不可靠"的
       那一类，该加确定性标准而不是继续加提示词字数。这里只查最容易、最不
       会误判的信号——转写里出现过 "$" 金额，但整份计划里一张
       count_up/gauge/countdown/before_after 都没有——出现这种情况几乎总是
       该数字被漏掉了，不是"这条视频真的没有数字"（那种情况下一开始就不会
       在转写里出现 "$"）。
    5. 数字卡里的值跟转写对不上——同一支 job_452ef6c48100，2026-07-21 真实
       复现：criterion 4 只查"有没有数字卡"，从没查过卡片里的数字对不对。
       同一份转写（前后两次检查过，remove_filler 前后都一样）只说过
       "$8,400" 和 "one and a half million"，但某几轮重规划把 count_up 行
       写成了 "$6,300"/"$1.1M"——转写里根本没有这两个数字，纯属编造，不是
       retake/去重漏留的旧片段（Rule 16 那条"未复现"的结论建立在旧证据上，
       这次真实复现了）。`_ungrounded_count_up_rows` 同时抓数字形式
       （"$8,400"）和词面大数（"one and a half million"），只在转写里确实
       能解析出至少一个数字候选时才生效，避免误伤解析不了的行（百分比、
       小计数等）。
    6. 说了多个具体数字，但只有一部分落进了卡片——2026-07-23 真实复现（WhatsApp
       实际交付的预览）：同一句话里 "one and a half million" 和 "$8,400" 都被
       说出来了，规划却只留下 Premium 那一张卡，Coverage 的 $1.5M 整个消失。
       标准 4 只查"有没有数字卡"，这里已经有一张（Premium），检查照样通过；
       标准 5 只查"卡片里的数字有没有编造"，这张卡的数字（$8,400）也确实是
       转写里说的，同样通过。两条现有标准都不覆盖"漏了另一个数字"这一种情况。
       `_uncovered_spoken_values` 反过来查：每一个转写里说过的数字，有没有
       在某张 count_up/before_after 卡里落地——跟标准 5 用同一套刻度换算兜底
       （卡片数值可能是原始值也可能是已经缩放过的），方向相反而已。
    7. 同一句话里既有"还剩 N 天"式的倒计时措辞，又有具体月份/日期，但规划
       只产出日历卡，倒计时整个消失——同一次真实复现："30 days on the 28th
       of July" 落在同一个转写分段里（真实数据验证过），SYSTEM_PROMPT 把倒计
       时和日历当成同一句话可以各自独立触发的两个数据点，但没有任何机制
       保证两个都真的被产出。`_spoken_countdown_and_date_together` 的匹配范围
       刻意收窄到"倒计时措辞跟月份名同段共现"，不是"凡是提到天数就该有倒计
       时"，避免在描述时长的无关句子上（"我做这行十年了"、分步流程的"用了
       三天完成"）大量误报。
    8. 转写末段明显在做收尾/告别/留联系方式，但计划里没有 outro（或 outro
       的 headline 是空的）——同一次真实复现，用户直接反馈"the outro is not
       there again"。`_transcript_has_closing_language` 只看视频最后 1/4
       时间段，避免视频中段一句"欢迎联系我们"被误判成结尾。只在视频长到
       outro 本来就该有意义时才生效（跟 pipeline_runner.py 自己的
       `duration_frames >= 360` 门槛对齐），短视频不强求。
    """
    failures: list[str] = []
    if duration >= _MIN_DURATION_FOR_VISUALS_S:
        total_visuals = (
            len(plan["data_cards"]) + len(plan["gauges"]) + len(plan["countdowns"])
            + len(plan["calendar_events"]) + len(plan["before_after"]) + len(plan["quotes"])
            + len(plan["step_lists"]) + len(plan["topic_cards"]) + len(plan["corner_cards"])
            + len(plan["sections"])
            + len(plan["comparisons"]) + len(plan["ranked_lists"]) + len(plan["checklists"])
            + len(plan["location_pins"]) + len(plan["testimonials"]) + len(plan["icon_clusters"])
            + len(plan["progress_bars"]) + len(plan["pros_cons"]) + len(plan["milestone_tracks"])
            + len(plan["trust_badges"]) + len(plan["bar_charts"]) + len(plan["milestone_unlocks"])
        )
        if total_visuals == 0:
            failures.append(
                f"ZERO visual moments planned for a {duration:.0f}s talking video. Every talking "
                "video has visualizable content — named apps/tools (corner_card), described "
                "processes/steps (step_list), claims/tips (topic_card), numbers (count_up), "
                "risks (gauge). Re-read the transcript and plan at least 2-3 moments."
            )
    gaps = _sparse_gaps(plan, duration)
    if gaps:
        spans = ", ".join(f"{a / FPS:.0f}s-{b / FPS:.0f}s" for a, b in gaps)
        failures.append(
            f"No visual event AT ALL during these spans: {spans} — on screen it's just the "
            "speaker and captions for too long. Add moments (topic_card/step_list/corner_card "
            "if nothing numeric is spoken there) anchored to sentences spoken WITHIN those spans."
        )
    bad_titles = _zero_value_titles(raw)
    if bad_titles:
        failures.append(
            f"These count_up card(s) display as ZERO after divideBy/decimals formatting: "
            f"{', '.join(bad_titles)} — the extracted value or its divideBy is wrong. Re-read "
            "the transcript for the actual spoken figure (and only apply divideBy to RAW dollar "
            "amounts, not values already spoken in millions)."
        )
    dollar_amounts = _spoken_dollar_amounts(segments or [])
    if dollar_amounts:
        has_numeric_card = any(
            isinstance(dp, dict) and dp.get("visual", "count_up") in _NUMERIC_VISUAL_TYPES
            for dp in (raw.get("data_points") or [])
        )
        if not has_numeric_card:
            failures.append(
                f"The transcript explicitly says {', '.join(dollar_amounts)} but the plan has "
                "ZERO count_up/gauge/countdown/before_after cards — a spoken dollar amount "
                "almost always deserves its own numeric card, not just a generic topic_card. "
                "Add a count_up (or gauge/before_after if it fits that shape better) for each "
                "amount actually spoken."
            )
    ungrounded = _ungrounded_count_up_rows(raw, segments or [])
    if ungrounded:
        spoken = _grounded_spoken_values(segments or [])
        failures.append(
            f"These count_up card value(s) do not match ANY number actually spoken in the "
            f"transcript: {', '.join(ungrounded)}. The transcript only ever states these "
            f"figures: {', '.join(f'{v:g}' for v in spoken)} — re-read the transcript and use "
            "the exact spoken figure for this card, do not invent or approximate a nearby "
            "number."
        )
    uncovered_values = _uncovered_spoken_values(raw, segments or [])
    if uncovered_values:
        failures.append(
            f"These figure(s) are spoken in the transcript but have NO matching count_up/"
            f"before_after value anywhere in the plan: {', '.join(uncovered_values)}. If multiple "
            "distinct numbers are spoken in the same sentence/moment (e.g. a coverage amount AND "
            "a premium amount), each one needs its OWN row/card — do not keep only one number and "
            "silently drop the other."
        )
    if (_spoken_countdown_and_date_together(segments or [])
            and plan["calendar_events"] and not plan["countdowns"]):
        failures.append(
            "The transcript names both a time-remaining figure (e.g. \"N days\") and a specific "
            "calendar date for the same event, but the plan only produced a calendar card and ZERO "
            "countdown cards. Add a \"countdown\" data point for the day-count figure IN ADDITION "
            "to the calendar — they are two independently valid visuals for the same moment, not "
            "alternatives."
        )
    raw_outro = raw.get("outro")
    has_outro_headline = isinstance(raw_outro, dict) and str(raw_outro.get("headline", "")).strip()
    if (duration >= _MIN_DURATION_FOR_OUTRO_S
            and _transcript_has_closing_language(segments or [], duration)
            and not has_outro_headline):
        failures.append(
            "The end of the transcript has closing/farewell/contact-CTA language but the plan has "
            "NO outro (or an outro with an empty headline). Write an outro CTA card (kicker/"
            "headline/subtext/cta_label, grounded in what the speaker actually says at the end) "
            "per instruction 1c — do not leave the video ending on just the raw speaker footage."
        )
    return failures

REPLAN_SYSTEM_PROMPT = """You previously produced a content plan for this talking-head video, but the listed time spans have NO visual event at all (no data graphic, no quote, no section takeover) — on screen it's just the speaker and captions for too long.

These spans do NOT need to contain a number to deserve a visual — a named app/tool ("WhatsApp", "an AI Agent"), an in-progress action (uploading, generating, converting), or a described step in a process ("you send X, it gives you back Y") is just as visualizable as a statistic. A span about a workflow/tool-demo with zero numbers should still get moments — most commonly "step_list" or "topic_card" for that kind of content, not nothing.

From the transcript lines spoken WITHIN those spans only, add visual moments using these exact shapes (fill in every field — a moment missing "seconds"/a row's "seconds" gets silently dropped, so never omit it):
- topic_card (default choice for a process/tool/tip moment, keeps the speaker visible): {"visual":"topic_card","icon":"chat|lightbulb|check|sparkle","headline":"short statement, verbatim from the line, <=60 chars","sub":"optional supporting detail, <=80 chars","seconds":12.3}
- step_list (only if there's a genuine 2-5 step sequential process spoken as steps): {"visual":"step_list","title":"...","steps":[{"label":"short step name","seconds":12.3}, ...]}
- count_up (only if an actual number/quantity is spoken): {"visual":"count_up","title":"...","rows":[{"label":"...","seconds":12.3,"value":42,"prefix":"","divideBy":1,"decimals":0,"unit":""}]}
- gauge/countdown/calendar: same shapes as the main content plan (risk/time-remaining/date), only if genuinely present in these lines.
- quote (exact spoken line, verbatim, <=80 chars): only if the line is a genuinely striking standalone statement — never for a routine/transitional sentence, and never the default choice.

1 moment per span is enough; skip a span only if its lines are genuinely too weak to show even as a topic_card (rare — that is acceptable, but don't reach for it just because nothing is numeric).

Output ONLY valid JSON: {"data_points": [ ... ]}"""


def _coverage_spans(plan: dict) -> list[tuple[int, int]]:
    spans = []
    # topic_cards 必须算进覆盖范围——LLM 自己规划的、或密度下限补规划产出的
    # 都可能是 topic_card，漏掉它会让密度检查始终觉得这段"还是空的"，导致
    # 同一段时间被重复补规划、堆出用户明确反对的"互相盖住"的多张卡片。
    #
    # Fix C30（2026-07-20，真实生产复现——job_f7b171f8d952/Dixon 视频）：这个
    # 列表漏了 step_lists 和 corner_cards 整整两种图形类型——SYSTEM_PROMPT 第
    # 2(b)/3 条明确把这两个列为"非数字内容"（命名工具/应用、进行中的动作、
    # 说出来的分步流程）的正确选择，LLM 也确实按提示词的引导规划出了真实、
    # 有信息量的 step_list("發送內容→AI製作數字人→自動剪接→收回上傳"四步)
    # 和 corner_card(WhatsApp 聊天气泡)——但因为这两类从没进过覆盖范围统计，
    # _sparse_gaps 死活觉得这段时间"还是空的"，criterion loop 三轮重规划全部
    # 判定失败，C28 的确定性兜底也跟着白白尝试、白白放弃。不是 LLM 没听
    # 提示词的话，是负责"检查画面是否已经有内容"的这个函数自己没跟上
    # SYSTEM_PROMPT 早就支持的完整图形词汇表——这个漏洞影响的是*所有*用
    # step_list/corner_card 覆盖非数字内容的视频，不只是这一支。
    for g in (plan["data_cards"] + plan["gauges"] + plan["countdowns"]
              + plan["calendar_events"] + plan["quotes"] + plan["topic_cards"]
              + plan["step_lists"] + plan["corner_cards"]
              + plan["comparisons"] + plan["ranked_lists"] + plan["checklists"]
              + plan["location_pins"] + plan["testimonials"] + plan["icon_clusters"]
              + plan["progress_bars"] + plan["pros_cons"] + plan["milestone_tracks"]
              + plan["trust_badges"] + plan["bar_charts"] + plan["milestone_unlocks"]):
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


def _sparse_gap_quote_candidates(gaps: list[tuple[int, int]], segments: list[dict]) -> list[dict]:
    """Fix C28（2026-07-20，真实生产复现——job_f7b171f8d952，纯叙事内容全片
    0 个数据卡/仪表盘/倒计时/日历，_sparse_gaps 连续 3 轮重规划都没修好）：
    criterion 2 是四条规划质量标准里唯一一条至今没有确定性兜底的——
    intro_lead_dead_space（Fix C13）、section_takeover_lacks_content（Fix C15）
    都已经从"指望 LLM 这次会听话"换成了机械兜底，这条一直没有，Rule 13 的
    教训在这里同样成立。

    直接机械插入一张复读字幕原文的 topic_card 会重蹈 `_fallback_topic_cards_
    for_gaps` 的覆辙——那正是被用户明确否决、专门删掉的做法（"popups for the
    sake of having them"）。这里改用已经验证过、专门为"数据稀薄的视频"设计的
    quotes 机制（f60731c "Data-less videos get canvas motion"提交）：QuoteCard
    是一次真正的排版时刻（强调引号、逐字揭示、下划线扫过），不是简单复读——
    跟被删掉的那个 fallback 不是同一类东西，REPLAN_SYSTEM_PROMPT 自己也把
    quote 列为可用选项之一。

    唯一的问题是它要求"genuinely striking"——对纯叙事、没有任何数字/日期
    可讲的内容，LLM 卡在"没资格用 quote"和"没资格用 topic_card"之间，三轮
    都在正确地拒绝硬造内容，而不是真的偷懒。这里退一步：不再要求"striking"，
    机械选这段空档时间里最长的一整句转写原文（越长的句子越可能是实质性
    表达，不是语气词/过渡句）。找不到任何一句能塞进 QuoteCard 80 字符上限
    的完整句时，这个空档直接跳过——宁可保持"只有说话人+字幕"，也不要插入
    一句被腰斩、读不完整的话。
    """
    candidates = []
    for gap_start, gap_end in gaps:
        gap_start_s, gap_end_s = gap_start / FPS, gap_end / FPS
        best_seg = None
        for seg in segments:
            text = str(seg.get("text", "")).strip()
            if not text or len(text) > 80:
                continue
            try:
                start_s = float(seg.get("start", 0))
                end_s = float(seg.get("end", start_s))
            except (TypeError, ValueError):
                continue
            mid_s = (start_s + end_s) / 2
            if not (gap_start_s <= mid_s <= gap_end_s):
                continue
            if best_seg is None or len(text) > len(str(best_seg.get("text", ""))):
                best_seg = {"text": text, "start_seconds": start_s}
        if best_seg is not None:
            candidates.append({
                "visual": "quote", "seconds": best_seg["start_seconds"], "text": best_seg["text"],
            })
    return candidates


def _apply_richness_floor(raw: dict, plan: dict, segments: list[dict],
                          duration: float, allow_replan: bool = True,
                          word_timestamps: Optional[list[dict]] = None) -> dict:
    """[已被 plan_content 的 criterion loop 取代——密度不足现在是循环里的一条
    标准（_plan_quality_failures 第 2 条），跟其它标准一起在每轮重规划里统一
    修复，而不是单独补救一次。保留此函数供既有单测使用（gap 检测/接受语义
    仍然正确），生产代码不再调用。]"""
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
        logger.debug(f"content_planner: 补规划原始 data_points = {json.dumps((extra or {}).get('data_points'), ensure_ascii=False)}")
        if extra and isinstance(extra.get("data_points"), list):
            # 标记成 _gap_fill——这批本来就是"专门去填一个已侦测到的空档"而
            # 补的内容，不该被更早、跟它毫无关系的一段悄悄拼在同一个堆叠里
            # 共享退场时间（见堆叠逻辑里 is_gap_fill 的用法和那段真实生产 bug
            # 的说明：job_1237c9c59bc0，countdown 被一张晚了 11s 才出现的兜底
            # 卡片硬拖着多播了 12 秒）。
            new_points = [dp for dp in extra["data_points"] if isinstance(dp, dict)]
            for dp in new_points:
                dp["_gap_fill"] = True
            data_points += new_points
            plan = _to_frame_plan({**raw, "data_points": data_points}, duration)
            gaps = _sparse_gaps(plan, duration)
            if not gaps:
                logger.info("content_planner: 补规划一轮后密度达标")
                return plan

    # 仍有空档——曾经在这里机械地从转写原文切一段塞进 topic_card 当兜底
    # (_fallback_topic_cards_for_gaps，已删除)。用户明确反馈：这种卡片的
    # 内容就是原话，跟屏幕下方本来就在滚动的字幕一字不差，纯粹"为了有画面
    # 而有画面"，没有任何附加信息量。宁可这段时间就只有说话人+字幕（真实存在
    # 的素材允许的话，上面的 LLM 补规划已经优先尝试过给出真正有信息量的图形
    # 了），也不要一张读起来跟字幕一样的卡片。
    if gaps:
        remaining = ", ".join(f"{a / FPS:.0f}s-{b / FPS:.0f}s" for a, b in gaps)
        logger.info(f"content_planner: 补规划后仍有空档，接受（不再机械垫字幕卡片）: {remaining}")
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
- Stutters and self-corrections: if the speaker restarts a phrase or repeats themselves to get it right, cut the ENTIRE earlier failed attempt, not just the part where it visibly breaks off. A common mistake: the speaker gets partway through a sentence cleanly, stumbles or trails off, then restarts and says the WHOLE thing again from the top in a complete, clean version. It's tempting to only cut the broken tail (since the opening words sounded fine on their own) — but if that opening content is said again in the clean restart, the entire earlier attempt is now redundant and must be cut in full, back to wherever the repeated content begins — not just the part that breaks off. Test: after your cuts, read the remaining transcript straight through — if any clause or sentence's content still appears twice, you under-cut; extend the cut range backward to the start of that clause. Example: ORIGINAL "...your premium is $8,400. I've put the full breakdown in this video. So you can have —" [restart] "I've put the full breakdown in this video so you have everything in one place." → WRONG: cut only "So you can have" (leaves "I've put the full breakdown in this video" duplicated). RIGHT: cut the entire first "I've put the full breakdown in this video. So you can have" — the whole failed attempt — keeping only the second, complete instance.
- Do NOT cut: meaningful words, correct sentences, or pauses that are just natural speech rhythm (that is a separate, silence-only cleanup step — you are only removing WORDS the speaker didn't mean to leave in, not silence).
- Be conservative about content that is NOT repeated elsewhere: if you're not sure a word is filler and it doesn't reappear in a cleaner form later, keep it. But conservatism does not apply to the repeated-clause case above — once content is confirmed to reappear in a cleaner later version, the earlier instance must be cut in full, even if individual words in it looked fine in isolation.
- Cuts must be at word boundaries — you can only cut whole words from the numbered list, never partial words.

Output ONLY valid JSON, no markdown, no prose:
{
  "cut_word_indices": [12, 13, 45]
}
If nothing needs cutting, return {"cut_word_indices": []}."""


def _plan_filler_removal_once(words: list[dict], *, feedback: Optional[str] = None) -> set[int]:
    """单次口误/重录判断 -> LLM 判断为口误的词在传入 words 列表里的下标集合。

    不含 keep_ranges 构建、不含事后复核——见 plan_filler_removal。返回下标而
    不是 keep_ranges，是为了让调用方能在"当前仍保留的词范围"内重新判断
    （下标相对那个子列表），再把结果映射回原始下标跟已有的裁剪取并集，实现
    跨轮次单调递增（见 plan_filler_removal 的文档）。
    """
    numbered = "\n".join(f"{i}: {w['word']} [{w['start']:.2f}-{w['end']:.2f}]" for i, w in enumerate(words))
    if feedback:
        numbered = (
            f"NOTE: a previous pass at this exact task missed the following issue — "
            f"make sure it's addressed this time: {feedback}\n\n{numbered}"
        )

    raw = _call_llm_json("口误检测", FILLER_SYSTEM_PROMPT, numbered, temperature=0.1,
                         model=get_config().llm_model_long_output)
    if raw is None:
        return set()

    try:
        cut_indices = {int(i) for i in (raw.get("cut_word_indices") or []) if 0 <= int(i) < len(words)}
    except Exception as e:
        logger.warning(f"content_planner: 口误检测结果解析失败，跳过: {e}")
        return set()

    logger.info(f"content_planner: 口误检测 -> 判定剪掉 {len(cut_indices)} 个词（本轮 {len(words)} 词范围内）")
    return cut_indices


def _keep_ranges_from_cuts(words: list[dict], cut_indices: set[int], duration: float) -> list[dict]:
    """words（原始词表，含时间戳）+ 需要剪掉的原始下标集合 -> 合并后的保留片段
    列表（已加 padding，可以直接喂给 VideoTrimmer）。"""
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


def _subtract_spans(keep_ranges: list[dict], cut_spans: list[dict]) -> list[dict]:
    """从 keep_ranges 里再挖掉 cut_spans——用于机械兜底事后追加裁剪，不用重新
    走一遍 LLM 的 cut_indices 逻辑。"""
    result: list[tuple[float, float]] = []
    for r in keep_ranges:
        segments = [(r["start_seconds"], r["end_seconds"])]
        for cs in cut_spans:
            cs_s, cs_e = cs["start_seconds"], cs["end_seconds"]
            new_segments = []
            for s, e in segments:
                if cs_e <= s or cs_s >= e:
                    new_segments.append((s, e))
                    continue
                if cs_s > s:
                    new_segments.append((s, cs_s))
                if cs_e < e:
                    new_segments.append((cs_e, e))
            segments = new_segments
        result.extend((s, e) for s, e in segments if e > s)
    result.sort(key=lambda p: p[0])
    return [{"start_seconds": s, "end_seconds": e} for s, e in result]


_CLAUSE_END_RE = re.compile(r"[.?!]$")
_WORD_CHARS_RE = re.compile(r"[^\w']")


def _split_into_clauses(kept_words: list[dict]) -> list[list[dict]]:
    clauses: list[list[dict]] = []
    cur: list[dict] = []
    for w in kept_words:
        cur.append(w)
        if _CLAUSE_END_RE.search(w["word"].strip()):
            clauses.append(cur)
            cur = []
    if cur:
        clauses.append(cur)
    return clauses


def _clause_signature(clause: list[dict]) -> set[str]:
    return {t for w in clause if (t := _WORD_CHARS_RE.sub("", w["word"].lower()))}


def _dedupe_repeated_clauses(words: list[dict], keep_ranges: list[dict]) -> list[dict]:
    """机械兜底：LLM 判断"重录该剪多少"时，容易只剪掉明显断掉的尾巴，漏剪前面
    单独看语法通顺、但内容在后面被完整重说了一遍的部分（真实事故：2026-07-16
    用户反馈"重复的话还是没剪掉"，加强过 prompt + 明确给了同款反例仍未收敛——
    flash 档模型对这种"往后扫描确认重复、再回头改前面已经做的裁剪"的推理不够
    稳，靠 prompt 措辞压不住，改用不依赖模型能力的确定性兜底）。

    把保留下来的内容按句末标点切成从句，两两比较词汇重叠度；重叠度高的判定为
    同一次内容的两次表达，只留后一个（说话人的最终修正版），前一个整段追加进
    裁剪——不管 LLM 那一轮判断有没有意识到这是重录。
    """
    kept = _words_in_keep_ranges(words, keep_ranges)
    clauses = _split_into_clauses(kept)
    if len(clauses) < 2:
        return keep_ranges

    redundant_spans = []
    for i in range(len(clauses)):
        if len(clauses[i]) < 3:  # 太短的从句（如"Take care."）词汇重叠天然就高，跳过避免误伤
            continue
        sig_i = _clause_signature(clauses[i])
        if not sig_i:
            continue
        for j in range(i + 1, len(clauses)):
            sig_j = _clause_signature(clauses[j])
            if not sig_j:
                continue
            # 用"容纳率"（更短从句的词有多少比例被更长从句覆盖）而不是 Jaccard——
            # 重录的后半段通常比前半段更完整（加了没说完的结尾），用 max 分母的
            # Jaccard 算出来的重叠度会被拉低，min 分母才能准确反映"包含关系"。
            overlap = len(sig_i & sig_j) / min(len(sig_i), len(sig_j))
            if overlap >= 0.7:
                redundant_spans.append({
                    "start_seconds": clauses[i][0]["start"],
                    "end_seconds": clauses[i][-1]["end"],
                })
                logger.info(
                    "content_planner: 机械兜底检出重复从句，追加剪掉更早的版本: "
                    f"'{' '.join(w['word'] for w in clauses[i])}'"
                )
                break  # i 已判定冗余，不用再跟后面的从句比较

    if not redundant_spans:
        return keep_ranges
    return _subtract_spans(keep_ranges, redundant_spans)


# 单个词正常发音很少超过这个时长（哪怕说话人刻意拖长）。超过的部分极可能是
# Whisper word-level 强制对齐把一段没有转写出文字的音频错误地记在了这个词
# 头上——不是这个词真的说了这么久。
#
# 确认过的真实误伤（2026-07-24）：原阈值 1.2s 太紧——一段真实视频里单词
# "Cloud"（"Cloud Code" 的一部分）被 Whisper 报了 1.56s，压过阈值触发强制
# 裁剪，切掉了 16.72-17.08s 这 0.36 秒，正好切进 "Cloud Code" 里；被切过的
# 音频重新转写后变成听不懂的 "CodeCode"/"like Code."，比原始转写还烂——
# 这条安全网本身把干净的音频弄脏了。原始动机的事故是被吞掉整句话、时长
# 接近 5 秒，2.2s 这个新阈值依然能拦住那类真正的异常，同时不再误伤"略慢
# 但真实存在"的正常词。
_MAX_PLAUSIBLE_WORD_DURATION = 2.2

# 置信度兜底（2026-07-24，同一次真实误伤调查的后续）：一开始想用"低置信度
# 才裁剪"当第二道保险，但拿真实数据一测发现这个直觉是反的——上面那个被误伤
# 的 "Cloud" 本身 probability 只有 0.320，跟"确实听不清/不常见词"的置信度
# 区间完全重叠，不是"转写有把握但时长算错"那种能被置信度区分出来的情况。
# faster-whisper 的置信度反映的是"这个词是不是训练分布里常见的词"，不是
# "这段时间戳对不对"——"Cloud Code"这种不常见专有名词，哪怕两个字都听对了，
# 置信度天然就偏低。所以这里没有用"低置信度"当裁剪的理由，而是反过来：只有
# 置信度低到几乎等于"模型自己都不知道这是什么"（≤0.15，比一般生僻词/专有
# 名词的置信度还低一截）才裁剪——绝大多数真实存在但少见的词会被保护下来，
# 只有真正对齐失败、内容成谜的那种极端情况才会触发。
_UNACCOUNTED_AUDIO_MAX_CONFIDENCE = 0.15


def _flag_unaccounted_audio(words: list[dict]) -> list[dict]:
    """兜底：真实事故（2026-07-16）——同一句话说话人重说了一遍，Whisper 转写
    整条长视频时在解码层面把重复的这段话整体吞掉（一个字都没出现在词表
    里），但强制对齐还是要给这段音频找个落脚点，于是全扣在了相邻词
    "or" 头上，把它的时长标成了将近 5 秒（正常应 0.1-0.2 秒）。cut_word_indices
    只能对"词表里出现的词"做判断，这段话从没作为词出现过，LLM 和后面的
    机械兜底（_dedupe_repeated_clauses，按文本比较）都无从判断、无从剪——
    结果这段没人审查过的音频靠这个超长时长被原样带进了成片。

    这里直接在源头拦截：扫出任何时长异常**且**置信度低到几乎为零的词，把
    超出合理时长之后的部分当作"不知道是什么内容，默认不能进成片"，转成
    强制裁剪区间。时长异常单独一个条件不够——见 _UNACCOUNTED_AUDIO_MAX_
    CONFIDENCE 的说明，必须两个信号同时成立才裁剪，宁可漏放过一段真正的
    异常，也不能再重演"把真实存在的生僻词当垃圾切掉"的真实事故。
    ElevenLabs 转写路径不提供置信度（见 _transcribe_elevenlabs），缺失时
    按 0.0 处理（最不确定），保留原有的纯时长防护，不因为换了转写源就
    悄悄弱化这道安全网。
    """
    spans = []
    for w in words:
        dur = w["end"] - w["start"]
        if dur <= _MAX_PLAUSIBLE_WORD_DURATION:
            continue
        prob = w.get("probability", 0.0)
        if prob > _UNACCOUNTED_AUDIO_MAX_CONFIDENCE:
            logger.info(
                f"content_planner: 词 '{w['word']}' 时长异常({dur:.2f}s)但置信度"
                f"({prob:.2f})不算低到离谱，判定为真实存在的生僻词/专有名词，不裁剪"
            )
            continue
        excess_start = w["start"] + _MAX_PLAUSIBLE_WORD_DURATION
        spans.append({"start_seconds": excess_start, "end_seconds": w["end"]})
        logger.warning(
            f"content_planner: 词 '{w['word']}' 时长异常({dur:.2f}s @ "
            f"{w['start']:.2f}-{w['end']:.2f})且置信度极低({prob:.2f})，疑似转写"
            f"吞掉了一段未知内容，强制裁掉 {excess_start:.2f}-{w['end']:.2f}"
        )
    return spans


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


_DUP_MIN_NGRAM = 4  # 判定"重复短语"至少要匹配这么多个连续词，短于此容易误伤自然重复用语
_DUP_MAX_GAP_SECONDS = 15.0  # 两次出现的起点间隔超过这个就不算"半途重录、后面重说一遍"


def _normalize_word_for_dup(w: str) -> str:
    return re.sub(r"[^\w']", "", w).lower()


def _cut_duplicate_phrases(words: list[dict], cut_indices: set[int]) -> set[int]:
    """确定性重复短语检测（不靠 LLM）——plan_filler_removal 的最后一道兜底。

    在"剪完后实际会播放"的词序列（words 里不在 cut_indices 的部分，按原始
    顺序）里找长度 >= _DUP_MIN_NGRAM 的重复短语，且第二次出现的起点在第一次
    起点的 _DUP_MAX_GAP_SECONDS 秒以内——这是"话说到一半重录、紧接着又完整
    说了一遍"的典型间距，跟真正的修辞性重复（通常间隔远得多，或逐词并不
    相同）区分开。命中就把第一次出现的整段（从它的起点到第二次出现的起点，
    含两次出现之间的任何词——那是被放弃的第一次尝试的尾巴）标记为剪掉：
    重录的惯例是后一次才是干净的版本。

    确认过的真实生产 bug（David 视频真实渲染，job_e44166eb8c38）：口误复核
    重试 2 次后仍报告 2 处遗留重复（"I've put the full breakdown in this
    video" 和 "If you have any questions, just WhatsApp me directly" 各说了
    两遍），但重试耗尽后管线照常交付了带着两处重复的版本——这里不管 LLM 判断
    结果如何，都用纯规则再扫一遍，兜住"复核本身也漏判"这类失败。

    返回新增需要剪掉的原始下标集合（不包含 cut_indices 里已有的）。
    """
    kept_order = [i for i in range(len(words)) if i not in cut_indices]
    n = len(kept_order)
    if n < _DUP_MIN_NGRAM * 2:
        return set()
    norm = [_normalize_word_for_dup(words[i]["word"]) for i in kept_order]

    newly_cut: set[int] = set()
    i = 0
    while i <= n - _DUP_MIN_NGRAM:
        gram = tuple(norm[i:i + _DUP_MIN_NGRAM])
        if any(not tok for tok in gram):
            i += 1
            continue
        first_start = words[kept_order[i]]["start"]
        match_j = None
        j = i + _DUP_MIN_NGRAM
        while j <= n - _DUP_MIN_NGRAM:
            if words[kept_order[j]]["start"] - first_start > _DUP_MAX_GAP_SECONDS:
                break
            if tuple(norm[j:j + _DUP_MIN_NGRAM]) == gram:
                match_j = j
                break
            j += 1
        if match_j is not None:
            for pos in range(i, match_j):
                newly_cut.add(kept_order[pos])
            i = match_j  # 跳到幸存的（干净的）那一份继续扫描
        else:
            i += 1
    return newly_cut


def plan_filler_removal(words: list[dict], duration: float) -> list[dict]:
    """转写词级时间戳 -> 保留片段列表（喂给 VideoTrimmer 的 concat 操作）。

    跟 remove_silences（纯静音检测）是两码事：这里判断的是"这个词是不是口误/
    语气词/重录的失败尝试"，静音检测测不到有声的"呃""嗯"，也测不到中间没停顿
    的重录。没配 LLM 或调用失败时返回空列表（调用方应该跳过这步，不要因为这个
    可选的精修步骤失败就搞垮整条剪辑流程）。

    加了一次事后复核（verify_filler_removal）：单次判断可能漏掉真实存在的重录
    （确认过的真实 bug——判断没通过任何检查就直接交付）。复核发现问题就把问题
    喂回去重新判断；最多重试 FILLER_VERIFY_MAX_RETRIES 次。

    两处关键修复（都是从同一个真实生产 bug 追出来的——job_e44166eb8c38 最终
    交付的版本里两句话各说了两遍，服务端日志显示三轮判断剪掉的词数是
    24 -> 28 -> 15，说明第 3 轮是对着"当前仍保留的全部词"从头重新判断，把
    第 2 轮已经正确剪掉的重录又判定为"没问题"留了下来）：

    1. **单调重试**：不再每轮都对全量转写重新判断。第 1 轮照常对全量词判断；
       之后每一轮只把"当前仍保留"的词子集喂给 LLM（下标相对子集，用后再映射
       回原始下标），返回的裁剪结果跟已有的取**并集**——已经判定剪掉的词
       永远不会被后续轮次"判回来"。
    2. **交付遗留问题最少的一次尝试**：重试耗尽时不再无条件交付"最后一轮"的
       结果（哪怕它比某个更早的中间状态还差），而是交付复核过程中遗留问题数
       最少的那个状态。

    复核 schema 是"issues"（列表），不是单条"issue"——真实生产数据验证过一份
    转写里可以同时有 3 处遗留重录，早先的单条 issue schema 一次只能报一条，
    重试时只喂回其中一条问题，另外两条从没被 LLM 看见过、自然也没被剪掉。这里
    每轮把复核返回的全部 issues 拼接喂回去，而不是只取第一条。

    最后无论上面走到哪一步，都跑两道跟 LLM 判断完全独立的确定性兜底：
    - _cut_duplicate_phrases：n-gram 级精确重复 + 时间邻近窗口，抓"话说到
      一半重录、紧接着又完整说了一遍"这种紧凑重复。
    - _dedupe_repeated_clauses（每轮 LLM 判断后都跑，不等复核触发）：从句级
      词汇重叠/包含率，不受时间邻近限制，抓"LLM 只剪掉了重录明显断掉的
      尾巴，漏剪前面语法通顺、但内容在后面被完整重说"的更松散的重复。
    两者检测粒度和触发条件不同，互不覆盖，都保留。

    另外，_flag_unaccounted_audio 扫出时长离谱的词（疑似 Whisper 强制对齐
    把一段没转写出文字的重复音频整段吞掉、扣在了相邻词头上），把这些词
    从没作为文字出现过、cut_word_indices 根本无法判断的内容，在最终结果上
    强制裁掉——跟上面所有基于文本/下标的检测都是互补关系，不是替代。
    """
    if not words:
        logger.info("content_planner: 没有词级时间戳，跳过口误检测")
        return []

    unaccounted_spans = _flag_unaccounted_audio(words)

    # cut_indices 为空（没有任何词需要剪）时统一返回 [] 而不是"从 0 到 duration
    # 的一整段"——两者对 VideoTrimmer 而言效果一样，但调用方 _op_remove_filler
    # 用 `if not keep_ranges` 当作"什么都不用剪，跳过这一步"的哨兵值，保留这个
    # 约定能省掉一次没意义的 concat/重新编码。
    def _finalize(indices: set[int]) -> list[dict]:
        if not indices:
            ranges: list[dict] = []
        else:
            ranges = _keep_ranges_from_cuts(words, indices, duration)
        ranges = _dedupe_repeated_clauses(words, ranges)
        return _subtract_spans(ranges, unaccounted_spans) if unaccounted_spans else ranges

    cut_indices = _plan_filler_removal_once(words)
    best_cut_indices = set(cut_indices)
    best_issue_count: Optional[int] = None

    for attempt in range(FILLER_VERIFY_MAX_RETRIES + 1):
        # C44: 这一步必须在每一轮 verify 之前跑，不能只在重试耗尽后当兜底——
        # 之前的写法只有"重试全部失败"这一条路径才会走到文件末尾的
        # _cut_duplicate_phrases 调用；只要 LLM 复核在第 1 轮就判"clean"
        # （复核本身会看漏，跟 remove_filler 漏判是同一类错误），或者复核调用
        # 失败返回 None（约定当作"假定通过"），下面这行永远执行不到，确定性
        # 兜底就变成了死代码。真实生产 bug（job_452ef6c48100）：口误复核第
        # 1 轮就判 clean，"If you have any questions, just WhatsApp me
        # directly" 的重录整段原样播出。
        dup_cut = _cut_duplicate_phrases(words, cut_indices)
        if dup_cut:
            logger.warning(
                f"content_planner: 确定性重复短语检测追加剪掉 {len(dup_cut)} 个词"
                "（LLM 判断还没来得及/没抓到）"
            )
            cut_indices = cut_indices | dup_cut

        keep_ranges = _finalize(cut_indices)
        review = verify_filler_removal(words, keep_ranges)
        if review is None or review.get("clean", True):
            return keep_ranges

        issues = review.get("issues")
        if not issues:
            single = review.get("issue")
            issues = [single] if single else []
        issues = [str(i)[:200] for i in issues if i]

        if best_issue_count is None or len(issues) < best_issue_count:
            best_issue_count = len(issues)
            best_cut_indices = set(cut_indices)

        if attempt >= FILLER_VERIFY_MAX_RETRIES:
            logger.warning(
                f"content_planner: 重试 {FILLER_VERIFY_MAX_RETRIES} 次后口误复核仍不通过，"
                f"交付遗留问题最少的一次尝试（而不是最后一次）: {issues}"
            )
            cut_indices = best_cut_indices
            break

        feedback = "; ".join(issues) if issues else "unspecified leftover issue"
        logger.warning(f"content_planner: 口误复核发现 {len(issues)} 处遗留问题，重新判断一次: {feedback}")

        kept_orig_indices = [i for i in range(len(words)) if i not in cut_indices]
        kept_words = [words[i] for i in kept_orig_indices]
        new_cut_in_kept = _plan_filler_removal_once(kept_words, feedback=feedback)
        cut_indices = cut_indices | {kept_orig_indices[i] for i in new_cut_in_kept}

    # 循环内每一轮都已经跑过 _cut_duplicate_phrases（见上方 C44 注释），
    # best_cut_indices 取的就是那时已经去重过的状态，这里不用再跑一遍。
    return _finalize(cut_indices)

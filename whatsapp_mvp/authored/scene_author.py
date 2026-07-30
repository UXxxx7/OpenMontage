#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6 · SceneAuthor / FeedbackReviser —— 现写场景 + 看帧改代码(设计文档 §2.2/§2.6)。

实验脚本 arm_b_author_scene.py / arm_b_feedback.py 的产品化收敛:去 CLI 外壳,
输入输出变成干净的函数契约,LLM 传输层可注入(测试不联网,真调在用户机 smoke)。

两个入口:
    author_scene(ctx, llm_call=None)  -> AuthorResult   # 首次现写
    revise_scene(tsx, defects, ctx, notes="", llm_call=None) -> AuthorResult  # 修订
ctx = AuthorContext(transcript segments/words/duration、instruction、
                    example_images、broll 元数据、画幅)
defects 直接吃 M2 QAVerdict.defects 的形态({t0,t1,kind,desc,frame_path})——
M2 的输出就是本模块修订输入,契约咬合。

冻结提示:创作提示含第 8 条"全时间轴有效"通用不变量(黑屏事故后加的预防);
修订提示锁定"只修 bug、不重设计、Props 契约不变"。实验期间不按条微调。

传输层:llm_call(messages, max_tokens, temperature) -> {"content", "usage"}。
不传则用默认 requests 实现(AUTHOR_LLM_* 环境变量,回退仓库 VISION_LLM_* 配置)。
LLM 报错/超时不外抛 —— AuthorResult.ok=False + error,Orchestrator 靠返回值驱动。

计费:Gemini 思考型模型 total 含 thinking(按输出价计费),
billable_out = total - prompt。与实验期口径一致,便于成本对比。
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MAX_TOKENS = 32000        # 思考型模型 thinking 与输出共用预算,8000 会被吃光截断(实测)
TEMP_AUTHOR = 0.3
TEMP_REVISE = 0.2         # 修订更保守,别乱发挥
# describe 输出很短,但思考型模型 thinking 也吃这份预算 —— 必须给足,否则 thinking
# 花光预算、真正输出为空 → JSON 解析失败被吞成 {}(离线本地测抓到)。与 author 同档。
DESCRIBE_MAX_TOKENS = MAX_TOKENS

# ─────────────────────────── 冻结的系统提示 ───────────────────────────

AUTHORING_SYSTEM_PROMPT = """You are a senior motion-graphics engineer. You write a SELF-CONTAINED Remotion composition (TypeScript/TSX) that edits ONE portrait talking-head video into a polished short — captions, tasteful motion, and b-roll inserts — bespoke for THIS video's content and the user's intent.

You are NOT filling a template or a fixed schema. You decide the layout, the animations, when and how b-roll appears, what on-screen text/cards to add, and their timing — grounded ONLY in what this transcript actually says and what the user asked for. Imitate the look/feel of the reference image(s) the user provides.

OUTPUT CONTRACT — the file you write MUST:
1. Be a single valid .tsx file, default-exporting a React component named `AuthoredScene`.
2. Import only from "react" and "remotion" (AbsoluteFill, OffthreadVideo, Sequence, useCurrentFrame, useVideoConfig, interpolate, spring, staticFile, Img). Do NOT import any project-local components or external libraries — everything self-contained, inline styles only.
3. Read everything it needs from a single props object of THIS exact shape (already provided at render time — do not invent other props):
   type Props = {
     videoSrc: string;
     broll: { src: string; label: string; startFrame: number; endFrame: number }[];
     words: { word: string; start: number; end: number }[];
     fps: number;
     durationInFrames: number;
     width: number; height: number;
   };
   The component signature is: `export const AuthoredScene: React.FC<Props> = (props) => { ... }` plus `export default AuthoredScene;`.
4. Render the person video full-timeline as the base (OffthreadVideo src={videoSrc}). During a b-roll window, show that b-roll prominently — landscape b-roll must NOT be side-cropped; contain it and fill the rest with a blurred copy of the same frame.
5. Burn in captions from `words`, grouped into short readable phrases in sync with the spoken words. Keep them legible (safe-area bottom third, high contrast).
6. Add a few — not many — on-screen graphic beats ONLY where the transcript genuinely warrants one, anchored to the words being spoken. Match the reference image's aesthetic.
7. Use spring()/interpolate() for entrances; nothing pops in hard. Everything deterministic from `frame` (no Date/random).
8. WHOLE-TIMELINE VALIDITY (critical, general): every visual state must be correct across the ENTIRE duration, not only inside the window it was designed for. For any element whose state changes around a b-roll window (e.g. the person shrinking into a PiP inset), explicitly define its state for BEFORE, DURING, and AFTER that window. An inset/PiP that shrinks the person MUST default to person-fills-the-card whenever no b-roll is active — never leave a card or container showing only its (black) background. Concretely: do NOT gate a transition on a single spring whose value before its trigger frame yields the wrong state (e.g. `1 - spring(frame - endFrame)` is 1 before the window starts). Prefer building progress as (enterSpring at windowStart − exitSpring at windowEnd) so it is 0 before, 1 during, 0 after. Mentally trace frame 0, mid-clip, and the final frame and confirm every region shows real content.

Return ONLY the raw .tsx file content. No markdown fences, no prose, no explanation."""

REVISION_SYSTEM_PROMPT = """You are a senior motion-graphics engineer REVISING an existing Remotion composition (AuthoredScene.tsx) that you wrote earlier. You are shown DEFECTS detected on its ACTUAL RENDER (machine-scanned regions and/or user notes, with captured frames), plus the current code. Fix the specific defects. This is a debugging pass, not a rewrite.

RULES:
1. KEEP everything that already works (layout, colors, captions, the reference look). Change ONLY what the defects show to be broken. Do not restyle or re-theme.
2. The frames are ground truth. A common bug: a state/animation gate is inverted or only handles the AFTER case, so a section (e.g. BEFORE a b-roll window) shows a blank/black card or a mis-sized element. For BEFORE/DURING/AFTER each window, reason what each region SHOULD show. A PiP that shrinks the person must default to person-fills-the-card when no b-roll is active — `1 - spring(frame - endFrame)` is 1 before the window and wrongly shrinks the person from frame 0; rebuild as (enterSpring at start − exitSpring at end). Treat each machine-flagged region as a confirmed defect.
3. The Props contract is UNCHANGED and fixed. `export const AuthoredScene: React.FC<Props>` + `export default AuthoredScene;`. Import only from "react" and "remotion". Self-contained, deterministic from `frame`.
4. Landscape b-roll never side-cropped (contain + blurred fill). Person video plays full timeline. Captions stay in sync from `words`.
5. Return ONLY the full corrected .tsx file content — the entire file, not a diff, no markdown fences, no prose."""


# ─────────────────────────── 数据契约 ───────────────────────────

@dataclass
class AuthorContext:
    segments: list                 # [{start, text}]  转写分句
    words: list                    # [{word, start, end}]
    duration_s: float
    instruction: str = ""
    example_images: list = field(default_factory=list)   # 风格参考图路径(含参考素材代表帧)
    broll: list = field(default_factory=list)            # [{src,label,startFrame,endFrame}]
    style_spec: dict = field(default_factory=dict)       # 参考素材风格谱(StyleReference,可空)
    width: int = 1080
    height: int = 1920
    fps: int = 30


@dataclass
class AuthorResult:
    ok: bool
    tsx: str = ""
    contract_markers: bool = False   # 粗检:AuthoredScene + export default 都在
    usage: dict = field(default_factory=dict)  # {prompt, completion, thinking, total}
    error: str = ""


# ─────────────────────────── 组装基元 ───────────────────────────

def _img_part(p: Path) -> dict:
    b64 = base64.b64encode(p.read_bytes()).decode()
    ext = p.suffix.lower().lstrip(".") or "png"
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    return {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}}


def _transcript_text(segments: list, limit: int = 12000) -> str:
    lines = []
    for s in segments or []:
        try:
            lines.append(f"[{float(s['start']):.1f}s] {str(s.get('text', '')).strip()}")
        except (KeyError, TypeError, ValueError):
            continue
    return "\n".join(lines)[:limit]


def _broll_desc(broll: list) -> str:
    return "\n".join(
        f"  - src={b.get('src')!r} label={b.get('label')!r} "
        f"suggested frames {b.get('startFrame', 0)}-{b.get('endFrame', 0)}"
        for b in broll or []) or "  (none)"


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip() + "\n"


def _usage_from(raw: dict) -> dict:
    u = raw or {}
    pt, ct, tt = u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens")
    total = tt if tt is not None else (pt or 0) + (ct or 0)
    thinking = (tt - (pt or 0) - (ct or 0)) if (tt is not None and ct is not None) else 0
    return {"prompt": pt or 0, "completion": ct or 0,
            "thinking": max(0, thinking or 0), "total": total or 0}


def cost_usd(usage: dict, price_in: float, price_out: float) -> float:
    """thinking 按输出价计费:billable_out = total - prompt。"""
    billable_out = max(0, usage.get("total", 0) - usage.get("prompt", 0))
    return usage.get("prompt", 0) / 1e6 * price_in + billable_out / 1e6 * price_out


# ─────────────────────────── 消息组装 ───────────────────────────

def build_author_messages(ctx: AuthorContext) -> list:
    content: list = []
    for ip in ctx.example_images:
        p = Path(ip)
        if p.exists():
            content.append({"type": "text", "text": "REFERENCE IMAGE (imitate this look):"})
            content.append(_img_part(p))
    if ctx.broll:
        broll_block = (
            f"B-ROLL clips available — these are REAL uploaded video files in prop `broll` "
            f"({len(ctx.broll)} clip(s)). You MUST composite each one into its window with "
            f"OffthreadVideo (src={{b.src}}) — do NOT replace an uploaded clip with a drawn "
            f"card/graphic. The startFrame/endFrame below are suggested windows; keep them, or "
            f"shift a clip to better match its label against the words being spoken:\n"
            f"{_broll_desc(ctx.broll)}"
        )
    else:
        broll_block = ("B-ROLL clips: (none uploaded) — do not fabricate video b-roll; "
                       "any on-screen graphics must be drawn cards/text only.")
    # 参考素材风格谱(StyleReference,模块3):有就加一段"参考风格"指令,冲突以参考为准、不抄内容。
    style_block = ""
    if getattr(ctx, "style_spec", None):
        try:
            try:
                from .style_reference import render_style_reference_block
            except ImportError:
                from style_reference import render_style_reference_block
            style_block = render_style_reference_block(ctx.style_spec)
        except Exception:  # noqa: BLE001 —— 参考段渲染失败不拖垮现写
            style_block = ""
    user_text = (
        f"USER INSTRUCTION (primary intent — follow it):\n{ctx.instruction or '(none)'}\n\n"
        f"VIDEO: portrait {ctx.width}x{ctx.height}, {ctx.duration_s:.1f}s, "
        f"{ctx.duration_s * ctx.fps:.0f} frames @{ctx.fps}fps.\n"
        f"{broll_block}\n\n"
        + (style_block + "\n" if style_block else "")
        + f"TRANSCRIPT (with times; use for caption timing & where graphics belong):\n"
        f"{_transcript_text(ctx.segments)}\n\n"
        f"Now write AuthoredScene.tsx per the output contract. Return ONLY the .tsx content."
    )
    content.append({"type": "text", "text": user_text})
    return [{"role": "system", "content": AUTHORING_SYSTEM_PROMPT},
            {"role": "user", "content": content}]


def build_revise_messages(tsx: str, defects: list, ctx: AuthorContext,
                          notes: str = "") -> list:
    """defects 形态 = M2 QAVerdict.defects([{t0,t1,kind,desc,frame_path}])。"""
    content: list = []
    for ip in ctx.example_images:
        p = Path(ip)
        if p.exists():
            content.append({"type": "text", "text": "TARGET STYLE REFERENCE (keep this look):"})
            content.append(_img_part(p))
    defect_lines = []
    for d in defects or []:
        t0, t1 = d.get("t0", 0), d.get("t1", 0)
        defect_lines.append(f"  - [{d.get('kind', '?')}] {t0:.1f}-{t1:.1f}s: {d.get('desc', '')}")
        fp = d.get("frame_path")
        if fp and Path(fp).exists():
            content.append({"type": "text",
                            "text": f"[CAPTURED FRAME of defect at t≈{(t0 + t1) / 2:.1f}s]"})
            content.append(_img_part(Path(fp)))
    user_text = (
        "DEFECTS ON THE ACTUAL RENDER (machine scan + user notes):\n"
        + ("\n".join(defect_lines) or "  (none machine-flagged)")
        + (f"\n\nUSER NOTES:\n{notes}" if notes else "")
        + f"\n\nVIDEO: portrait {ctx.width}x{ctx.height}, {ctx.duration_s:.1f}s @{ctx.fps}fps.\n"
        f"TRANSCRIPT:\n{_transcript_text(ctx.segments)}\n\n"
        "CURRENT AuthoredScene.tsx (fix in place, keep what works):\n"
        "```tsx\n" + tsx + "\n```\n\nReturn ONLY the full corrected .tsx."
    )
    content.append({"type": "text", "text": user_text})
    return [{"role": "system", "content": REVISION_SYSTEM_PROMPT},
            {"role": "user", "content": content}]


# ─────────────────────────── 默认传输层(requests)───────────────────────────
#
# 架构复审后新增（2026-07-29）：这里原来是自己重新写的一份 requests.post(
# timeout=240)，跟同一晚在 llm_client.py 里整晚在修的 DeepSeek 传输层是完全
# 独立的第二套实现，没有分享任何加固：
#   - timeout=240 只保证单次 socket 读取不超时，服务器间歇挤字节的话照样能
#     拖到几分钟甚至更久——跟 llm_client._post_with_retries 改之前一模一样
#     的漏洞（真实事故：DeepSeek 侧同一类调用实测卡过 17 分钟）。
#   - 每次都新开一条连接，没有复用。
# 复用 llm_client._post_bounded（后台线程 + 硬性总耗时上限，不管服务器怎么
# "挤牙膏"到点就不再等；同时带上模块级 Session 连接复用）——请求体构造/
# AUTHOR_LLM_*/VISION_LLM_* 配置回退/响应解析都不动，纯传输层换血。
#
# 硬上限**没有**直接沿用 llm_client 自己的默认值（75s，是按 DeepSeek 非流式
# 网关 ~60s 硬时限、纯 JSON 内容规划这类调用的实际时长调的）——这里的调用
# 现写/修订的是真实场景代码（MAX_TOKENS=32000，思考型模型自带 thinking
# token），明显是更重的调用，硬套一个为不同工作量调的数字，会把本该成功、
# 只是本来就需要更久的调用提前误杀，那不是这个机制原本要防的问题（防的是
# "服务器挤牙膏导致单次调用无限期卡住"，不是"限制所有调用必须多快完成"）。
# 用 `_post_bounded` 新增的 `hard_deadline_s` 参数传回原来这里的 240，保留
# 原作者对这类调用合理时长的判断，只是让它变成一个真正会生效的上限，而不
# 是"配了但挤牙膏场景下形同虚设"的数字。
def _default_llm_call(messages: list, max_tokens: int, temperature: float) -> dict:
    from whatsapp_mvp.llm_client import _post_bounded
    base = os.getenv("AUTHOR_LLM_BASE_URL", "").rstrip("/")
    key = os.getenv("AUTHOR_LLM_API_KEY", "")
    model = os.getenv("AUTHOR_LLM_MODEL", "")
    if not (base and key and model):
        try:
            from whatsapp_mvp.config import get_config
            c = get_config()
            base = base or (getattr(c, "vision_llm_base_url", "") or "").rstrip("/")
            key = key or (getattr(c, "vision_llm_api_key", "") or "")
            model = model or (getattr(c, "vision_llm_model", "") or "")
        except Exception:
            pass
    if not (base and key and model):
        raise RuntimeError("未配置多模态模型(AUTHOR_LLM_* 或仓库 VISION_LLM_*)")
    # 修复上面这行硬套"/v1"的真实 bug（2026-07-29，用真实配置直接调用时复现）：
    # 本机 VISION_LLM_BASE_URL 配的是 https://open.bigmodel.cn/api/paas/v4——
    # 不以 "/v1" 结尾，走的是 else 分支拼出 ".../v4/v1/chat/completions"，
    # 直接 404（智谱这个网关下根本没有这条路径，真实的是 ".../v4/chat/
    # completions"，不带 /v1）。llm_client.call_vision_chat 对同一个
    # VISION_LLM_BASE_URL 用的是不猜测、直接拼接的写法（这整晚一直在用、
    # 确认工作正常），这里改成同一种写法——配置了什么 base 就在后面接
    # "/chat/completions"，不再用"是否以 /v1 结尾"去猜要不要插一段 /v1。
    endpoint = base + "/chat/completions"
    resp = _post_bounded(
        endpoint, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"model": model, "messages": messages,
         "temperature": temperature, "max_tokens": max_tokens},
        240, hard_deadline_s=240)
    resp.raise_for_status()
    data = resp.json()
    return {"content": data["choices"][0]["message"]["content"],
            "usage": data.get("usage") or {}}


# ─────────────────────────── 429 退避重试 ───────────────────────────

import concurrent.futures
import time

import requests


def _is_rate_limit(e: Exception) -> bool:
    """判断异常是不是限流(Gemini/OpenAI 兼容端点的 429 / RESOURCE_EXHAUSTED)。"""
    s = str(e)
    return ("429" in s or "Too Many Requests" in s
            or "RESOURCE_EXHAUSTED" in s or "rate limit" in s.lower())


# 架构复审后新增（2026-07-29）：_invoke 原来只认限流异常可重试，网络层的
# 连接中断/超时（"Response ended prematurely"这类，2026-07-27~28 在 DeepSeek
# 那条路上反复实测过 65 次、单晚 19 次）会直接被当成"非限流异常"立即抛出、
# 不重试——传输层换成 _post_bounded 之后这类失败会更快浮现（不用再等到
# 240s），但"浮现得更快"不等于"变得可重试"，两者是独立的两件事，都要处理。
# 传输层瞬时故障和限流是同一类"该多等一下再试"的失败，只是触发条件不同，
# 复用同一套 _backoffs() 退避序列即可，不需要另开一条退避逻辑。
def _is_transient_network_error(e: Exception) -> bool:
    """判断异常是不是传输层瞬时故障（连接中断/超时/硬性总耗时上限触发），
    不是调用方内容本身的问题（比如请求体格式错、鉴权失败），这类值得重试。"""
    return isinstance(e, (
        requests.ConnectionError, requests.Timeout,
        requests.exceptions.ChunkedEncodingError, concurrent.futures.TimeoutError,
    ))


def _backoffs() -> list:
    """429/网络重试退避秒序列。默认 5,15,30(跨过 Gemini 每分钟 RPM 窗口);
    env 可覆盖,测试设 '0,0' 免真 sleep。总退避 ≤50s,落在 worker 的规划超时
    (默认 180s)内。"""
    raw = os.getenv("AUTHOR_LLM_RETRY_BACKOFF", "5,15,30")
    out = []
    for x in raw.split(","):
        try:
            out.append(max(0.0, float(x.strip())))
        except ValueError:
            continue
    return out or [5.0, 15.0, 30.0]


def _invoke(llm_call: Callable | None, messages: list, max_tokens: int,
            temperature: float) -> dict:
    """调模型;**仅对 429/限流、传输层瞬时故障**做有限退避重试(必需的
    author/revise 用)。其它异常立即抛出(不无谓重试);退避用尽仍失败则抛
    最后一次。"""
    fn = llm_call or _default_llm_call
    delays = _backoffs()
    for i in range(len(delays) + 1):
        try:
            return fn(messages, max_tokens, temperature)
        except Exception as e:  # noqa: BLE001
            if i < len(delays) and _is_rate_limit(e):
                logger.warning(f"限流(429),{delays[i]:.0f}s 后重试第 {i + 1} 次: {e}")
                time.sleep(delays[i])
                continue
            if i < len(delays) and _is_transient_network_error(e):
                logger.warning(f"传输层瞬时故障,{delays[i]:.0f}s 后重试第 {i + 1} 次: {e}")
                time.sleep(delays[i])
                continue
            raise


# ─────────────────────────── 入口 ───────────────────────────

def _call(messages: list, temperature: float,
          llm_call: Callable | None) -> AuthorResult:
    try:
        raw = _invoke(llm_call, messages, MAX_TOKENS, temperature)
    except Exception as e:  # noqa: BLE001 —— 不外抛,返回值驱动
        return AuthorResult(ok=False, error=f"{type(e).__name__}: {e}")
    tsx = _strip_fences(raw.get("content", ""))
    markers = ("AuthoredScene" in tsx) and ("export default" in tsx)
    return AuthorResult(ok=markers, tsx=tsx, contract_markers=markers,
                        usage=_usage_from(raw.get("usage")),
                        error="" if markers else "输出缺少 AuthoredScene/export default 契约标记")


def author_scene(ctx: AuthorContext, llm_call: Callable | None = None) -> AuthorResult:
    return _call(build_author_messages(ctx), TEMP_AUTHOR, llm_call)


def revise_scene(tsx: str, defects: list, ctx: AuthorContext, notes: str = "",
                 llm_call: Callable | None = None) -> AuthorResult:
    return _call(build_revise_messages(tsx, defects, ctx, notes), TEMP_REVISE, llm_call)


# ─────────────────────────── 画面设计描述(给分镜用)───────────────────────────

DESCRIBE_SYSTEM_PROMPT = """You explain the VISUAL DESIGN of a Remotion video scene to a NON-TECHNICAL user, in Chinese.
Given the scene's .tsx source, describe what the FINISHED video LOOKS like — card colors and shape, on-screen text/labels, picture-in-picture layout, captions, motion/effects. Describe the DESIGN the viewer sees, never the code or prop names.
Return ONLY compact JSON (no markdown, no prose) of exactly this shape:
{"summary": "<一两句总体画面风格>", "style": {"卡片": "<颜色/形状/特效>", "字幕": "<位置/样式>", "动效": "<入场/过渡>", "讲话人": "<铺底/PIP 布局>"}}
Each value stays under ~20 Chinese characters. Omit any key whose element isn't present in the scene."""


def build_describe_messages(tsx: str) -> list:
    return [{"role": "system", "content": DESCRIBE_SYSTEM_PROMPT},
            {"role": "user", "content": "SCENE .tsx:\n```tsx\n" + (tsx or "") +
             "\n```\n\nReturn ONLY the JSON describing what the video looks like."}]


def describe_scene(tsx: str, llm_call: Callable | None = None) -> dict:
    """读 tsx → 给用户看的画面设计描述 {summary, style}。用于分镜的 narrative_fn。
    非契约、纯描述;任何失败(未配模型/网络/解析)都返回 {} → 分镜降级为纯 derived。"""
    if not (tsx or "").strip():
        return {}
    try:
        raw = (llm_call or _default_llm_call)(build_describe_messages(tsx),
                                              DESCRIBE_MAX_TOKENS, TEMP_REVISE)
    except Exception as e:  # noqa: BLE001 —— 描述失败不外抛,但记因
        logger.warning(f"describe_scene: 模型调用失败,分镜降级为纯 derived: {type(e).__name__}: {e}")
        return {}
    txt = _strip_fences(raw.get("content", ""))
    if not txt.strip():
        logger.warning("describe_scene: 模型返回空内容(思考型模型可能把预算吃光?)"
                       f",usage={raw.get('usage')}")
        return {}
    import json
    obj = None
    try:
        obj = json.loads(txt)
    except Exception:  # noqa: BLE001 —— 容错:JSON 前后可能有噪声,截大括号再试
        s, e = txt.find("{"), txt.rfind("}")
        if 0 <= s < e:
            try:
                obj = json.loads(txt[s:e + 1])
            except Exception:  # noqa: BLE001
                obj = None
    if isinstance(obj, list):            # 模型偶尔包一层数组 → 取第一个 dict
        obj = next((x for x in obj if isinstance(x, dict)), None)
    if not isinstance(obj, dict):
        logger.warning(f"describe_scene: 无法解析成 JSON 对象,分镜降级。原文前 200 字: {txt[:200]!r}")
        return {}
    out: dict = {}
    if isinstance(obj.get("summary"), str) and obj["summary"].strip():
        out["summary"] = obj["summary"].strip()[:120]        # 防超长污染分镜文本
    if isinstance(obj.get("style"), dict):
        # value 只收标量并截断(模型可能突破"≤20 字"约束或塞进嵌套结构)
        style = {str(k)[:20]: str(v).strip()[:40] for k, v in obj["style"].items()
                 if isinstance(v, (str, int, float)) and str(v).strip()}
        if style:
            out["style"] = dict(list(style.items())[:8])      # 最多 8 项,防刷屏
    if out:
        logger.info(f"describe_scene: 画面描述已生成,usage={raw.get('usage')}")   # 成本可见
    return out
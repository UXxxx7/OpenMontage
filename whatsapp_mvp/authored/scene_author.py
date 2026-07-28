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
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MAX_TOKENS = 32000        # 思考型模型 thinking 与输出共用预算,8000 会被吃光截断(实测)
TEMP_AUTHOR = 0.3
TEMP_REVISE = 0.2         # 修订更保守,别乱发挥

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
    example_images: list = field(default_factory=list)   # 风格参考图路径
    broll: list = field(default_factory=list)            # [{src,label,startFrame,endFrame}]
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
    user_text = (
        f"USER INSTRUCTION (primary intent — follow it):\n{ctx.instruction or '(none)'}\n\n"
        f"VIDEO: portrait {ctx.width}x{ctx.height}, {ctx.duration_s:.1f}s, "
        f"{ctx.duration_s * ctx.fps:.0f} frames @{ctx.fps}fps.\n"
        f"B-ROLL clips available (prop `broll`):\n{_broll_desc(ctx.broll)}\n\n"
        f"TRANSCRIPT (with times; use for caption timing & where graphics belong):\n"
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

def _default_llm_call(messages: list, max_tokens: int, temperature: float) -> dict:
    import requests
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
    endpoint = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
    resp = requests.post(
        endpoint, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages,
              "temperature": temperature, "max_tokens": max_tokens},
        timeout=240)
    resp.raise_for_status()
    data = resp.json()
    return {"content": data["choices"][0]["message"]["content"],
            "usage": data.get("usage") or {}}


# ─────────────────────────── 入口 ───────────────────────────

def _call(messages: list, temperature: float,
          llm_call: Callable | None) -> AuthorResult:
    try:
        raw = (llm_call or _default_llm_call)(messages, MAX_TOKENS, temperature)
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
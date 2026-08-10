#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4 · StoryboardEmitter —— 用户可读分镜,精确到时间(设计文档 §2.11 / §4)。

在 compose 定稿(accept)后调用一次。产出 Storyboard(结构化)+ 可读文案。

职责边界(已评审定稿的方案 B/C):
  - **能机械回填的全机械回填**(source="derived",可验真):
      captions —— 逐句字幕,由 words 按停顿聚句,时间即 whisper 时间戳;
      brolls   —— b-roll 窗口,由 broll prop 直接换算(帧→秒);
      骨架 beats —— 开场/每个 b-roll 窗口/收尾,由时间轴推出。
  - **模型只补设计性叙述**(source="model"):布局描述、段落标签、设计意图。
    通过可注入的 narrative_fn 提供(M6 接真模型;不传则纯 derived 分镜,依然可用)。
  - **防漂移(关键)**:模型叙述是描述不是契约——凡与 derived 事实冲突的一律
    以事实为准纠正:
      · beat 时间超出片长 → 截到 [0, duration];t0>=t1 → 丢弃;
      · beat 声称的 b-roll 窗口与 broll prop 偏差 > SNAP_TOL_S → 吸附到真窗口;
      · 时间乱序 → 排序。每处纠正记入 storyboard["corrections"](可审计)。

入口:
    emit_storyboard(words, broll, duration_s, fps=30, narrative_fn=None, tsx=None)
        -> dict(Storyboard,契约见设计文档 §4)
    render_text(storyboard) -> str(给用户看的时间轴文案,markdown)
"""

from __future__ import annotations

from typing import Callable

CAPTION_GAP_S = 0.6     # 相邻词停顿超过此值 → 断句
CAPTION_MAX_WORDS = 12  # 单句最长词数(防止无停顿长流水句)
SNAP_TOL_S = 0.75       # 模型 beat 声称的 b-roll 窗口与真窗口偏差超此值 → 吸附纠正


# ─────────────────────────── derived:机械回填 ───────────────────────────

def _captions_from_words(words: list) -> list:
    """words(词级时间戳)→ 逐句字幕。聚句规则:停顿断句 + 最长词数。"""
    out, cur = [], []
    for w in words or []:
        try:
            word, start, end = str(w["word"]), float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if cur and (start - cur[-1]["end"] > CAPTION_GAP_S or len(cur) >= CAPTION_MAX_WORDS):
            out.append(cur)
            cur = []
        cur.append({"word": word, "start": start, "end": end})
    if cur:
        out.append(cur)
    return [{"t0": round(c[0]["start"], 2), "t1": round(c[-1]["end"], 2),
             "text": " ".join(x["word"] for x in c)} for c in out]


def _brolls_from_props(broll: list, fps: int) -> list:
    """broll prop(帧窗)→ 秒级窗口。这是**事实源**,防漂移以它为准。"""
    out = []
    for b in broll or []:
        try:
            t0, t1 = float(b["startFrame"]) / fps, float(b["endFrame"]) / fps
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if t1 > t0 >= 0:
            out.append({"t0": round(t0, 2), "t1": round(t1, 2),
                        "label": str(b.get("label", "")),
                        "placement": str(b.get("placement", "嵌入卡片 + 讲话人右下 PIP"))})
    return sorted(out, key=lambda x: x["t0"])


def _skeleton_beats(brolls: list, duration_s: float) -> list:
    """时间轴骨架:开场 → 各 b-roll 窗口 → 收尾。全 derived,可验真。"""
    beats, cursor = [], 0.0
    for b in brolls:
        if b["t0"] - cursor > 0.5:
            beats.append({"t0": round(cursor, 2), "t1": b["t0"], "section": "讲话人主画面",
                          "layout": "讲话人铺满卡片", "elements": ["字幕"],
                          "rationale": "", "source": "derived"})
        beats.append({"t0": b["t0"], "t1": b["t1"], "section": f"b-roll: {b['label']}" if b["label"] else "b-roll",
                      "layout": b["placement"], "elements": ["b-roll", "PIP", "字幕"],
                      "rationale": "", "source": "derived"})
        cursor = b["t1"]
    if duration_s - cursor > 0.5:
        beats.append({"t0": round(cursor, 2), "t1": round(duration_s, 2), "section": "收尾",
                      "layout": "讲话人铺满卡片", "elements": ["字幕"],
                      "rationale": "", "source": "derived"})
    return beats


# ─────────────────────────── model:叙述合并 + 防漂移 ───────────────────────────

def _merge_narrative(sb: dict, narrative: dict, duration_s: float,
                     brolls: list) -> None:
    """把模型叙述并进 storyboard,凡与事实冲突以事实为准。就地改 sb。"""
    corrections: list[str] = sb.setdefault("corrections", [])
    if not isinstance(narrative, dict):
        corrections.append("模型叙述不是对象,整体忽略")
        return

    if isinstance(narrative.get("summary"), str) and narrative["summary"].strip():
        sb["summary"] = narrative["summary"].strip()
    if isinstance(narrative.get("style"), dict):
        sb["style"] = narrative["style"]

    raw_beats = narrative.get("beats")
    if not isinstance(raw_beats, list):
        if raw_beats is not None:
            corrections.append("模型 beats 不是数组,忽略")
        return

    merged: list[dict] = []
    for i, rb in enumerate(raw_beats):
        if not isinstance(rb, dict):
            corrections.append(f"beat[{i}] 不是对象,丢弃")
            continue
        try:
            t0, t1 = float(rb["t0"]), float(rb["t1"])
        except (KeyError, TypeError, ValueError):
            corrections.append(f"beat[{i}] 缺 t0/t1,丢弃")
            continue
        # 截到片长
        c0, c1 = max(0.0, t0), min(float(duration_s), t1)
        if (c0, c1) != (t0, t1):
            corrections.append(f"beat[{i}] 时间 [{t0},{t1}] 超片长,截为 [{c0},{c1}]")
        if c1 - c0 <= 0.05:
            corrections.append(f"beat[{i}] 截后为空区间,丢弃")
            continue
        # b-roll 吸附:声称是 b-roll 的 beat,窗口必须等于事实窗口
        section = str(rb.get("section", ""))
        if "b-roll" in section.lower() and brolls:
            nearest = min(brolls, key=lambda b: abs(b["t0"] - c0) + abs(b["t1"] - c1))
            drift = abs(nearest["t0"] - c0) + abs(nearest["t1"] - c1)
            if drift > SNAP_TOL_S:
                corrections.append(
                    f"beat[{i}] 声称 b-roll 窗口 [{c0:.1f},{c1:.1f}] 与实际 "
                    f"[{nearest['t0']},{nearest['t1']}] 不符,已吸附")
                c0, c1 = nearest["t0"], nearest["t1"]
        merged.append({
            "t0": round(c0, 2), "t1": round(c1, 2),
            "section": section or "段落",
            "layout": str(rb.get("layout", "")),
            "elements": [str(x) for x in rb.get("elements", []) if isinstance(x, (str, int, float))],
            "rationale": str(rb.get("rationale", "")),
            "source": "model",
        })
    if merged:
        sb["beats"] = sorted(merged, key=lambda b: b["t0"])


# ─────────────────────────── 入口 ───────────────────────────

def emit_storyboard(words: list, broll: list, duration_s: float, fps: int = 30,
                    narrative_fn: Callable[..., dict] | None = None,
                    tsx: str | None = None) -> dict:
    brolls = _brolls_from_props(broll, fps)
    sb: dict = {
        "summary": "",
        "style": {},
        "beats": _skeleton_beats(brolls, duration_s),
        "captions": _captions_from_words(words),
        "brolls": brolls,
        "duration_s": round(float(duration_s), 2),
        "corrections": [],
    }
    if narrative_fn is not None:
        try:
            narrative = narrative_fn(tsx=tsx, storyboard_skeleton=sb)
        except Exception as e:  # noqa: BLE001 —— 叙述失败不拖垮分镜,降级为纯 derived
            sb["corrections"].append(f"叙述生成失败({type(e).__name__}),降级为纯回填分镜")
            return sb
        _merge_narrative(sb, narrative, duration_s, brolls)
    return sb


def _fmt_t(t: float) -> str:
    return f"{int(t) // 60}:{t % 60:04.1f}" if t >= 60 else f"{t:.1f}s"


def render_text(sb: dict) -> str:
    """Storyboard → 给用户看的时间轴文案(markdown)。"""
    lines: list[str] = ["# 分镜设计说明", ""]
    if sb.get("summary"):
        lines += ["## 概述", "", sb["summary"], ""]
    if sb.get("style"):
        style_desc = ";".join(f"{k}: {v}" for k, v in sb["style"].items())
        lines += ["## 风格", "", style_desc, ""]
    lines += ["## 时间轴", "", "| 时间 | 段落 | 画面布局 | 屏上元素 | 设计意图 | 来源 |",
              "|---|---|---|---|---|---|"]
    for b in sb.get("beats", []):
        tag = "[精确]" if b.get("source") == "derived" else "[模型设计]"
        lines.append(f"| {_fmt_t(b['t0'])}–{_fmt_t(b['t1'])} | {b.get('section','')} "
                     f"| {b.get('layout','')} | {'、'.join(b.get('elements', []))} "
                     f"| {b.get('rationale','')} | {tag} |")
    if sb.get("brolls"):
        lines += ["", "## b-roll `[精确]`", ""]
        for b in sb["brolls"]:
            lines.append(f"- {_fmt_t(b['t0'])}–{_fmt_t(b['t1'])} · {b['label'] or '(未命名)'} · {b['placement']}")
    if sb.get("captions"):
        lines += ["", "## 逐句字幕(来自转写,精确到词)`[精确]`", ""]
        for c in sb["captions"]:
            lines.append(f"- {_fmt_t(c['t0'])}–{_fmt_t(c['t1'])} {c['text']}")
    lines += ["", "---", "看到哪段不满意,直接指出**时间 + 想怎么改**"
              "(例:\"12.6s 的 b-roll 放大、PIP 挪左下\"),系统只改那处、其余不动。"]
    return "\n".join(lines)
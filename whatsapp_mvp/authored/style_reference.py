#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模块1 · 维度解析器(AspectParser)+ StyleSpec 契约(参考风格模仿,设计文档 §2/§3.1)。

纯逻辑、无模型调用、确定。两个职责:
  1) StyleSpec 契约:规范维度键、空壳构造、按选中维度过滤。
  2) parse_aspects(instruction) -> 选中维度集合(默认 节奏+转场;"完全照"类 → 全部)。
     detect_reference_intent(text) -> 用户是否想"参照某素材风格"(供模块4 判是否找参考)。

维度(与设计文档 §2 一致):
  pacing 节奏 · transitions 转场 · animation 动效 · camera 运镜 ·
  color 配色 · typography 字幕/文字 · graphics 卡片/版式
"""

from __future__ import annotations

# 规范维度键(顺序稳定,渲染/过滤都按它)
ASPECTS = ("pacing", "transitions", "animation", "camera", "color", "typography", "graphics")
# 用户没指定任何风格维度时的默认(设计决策 2A:剪辑风格 = 节奏 + 转场)
DEFAULT_ASPECTS = ("pacing", "transitions")

# 维度关键词(中英;命中即选该维度)。刻意保守,避免和普通剪辑指令乱撞。
_ASPECT_KEYWORDS = {
    "transitions": ["转场", "过渡", "转场方式", "transition"],
    "pacing":      ["节奏", "剪辑节奏", "卡点", "快慢", "剪切", "pacing", "rhythm", "pace", "节拍"],
    "animation":   ["动画", "动效", "特效", "animation", "motion effect", "motion graphic"],
    "camera":      ["运镜", "镜头", "推拉", "摇移", "运镜方式", "camera move", "camera"],
    "color":       ["配色", "色调", "调色", "颜色", "色彩", "color", "colour", "grade", "tone"],
    # 字幕/文字 太易和"加字幕"这类剪辑动作撞 —— 只认明确的"样式/字体/风格"表述
    "typography":  ["字幕样式", "字幕风格", "文字样式", "文案样式", "字体", "标题样式",
                    "caption style", "subtitle style", "typography"],
    "graphics":    ["卡片", "版式", "排版", "布局", "图形", "layout", "card", "graphic"],
}

# "全部维度"触发词(强表达"整体照搬")。保守、具体,避免把"一样大"这种误判成全部。
_ALL_TRIGGERS = [
    "完全照", "完全按", "完全一样", "一模一样", "照搬", "整体风格", "整个风格",
    "完整还原", "所有维度", "全部维度", "都要学", "都学", "同款风格", "整体照",
    "完全模仿", "全套", "整体都",
]

# "想参照某素材风格"的意图词(供模块4 判断是否去找参考素材;宁可略宽,模块4 再按素材数量兜底)
_REF_INTENT = [
    "参照", "参考", "照这个", "照它", "照他", "仿照", "模仿", "同款", "这个风格",
    "它的风格", "这个的风格", "类似风格", "同样风格", "学这个", "学它", "reference", "#ref", "照着",
]


def _norm(text) -> str:
    return str(text or "").strip().lower()


def parse_aspects(instruction) -> list:
    """从用户指令解析要模仿的维度集合。
    - 命中"完全照/整体风格/同款风格"类 → 全部维度。
    - 命中具体维度关键词 → 那些维度。
    - 都没命中(含空串)→ 默认 {pacing, transitions}(设计决策 2A)。
    返回按 ASPECTS 顺序排列的 list(稳定、可测)。"""
    t = _norm(instruction)
    if any(w in t for w in _ALL_TRIGGERS):
        return list(ASPECTS)
    hit = {a for a, words in _ASPECT_KEYWORDS.items() if any(w in t for w in words)}
    if not hit:
        return list(DEFAULT_ASPECTS)
    return [a for a in ASPECTS if a in hit]   # 按规范顺序


def detect_reference_intent(text) -> bool:
    """用户文字里是否表达了"参照某素材风格"的意图。供模块4 决定是否去找参考素材
    (再结合素材数量兜底:有意图但只有 1 条素材,自然没有参考,不问)。"""
    t = _norm(text)
    return any(w in t for w in _REF_INTENT)


# ─────────────────────────── StyleSpec 契约 ───────────────────────────

# StyleSpec 里除维度外的固定元字段
_META_KEYS = ("analysis_mode", "aspects", "overall", "source_frames", "corrections")


def empty_style_spec() -> dict:
    """空壳 StyleSpec(分析失败/无参考时用;Arm B 见到它等于'不参照',照常出片)。"""
    return {
        "analysis_mode": None,   # "video" | "frames" | "image" | None
        "aspects": [],           # 本次实际模仿的维度
        "overall": "",           # 一句总述
        "source_frames": [],     # 代表帧路径
        "corrections": [],       # 解析容错记录
    }


def is_empty_style_spec(spec: dict) -> bool:
    """没有任何可用风格信息(没总述、也没有任一维度)→ 视为空,Arm B 不参照。"""
    if not isinstance(spec, dict):
        return True
    if str(spec.get("overall", "")).strip():
        return False
    return not any(spec.get(a) for a in ASPECTS)


_ASPECT_LABEL = {
    "pacing": "节奏(pacing)", "transitions": "转场(transitions)", "animation": "动效(animation)",
    "camera": "运镜(camera)", "color": "配色(color)", "typography": "字幕/文字样式(typography)",
    "graphics": "卡片/版式(graphics)",
}


def render_style_reference_block(spec: dict) -> str:
    """把 StyleSpec 渲成 author prompt 的"参考风格"段(模块3 用)。只渲 `aspects` 里、
    且有内容的维度;空/无参考 → 返回 ''(调用方不加此段)。冲突以参考为准、不抄内容。"""
    import json as _json
    if is_empty_style_spec(spec):
        return ""
    aspects = [a for a in (spec.get("aspects") or ASPECTS) if a in ASPECTS]
    lines = []
    overall = str(spec.get("overall", "")).strip()
    if overall:
        lines.append(f"  - 整体: {overall}")
    for a in ASPECTS:
        if a in aspects and spec.get(a):
            lines.append(f"  - {_ASPECT_LABEL[a]}: {_json.dumps(spec[a], ensure_ascii=False)}")
    if not lines:
        return ""
    return (
        "STYLE REFERENCE — the user gave a reference clip; imitate ONLY these style aspects:\n"
        + "\n".join(lines) + "\n"
        "Rules: match these aspects closely; when they conflict with your default house look, "
        "PREFER THE REFERENCE. Do NOT copy the reference's spoken/on-screen words or its footage "
        "— borrow style only.\n"
    )


def filter_style_spec(spec: dict, aspects) -> dict:
    """按选中维度裁剪 StyleSpec:只保留 aspects 里的维度 + 固定元字段。
    模块3 用它,确保'只应用用户选中的维度',其余即使分析出来也不强加。"""
    if not isinstance(spec, dict):
        return empty_style_spec()
    keep = {a for a in (aspects or []) if a in ASPECTS}
    out = {k: spec.get(k) for k in _META_KEYS if k in spec}
    out["aspects"] = [a for a in ASPECTS if a in keep]     # 规范顺序 + 去非法
    for a in ASPECTS:
        if a in keep and spec.get(a):
            out[a] = spec[a]
    return out
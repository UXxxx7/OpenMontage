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
# 表达了"参照风格"但没点名维度时的默认:除节奏/转场外,补上决定"外观像不像"的
# 配色/字幕样式/卡片版式(设计决策 2A 的放宽——用户说"模仿风格"通常指整套外观)。
_LOOK_DEFAULT = ("pacing", "transitions", "color", "typography", "graphics")

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
    - 命中具体维度关键词 → 只要那些维度。
    - 有"参照某风格"意图但没点名维度 → "外观"默认集合(节奏+转场+配色+字幕+版式)。
    - 完全无风格语境(含空串)→ 最小默认 {pacing, transitions}。
    返回按 ASPECTS 顺序排列的 list(稳定、可测)。"""
    t = _norm(instruction)
    if any(w in t for w in _ALL_TRIGGERS):
        return list(ASPECTS)
    hit = {a for a, words in _ASPECT_KEYWORDS.items() if any(w in t for w in words)}
    if hit:
        return [a for a in ASPECTS if a in hit]   # 点名了具体维度 → 只要那些
    # 没点名维度:表达了"参照某风格"意图 → 用更宽的"外观"默认(配色/字幕/版式
    # 才是"像不像"的大头;之前只给节奏+转场、配色没进 → 成片不像);否则退回最小默认。
    if any(w in t for w in _REF_INTENT):
        return [a for a in ASPECTS if a in _LOOK_DEFAULT]
    return list(DEFAULT_ASPECTS)


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


def render_style_reference_block(spec: dict, has_frames: bool = False) -> str:
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
    # 仅当现写 prompt 里真的附了参考帧(has_frames)才说"看上方帧",否则不误导模型。
    frames_line = ("Reference frames are provided above — imitate the look you SEE in them. "
                   if has_frames else "")
    return (
        "STYLE REFERENCE — the user gave a reference clip; imitate these style aspects:\n"
        + "\n".join(lines) + "\n"
        "Rules: match these aspects closely; when they conflict with your default house look, "
        "PREFER THE REFERENCE — do NOT impose your own house style. In particular, match the "
        "reference's OVERALL COLOR PALETTE, brightness and mood as a whole: if the reference is "
        "light / warm / minimal, do NOT output a dark or neon theme (and vice-versa); the "
        "background tone, card colors and typography must read as the SAME family as the reference. "
        + frames_line +
        "Do NOT copy the reference's spoken/on-screen words or its footage — borrow style only.\n"
    )


def render_focus_reference_block(focus_spec: dict, window=None, has_frames: bool = False) -> str:
    """把"聚焦(时间窗)"StyleSpec 渲成 author prompt 的一段:要求模型在整体风格之外,
    【额外复刻】用户单独点名的那一小段效果。空/无 → ''(调用方不加此段)。
    这是 ADDITIVE 的——明确要求"别让这一个效果盖过整体风格"。"""
    import json as _json
    if is_empty_style_spec(focus_spec):
        return ""
    aspects = [a for a in (focus_spec.get("aspects") or ASPECTS) if a in ASPECTS]
    lines = []
    overall = str(focus_spec.get("overall", "")).strip()
    if overall:
        lines.append(f"  - 这段效果整体: {overall}")
    for a in ASPECTS:
        if a in aspects and focus_spec.get(a):
            lines.append(f"  - {_ASPECT_LABEL[a]}: {_json.dumps(focus_spec[a], ensure_ascii=False)}")
    if not lines:
        return ""
    win = ""
    if window:
        try:
            win = f" (reference t={float(window[0]):.0f}-{float(window[1]):.0f}s)"
        except (TypeError, ValueError, IndexError):
            win = ""
    frames_line = ("The FOCUS frames shown above capture this exact effect — reproduce the "
                   "element you SEE in them. " if has_frames else "")
    return (
        f"ADDITIONAL FOCUS — beyond the overall style, the user singled out ONE specific "
        f"effect{win} they want you to REPRODUCE in your composition:\n"
        + "\n".join(lines) + "\n"
        + frames_line +
        "Recreate THIS specific graphic/effect as its own beat at a fitting moment — adapt its "
        "content to THIS video, borrowing the visual treatment (not the reference's words or "
        "footage). This is ADDITIVE: keep imitating the OVERALL style as well; do NOT let this "
        "single effect override or replace the global look.\n"
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


# ─────────────────────── 时间窗解析 + 整体/聚焦编排(块①) ───────────────────────
# 用户可能只想参考素材的"某一段"(如"26-29s左边的图形特效")。这里把这层意图解析成
# (start,end) 秒窗,并把指令编排成"整体 + 附加聚焦":整体照旧模仿全片风格,聚焦再额外
# 复刻用户点名的那一小段效果。全部纯逻辑——不触模型、不读盘;真正的裁剪/抽帧在模块2。

# 时间窗通常指"某个可见效果",没点名维度时给这些(不走 parse_aspects 的宽泛外观兜底)。
_FOCUS_DEFAULT = ("animation", "graphics", "transitions")
# rest 子句里表示"也要整体风格"的提示词(决定有窗时是否再叠加一次全局分析)。
_GLOBAL_HINT = ("风格", "整体", "整个", "全片", "全程", "通篇", "整段", "overall", "style")
# 无独立 rest 子句(窗与整体意图挤在同一句)时的兜底标记:只认"整段"类强词——
# 刻意不含裸"风格"(否则"只模仿26-29s的字幕风格"会被误判成也要整体)。
_STRONG_GLOBAL = ("整体", "整个", "全片", "全程", "通篇", "整段", "whole", "entire")


def _mk_window(a, b):
    """规整 (start,end):数值化 → start≥0 → 保证 end>start(反了则交换,相等/退化补极短窗)。
    任一非数 → None。不夹上界(不知道真实时长,交模块2)。"""
    try:
        a = float(a)
        b = float(b)
    except (TypeError, ValueError):
        return None
    if b < a:
        a, b = b, a
    a = max(0.0, a)
    if b <= a:
        b = a + 0.5          # 退化窗:给个极短区间,模块2 再按 dur 夹
    return (round(a, 3), round(b, 3))


def parse_reference_window(text):
    """从用户文字解析"想参考的时间段"(秒)。命中 → (start_s, end_s)(start≥0、end>start);
    没有明确时间表达 → None。支持:
      · mm:ss 范围  "0:26-0:29" / "1:05到1:12"
      · 秒范围      "26-29s" / "26到29秒" / "26~29秒"(第二个数必须带单位,防"2-3个"误判)
      · 单点        "第27秒" / "在27秒" / "27秒处|左右|附近" → 展成 ±STYLEREF_POINT_HALF 的小窗
    只解析,不夹真实时长(模块2 知道 dur 再裁)。"""
    import re as _re, os as _os
    t = str(text or "")
    if not t.strip():
        return None
    # ① mm:ss 范围(最具体,先试)。分隔符含 ASCII/全角连字符、em/en-dash、全角波浪、到/至。
    m = _re.search(r'(\d{1,2}):(\d{1,2})\s*[-~－—–～到至]\s*(\d{1,2}):(\d{1,2})', t)
    if m:
        try:
            a = int(m.group(1)) * 60 + int(m.group(2))
            b = int(m.group(3)) * 60 + int(m.group(4))
        except (TypeError, ValueError):
            return None
        return _mk_window(a, b)
    # ② 秒范围(第二个数字【必须】带单位,避免把"2-3个""1-2版"当时间)
    m = _re.search(
        r'(\d+(?:\.\d+)?)\s*(?:s|秒|sec|secs|seconds?)?\s*[-~－—–～到至]\s*'
        r'(\d+(?:\.\d+)?)\s*(?:s|秒|sec|secs|seconds?)', t)
    if m:
        return _mk_window(m.group(1), m.group(2))
    # ③ 单点(需明确锚点:第X秒 / 在X秒 / X秒处|左右|附近)→ 展成小窗
    m = _re.search(
        r'(?:第|在)\s*(\d+(?:\.\d+)?)\s*(?:s|秒)'
        r'|(\d+(?:\.\d+)?)\s*(?:s|秒)\s*(?:处|左右|附近|那|的地方)', t)
    if m:
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        try:
            p = float(raw)
        except (TypeError, ValueError):
            return None
        half = 1.5
        try:
            hv = float(_os.getenv("STYLEREF_POINT_HALF", "1.5"))
            if hv > 0:
                half = hv
        except (TypeError, ValueError):
            pass
        return _mk_window(p - half, p + half)
    return None


def split_by_window(text):
    """把指令拆成 (focus_clause, rest_clause, window)。
      · 按句/子句分隔符(。！？!?;；,，、及换行)切分;
      · 含时间表达的子句 → focus_clause(可多句,用逗号回接);其余 → rest_clause;
      · window = 从 focus 子句解析出的时间窗(取第一个)。
    没有任何时间表达 → ("", 原文, None)(调用方据此走"纯整体"旧路径)。"""
    import re as _re
    t = str(text or "")
    if not t.strip():
        return ("", "", None)
    parts = [p.strip() for p in _re.split(r'[。！？!?;；,，、\n\r]+', t) if p.strip()]
    if not parts:
        return ("", t.strip(), None)
    focus, rest, win = [], [], None
    for p in parts:
        w = parse_reference_window(p)
        if w is not None:
            focus.append(p)
            if win is None:
                win = w
        else:
            rest.append(p)
    if win is None:
        return ("", t.strip(), None)
    return ("，".join(focus), "，".join(rest), win)


def _explicit_aspects(text):
    """只返回被关键词【明确点名】的维度(不含任何默认/意图兜底)。没点名 → []。
    聚焦分析用它:片段只复刻用户真正指出的那类效果,不硬塞节奏/配色。
    注意:【点名的具体维度优先于"一模一样"类全触发】——"26-29s的转场要一模一样"指的是
    那个转场,不是全维度;只有没点名任何维度、纯"完全照搬这一段"才当作全维度。"""
    t = _norm(text)
    if not t:
        return []
    hit = {a for a, words in _ASPECT_KEYWORDS.items() if any(w in t for w in words)}
    if hit:
        return [a for a in ASPECTS if a in hit]     # 点名了具体维度 → 只要那些
    if any(w in t for w in _ALL_TRIGGERS):
        return list(ASPECTS)                        # 没点名 + "完全照/一模一样" → 全维度
    return []


def _wants_global(text) -> bool:
    """rest 子句是否表达了"也要模仿整体风格"(有独立 rest 子句时用)。契约字面:命中
    "完全照/整体风格"类强触发,或任一"风格/整体/…"提示词 → True。
    (放宽:不再要求同时有参照意图——rest 里只要提"风格/整体"就当成也要整体,
     否则"…画面风格要统一"这类明确诉求会被漏掉。)"""
    t = _norm(text)
    if not t:
        return False
    if any(w in t for w in _ALL_TRIGGERS):
        return True
    if any(h in t for h in _GLOBAL_HINT):
        return True
    return False


def _wants_global_strong(text) -> bool:
    """无独立 rest 子句(窗与整体意图同句)时的兜底:只认"完全照搬 / 整体/整个/全片…"
    这类强"整段"标记,不认裸"风格"——避免"只模仿26-29s的字幕风格"被误当成也要整体。"""
    t = _norm(text)
    if not t:
        return False
    if any(w in t for w in _ALL_TRIGGERS):
        return True
    if any(w in t for w in _STRONG_GLOBAL):
        return True
    return False


def plan_style_analysis(text) -> dict:
    """把用户指令编排成"整体 + 附加聚焦"的分析计划(模块4/块④ 消费)。
    返回 dict:
      window          (start,end)|None —— 聚焦时间窗
      global_on       bool            —— 是否跑全局(整体风格)分析
      global_aspects  list            —— 全局分析要模仿的维度
      focus_on        bool            —— 是否跑聚焦(时间窗)分析
      focus_aspects   list            —— 聚焦分析要复刻的维度
      focus_clause    str             —— 聚焦子句(含时间表达)
      rest_clause     str             —— 其余子句(整体语境)
    规则:
      · 无窗 → 只全局(等价旧行为):global_on=True,维度=parse_aspects(全文)。
      · 有窗 → 必做聚焦;仅当 rest 子句表达"也要整体风格"时再叠加全局(整体+附加聚焦)。
    纯逻辑,不触模型/磁盘。"""
    t = str(text or "")
    focus_clause, rest_clause, window = split_by_window(t)
    if window is None:
        return {
            "window": None,
            "global_on": True,
            "global_aspects": parse_aspects(t),
            "focus_on": False,
            "focus_aspects": [],
            "focus_clause": "",
            "rest_clause": t.strip(),
        }
    f_asp = _explicit_aspects(focus_clause) or [a for a in ASPECTS if a in _FOCUS_DEFAULT]
    # 全局信号:有独立 rest 子句 → 按契约字面判(风格/整体/完全照);没有独立 rest
    # (窗与整体意图挤同一句)→ 退回聚焦子句但只认"整段"强标记,避免"字幕风格"误触。
    if rest_clause.strip():
        g_on = _wants_global(rest_clause)
        g_src = rest_clause
    else:
        g_on = _wants_global_strong(focus_clause)
        g_src = focus_clause
    return {
        "window": window,
        "global_on": g_on,
        "global_aspects": parse_aspects(g_src) if g_on else [],
        "focus_on": True,
        "focus_aspects": f_asp,
        "focus_clause": focus_clause,
        "rest_clause": rest_clause,
    }

# 确定性的 props 层面几何×时间重叠检查——不依赖渲染、不依赖 LLM，纯读取最终
# 组装好的 render props 字典，计算 SpeakerCard 随时间变化的矩形/可见度，跟每个
# 带位置的图形元素做区间+矩形相交检测。
#
# 对应 Fix C4：三个真实生产 bug（job_e44166eb8c38）都是"渲染出来才发现"的
# ——zoneHeader 画在还没收起的大卡片上面、倒计时画在还没收起的大卡片上面、
# 说话人被隐藏了近一半时长且再也没有回来。这三个问题其实全都能从最终 props
# 的数字直接算出来，不需要真的跑一次渲染再去看 stills——这个模块就是把这类
# 检查提到渲染之前。

from __future__ import annotations

from typing import Any, Optional

FPS = 30

# Fix D 的强制上限，这里同时作为事后复核的兜底（万一 Fix D 的裁剪逻辑本身有
# 边界情况没覆盖到，这里再查一次）。
_MAX_CONTINUOUS_HIDDEN_FRAMES = 8 * FPS
_MAX_HIDDEN_FRACTION = 0.30
_DEAD_SPACE_FRAMES = 60  # 接管区间里超过这么多帧没有任何图形元素 = 死空间

# Fix C6（2026-07-16）：每这么多秒视频至少要有 1 项真正的图形/动画内容——
# 用户明确要求过"加一条标准检查动画到不到位，不要只返回说话人配字幕"。
# 12s/项是从真实案例反推的宽松下限：MrBeast backtest 出问题的那一版是
# 23.6s 视频只有 1 项内容（1 < ceil(23.6/12)=2，会被这条规则抓到）；dajaai
# 的 32.5s 视频只有 2 项（同样会被抓到，独立验证过那条视频确实存在"3s-28s
# 只有说话人和字幕"的缺口，见 content_planner 自己的 plan-quality 标准 2）。
# 数值来源：whatsapp_mvp/props_lint.py 同一批组件的 _EST_HEIGHT/_rect_height
# 已有的"什么算一项内容"口径，跟 pipeline_runner._visual_richness 用同一套
# 字段，两边保持一致。
_RICHNESS_FIELDS = (
    "dataCards", "gauges", "countdowns", "calendarEvents", "beforeAfter",
    "stepLists", "topicCards", "cornerCards", "quotes",
)
_SECONDS_PER_RICHNESS_UNIT = 12

# 每种可视元素的估计渲染高度（px @ 1080x1920）——跟 content_planner._EST_HEIGHT_BY_VISUAL
# 用途类似（那边是布局阶段用来决定堆叠够不够高度，这边是渲染前复核阶段用来
# 判断矩形是否相交），数值来源于各组件当前的实际尺寸。
_EST_HEIGHT = {
    "gauge": 330, "countdown": 300, "calendar": 560, "beforeAfter": 330,
    "pill": 100, "zoneHeader": 130, "topicCard": 180, "qrContact": 260,
}


def _rect_height(kind: str, entry: dict) -> int:
    if kind == "dataCard":
        return 100 + 112 * len(entry.get("rows") or [])
    if kind == "stepList":
        return 40 + 95 * len(entry.get("steps") or [])
    return _EST_HEIGHT.get(kind, 320)


def _scene_value_at(scenes: list[dict], frame: float, key: str) -> float:
    """跟 qa_stills._card_h_at 同一套线性关键帧插值，推广到 x/y/w/h 任意维度。
    不建模 SpeakerCard.tsx 自己的 TRANSITION_FRAMES 提前量——那是渲染层的
    细节，这里跟 qa_stills 保持同一个（保守的）近似：把整个关键帧间隔当成
    过渡区间，而不是只在最后 20 帧过渡。"""
    if not scenes:
        return 0.0
    prev = scenes[0]
    if frame <= prev["frame"]:
        return prev[key]
    for s in scenes[1:]:
        if frame <= s["frame"]:
            span = s["frame"] - prev["frame"]
            t = (frame - prev["frame"]) / span if span else 1.0
            return prev[key] + (s[key] - prev[key]) * t
        prev = s
    return prev[key]


def _card_rect_at(scenes: list[dict], frame: float) -> tuple[float, float, float, float]:
    return (
        _scene_value_at(scenes, frame, "x"),
        _scene_value_at(scenes, frame, "y"),
        _scene_value_at(scenes, frame, "w"),
        _scene_value_at(scenes, frame, "h"),
    )


def _opacity_at(keyframes: list[dict], frame: float) -> float:
    if not keyframes:
        return 1.0
    prev = keyframes[0]
    if frame <= prev["frame"]:
        return prev["opacity"]
    for k in keyframes[1:]:
        if frame <= k["frame"]:
            span = k["frame"] - prev["frame"]
            t = (frame - prev["frame"]) / span if span else 1.0
            return prev["opacity"] + (k["opacity"] - prev["opacity"]) * t
        prev = k
    return prev["opacity"]


def _rects_intersect(a: tuple, b: tuple) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _collect_elements(props: dict) -> list[dict]:
    """把 props 里所有"带时间窗"的元素统一成
    {"kind","label","mount","end",("x","y","w","h" 可选)} 方便统一处理。
    quote 等没有 x/y 的全画布类型只参与时间线覆盖率统计，不参与矩形检查。
    """
    out: list[dict] = []

    def add(kind: str, items: Optional[list[dict]]) -> None:
        for e in items or []:
            if not isinstance(e, dict):
                continue
            mount = e.get("mountFrame", e.get("fromFrame"))
            end = e.get("endFrame", e.get("toFrame"))
            if mount is None or end is None:
                continue
            item: dict[str, Any] = {
                "kind": kind,
                "label": e.get("title") or e.get("text") or e.get("headline") or kind,
                "mount": mount, "end": end,
            }
            if "x" in e and "y" in e:
                item["x"] = e["x"]
                item["y"] = e["y"]
                item["w"] = e.get("width", 960)
                item["h"] = _rect_height(kind, e)
            out.append(item)

    add("dataCard", props.get("dataCards"))
    add("gauge", props.get("gauges"))
    add("countdown", props.get("countdowns"))
    add("calendar", props.get("calendarEvents"))
    add("beforeAfter", props.get("beforeAfter"))
    add("pill", props.get("pills"))
    add("stepList", props.get("stepLists"))
    add("topicCard", props.get("topicCards"))
    add("zoneHeader", props.get("zoneHeaders"))
    add("quote", props.get("quotes"))
    qr = props.get("qrContact")
    if qr:
        add("qrContact", [qr])
    return out


def _transition_windows(scenes: list[dict]) -> list[tuple[int, int]]:
    """SpeakerCard 正在两个 scene 关键帧之间变形(w/h 改变)的帧区间——用跟
    _scene_value_at 一样保守的近似（整个关键帧间隔都算过渡区间）。宽高都
    没变的相邻关键帧（例如只是把同一个 mode 的 scene 拆成两段）不算过渡。
    """
    windows: list[tuple[int, int]] = []
    for i in range(len(scenes) - 1):
        a, b = scenes[i], scenes[i + 1]
        if a.get("w") != b.get("w") or a.get("h") != b.get("h"):
            windows.append((a["frame"], b["frame"]))
    return windows


def _hidden_spans(opacity_kf: list[dict], duration_frames: int, step: int = 1) -> list[tuple[int, int]]:
    """从 opacityKeyframes 提取"卡片不可见"(插值 opacity < 0.5)的连续区间。
    逐帧采样（默认 step=1）而不是只看关键帧端点——保证不错过精确的过 0.5
    交叉点，视频时长通常在几千帧以内，性能上完全可以接受。"""
    if not opacity_kf or duration_frames <= 0:
        return []
    spans: list[tuple[int, int]] = []
    span_start: Optional[int] = None
    f = 0
    while f < duration_frames:
        hidden = _opacity_at(opacity_kf, f) < 0.5
        if hidden and span_start is None:
            span_start = f
        elif not hidden and span_start is not None:
            spans.append((span_start, f))
            span_start = None
        f += step
    if span_start is not None:
        spans.append((span_start, duration_frames))
    return spans


def lint_props(props: dict) -> list[dict]:
    """跑全部检查，返回 findings 列表（结构化 dict，至少带 "check" 字段，
    跟 qa_stills 的 finding 形状保持一致）。空列表 = 干净。永不 raise——
    props 里缺字段/形状不对时该项检查就跳过，不影响其它检查。"""
    findings: list[dict] = []
    scenes = props.get("scenes") or []
    opacity_kf = props.get("opacityKeyframes") or []
    duration_frames = round((props.get("durationSeconds") or 0) * FPS)
    elements = _collect_elements(props)
    rect_elements = [e for e in elements if "x" in e]

    # 1) 元素两两之间：区间重叠 且 矩形也重叠。正常的堆叠系统本来就该保证
    # 不同 y 车道不会撞在一起，这里是双重保险（例如显式传入的 op 数据绕过了
    # 堆叠系统）。
    for i in range(len(rect_elements)):
        for j in range(i + 1, len(rect_elements)):
            a, b = rect_elements[i], rect_elements[j]
            if a["mount"] < b["end"] and b["mount"] < a["end"]:
                if _rects_intersect((a["x"], a["y"], a["w"], a["h"]), (b["x"], b["y"], b["w"], b["h"])):
                    findings.append({
                        "check": "element_overlap",
                        "detail": f"{a['kind']}({a['label']!r}) 跟 {b['kind']}({b['label']!r}) 时间和矩形都重叠",
                        "a": {"kind": a["kind"], "label": a["label"], "mount": a["mount"], "end": a["end"]},
                        "b": {"kind": b["kind"], "label": b["label"], "mount": b["mount"], "end": b["end"]},
                    })

    # 2) 元素 vs SpeakerCard：元素可见的同时卡片也可见（opacity>=0.05）且
    # 矩形相交——抓 header-over-card / countdown-under-card 这类真实 bug。
    # 在元素自己的存活区间内多点采样（不只是首尾两帧），因为卡片可能在
    # 元素存活期间的任何一刻还没收缩完。
    if scenes:
        for e in rect_elements:
            span = max(1, e["end"] - e["mount"])
            step = max(1, span // 30)
            f = e["mount"]
            while f < e["end"]:
                if f >= 0:
                    if _opacity_at(opacity_kf, f) >= 0.05:
                        card_rect = _card_rect_at(scenes, f)
                        if _rects_intersect(card_rect, (e["x"], e["y"], e["w"], e["h"])):
                            findings.append({
                                "check": "element_over_card",
                                "frame": f,
                                "detail": f"{e['kind']}({e['label']!r}) 在第 {f} 帧跟仍可见的 SpeakerCard 矩形重叠",
                                "card_rect": card_rect,
                                "element_rect": (e["x"], e["y"], e["w"], e["h"]),
                            })
                            break  # 每个元素只报一次
                f += step

    # 2b) Fix C7（2026-07-16，补 CLAUDE-v2.md §9 "Transitions"标准里几何检查
    # 覆盖不到的部分）：元素跟卡片矩形不相交，不代表两个动画没有"抢镜"——
    # SpeakerCard 正在两个 scene 关键帧之间变形(尺寸变化)的这段时间，如果
    # 同时有新元素挂载，两个动画会同时发生，即使它们的矩形完全没有重叠。
    # CLAUDE-v2.md 的原话："when animation/element B is about to appear...
    # animation/element A occupying that space must have already finished
    # disappearing, not be fading out concurrently"——这里把它变成一条可
    # 机械判定的规则：卡片转场期间不应该有新元素挂载。
    for win_start, win_end in _transition_windows(scenes):
        for e in elements:
            if win_start < e["mount"] < win_end:
                findings.append({
                    "check": "element_mounts_during_card_transition",
                    "detail": f"{e['kind']}({e['label']!r}) 在第 {e['mount']} 帧挂载，此时 SpeakerCard "
                              f"正在转场({win_start}-{win_end})，两个动画同时发生，会抢镜",
                    "transition_window": [win_start, win_end],
                })

    # 3) facecam 隐藏时长：最长连续隐藏区间 + 总隐藏占比
    hidden_spans = _hidden_spans(opacity_kf, duration_frames)
    total_hidden = sum(e - s for s, e in hidden_spans)
    longest_hidden = max((e - s for s, e in hidden_spans), default=0)
    if duration_frames > 0:
        frac = total_hidden / duration_frames
        if frac > _MAX_HIDDEN_FRACTION:
            findings.append({
                "check": "facecam_hidden_budget_exceeded",
                "detail": f"说话人被隐藏的总时长占比 {frac:.1%}，超过 {_MAX_HIDDEN_FRACTION:.0%} 的上限",
                "total_hidden_frames": total_hidden, "duration_frames": duration_frames,
            })
        if longest_hidden > _MAX_CONTINUOUS_HIDDEN_FRAMES:
            findings.append({
                "check": "facecam_hidden_too_long",
                "detail": f"单次连续隐藏 {longest_hidden} 帧({longest_hidden / FPS:.1f}s)，"
                          f"超过 {_MAX_CONTINUOUS_HIDDEN_FRAMES / FPS:.0f}s 上限",
                "longest_hidden_frames": longest_hidden,
            })

    # 4) facecam 是否在片尾之前恢复
    if hidden_spans and duration_frames > 0 and hidden_spans[-1][1] >= duration_frames - 1:
        findings.append({
            "check": "facecam_never_restored",
            "detail": f"说话人从第 {hidden_spans[-1][0]} 帧起被隐藏，直到片尾都没有恢复",
            "hidden_from_frame": hidden_spans[-1][0],
        })

    # 5) 接管期间的画布死空间：隐藏区间里超过 _DEAD_SPACE_FRAMES 帧没有任何
    # 图形元素在画面上。
    for start, end in hidden_spans:
        covering = sorted(
            (max(e["mount"], start), min(e["end"], end))
            for e in elements if e["mount"] < end and e["end"] > start
        )
        cursor = start
        for cs, ce in covering:
            if cs - cursor > _DEAD_SPACE_FRAMES:
                findings.append({
                    "check": "takeover_dead_space",
                    "detail": f"隐藏区间 [{start},{end}] 里，第 {cursor}-{cs} 帧"
                              f"（{(cs - cursor) / FPS:.1f}s）没有任何图形元素",
                    "gap_start": cursor, "gap_end": cs,
                })
            cursor = max(cursor, ce)
        if end - cursor > _DEAD_SPACE_FRAMES:
            findings.append({
                "check": "takeover_dead_space",
                "detail": f"隐藏区间 [{start},{end}] 里，第 {cursor}-{end} 帧"
                          f"（{(end - cursor) / FPS:.1f}s）没有任何图形元素",
                "gap_start": cursor, "gap_end": end,
            })

    # 5b) Fix D6（2026-07-16）：全画布接管(sections)本身既没有 timeline
    # 也没有 icon 时，SectionLayer 的渲染逻辑只剩标题+一个纯装饰性光斑——
    # 真实 backtest 截图确认过的 bug：一个"流程"接管撤走了说话人，观众看到
    # 的是标题、一大片空白、然后隔了很远才有几张够不着视线的卡片。这条
    # 专门抓这个几何模式（不依赖渲染/看图就能从 props 直接判断），跟
    # takeover_dead_space 互补：dead_space 抓的是"隐藏区间里内容按时间对
    # 不上"，这条抓的是"接管自己选择的视觉表现形式（纯标题）注定填不满
    # 画布"，两者是不同的失败模式，只有 dead_space 而没有这条会漏掉这一类。
    for sec in (props.get("sections") or []):
        if not sec.get("timeline") and not sec.get("icon"):
            findings.append({
                "check": "section_takeover_lacks_content",
                "detail": f"接管 '{sec.get('title', '')}' ({sec.get('fromFrame')}-{sec.get('toFrame')}) "
                          f"既没有 timeline 也没有 icon——SectionLayer 只会渲染标题+一个装饰性光斑，"
                          f"说话人被撤走却填不满画布。要么补 process_timeline（多阶段流程最合适），"
                          f"要么给一个 icon，要么这段内容其实不值得全画布接管，改回普通 workflow 模式",
                "fromFrame": sec.get("fromFrame"), "toFrame": sec.get("toFrame"),
            })

    # 6) Fix C6：视觉丰富度下限——每 _SECONDS_PER_RICHNESS_UNIT 秒至少要有 1
    # 项真正的图形/动画内容，不能只有说话人+字幕。跟 dead_space/element_over_card
    # 等其它检查一样是诊断/安全网：这条 finding 的 detail 文本会被喂回
    # content_planner 重新规划（见 pipeline_runner._op_apply_style 的
    # props_lint 循环），直接提示 LLM 加内容，而不是含糊地说"再试一次"。
    if duration_frames > 0:
        richness = sum(len(props.get(f) or []) for f in _RICHNESS_FIELDS)
        richness += sum(1 for s in (props.get("sections") or []) if s.get("timeline"))
        duration_s = duration_frames / FPS
        min_required = max(1, round(duration_s / _SECONDS_PER_RICHNESS_UNIT))
        if richness < min_required:
            findings.append({
                "check": "low_visual_richness",
                "detail": f"全片 {duration_s:.0f}s 只规划了 {richness} 项图形/动画内容"
                          f"（数据卡/仪表盘/倒计时/日历/前后对比/步骤列表/话题卡/角标卡/金句/时间线），"
                          f"低于 {min_required} 项的下限——大部分画面只有说话人和字幕，"
                          f"内容不够丰富，请补充更多锚定在具体语句上的图形",
                "richness": richness, "min_required": min_required,
            })

    # Fix D4：outro 不能落在隐藏区间里
    outro = props.get("outro")
    if outro and "fromFrame" in outro:
        of = outro["fromFrame"]
        for s, e in hidden_spans:
            if s <= of < e:
                findings.append({
                    "check": "outro_during_hidden_facecam",
                    "detail": f"outro 从第 {of} 帧开始，此时说话人仍处于隐藏区间 [{s},{e}]",
                })
                break

    return findings

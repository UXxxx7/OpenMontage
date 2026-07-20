# pipeline_runner 里纯数据变换函数的单测（不依赖真实转写/渲染）——目前只有
# Fix C2 的挂载时间整体平移。其余 pipeline_runner 逻辑深度耦合转写/渲染子
# 进程，走真实 /jobs 端到端验证（见 CLAUDE-v2.md 的验证阶梯），不在这里补单测。
#
# Run: uv run python -m whatsapp_mvp.test_pipeline_runner

from __future__ import annotations

from whatsapp_mvp.pipeline_runner import (
    _floor_shift_graphics, _floor_shift_zone_headers, _QUOTE_MIN_START_FRAMES,
)

FAILED = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL") + f" [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def test_shifts_below_floor_preserving_duration():
    items = [
        {"mountFrame": 91, "endFrame": 296},   # duration 205 — below floor, must shift
        {"mountFrame": 500, "endFrame": 600},  # above floor already — untouched
    ]
    _floor_shift_graphics(items, floor=120)
    check("低于 floor 的图形被整体平移到 floor", items[0]["mountFrame"] == 120, items[0])
    check("平移保留原有停留时长（205 帧）", items[0]["endFrame"] - items[0]["mountFrame"] == 205, items[0])
    check("已经在 floor 之后的图形不受影响", items[1] == {"mountFrame": 500, "endFrame": 600}, items[1])


def test_before_after_second_reveal_frame_shifts_too():
    items = [{"mountFrame": 50, "endFrame": 200, "secondRevealFrame": 90}]
    _floor_shift_graphics(items, floor=120)
    delta = 120 - 50
    check("beforeAfter 的 secondRevealFrame 跟着 mountFrame 一起平移",
          items[0]["secondRevealFrame"] == 90 + delta, items[0])


def test_row_offsets_stay_relative_and_valid():
    """count_up 的 rows[].mountOffset 是相对卡片自己 mountFrame 的相对值——
    整体平移卡片后，行的绝对落点(mountFrame+mountOffset)应该跟着平移同样的量，
    行本身不需要被触碰。"""
    items = [{"mountFrame": 91, "endFrame": 300, "rows": [{"mountOffset": 0}, {"mountOffset": 152}]}]
    _floor_shift_graphics(items, floor=140)
    delta = 140 - 91
    card = items[0]
    check("卡片整体平移", card["mountFrame"] == 140)
    check("行的 mountOffset 保持不变（相对值天然正确）",
          card["rows"][0]["mountOffset"] == 0 and card["rows"][1]["mountOffset"] == 152, card["rows"])
    check("行的绝对落点也跟着平移了同样的量",
          (card["mountFrame"] + card["rows"][1]["mountOffset"]) == (91 + 152 + delta), card)


def test_zone_headers_shift_from_to_frame():
    headers = [{"fromFrame": 91, "toFrame": 300}, {"fromFrame": 500, "toFrame": 700}]
    _floor_shift_zone_headers(headers, floor=120)
    check("低于 floor 的 zoneHeader 被平移", headers[0]["fromFrame"] == 120 and headers[0]["toFrame"] == 329, headers[0])
    check("已在 floor 之后的 zoneHeader 不受影响", headers[1] == {"fromFrame": 500, "toFrame": 700}, headers[1])


def test_quote_min_start_floor_keeps_facecam_visible_at_video_open():
    """真实用户反馈：quote 是唯一 solo(占满整个画布，把说话人盖住)的图形类型，
    LLM 把开场问候语当成 quote 素材时，在片头刚结束(甚至没有片头时从第 0 帧起)
    就立刻盖住脸——说话人该露脸的最关键时刻反而被挡住。这条地板线必须比一般
    图形的 intro-clamp floor 更晚，且不依赖 intro 是否存在(调用方在 pipeline_
    runner._op_apply_style 里对 plan_quotes 无条件套用，不像其它图形那样只在
    `if intro:` 分支内才被平移)。"""
    check("地板线是一段真正有意义的时间（>= 5s @ 30fps），不是形同虚设的极小值",
          _QUOTE_MIN_START_FRAMES >= 150, _QUOTE_MIN_START_FRAMES)
    quotes = [{"mountFrame": 4, "endFrame": 45, "text": "Hi there, it's David."}]  # grounded to ~0.2s, no intro
    _floor_shift_graphics(quotes, floor=_QUOTE_MIN_START_FRAMES)
    check("没有片头时，紧贴视频开头的 quote 仍被推到地板线之后",
          quotes[0]["mountFrame"] == _QUOTE_MIN_START_FRAMES, quotes[0])
    check("推移保留原有停留时长", quotes[0]["endFrame"] - quotes[0]["mountFrame"] == 41, quotes[0])

    later_quote = [{"mountFrame": 900, "endFrame": 1000, "text": "a quote much later in the video"}]
    _floor_shift_graphics(later_quote, floor=_QUOTE_MIN_START_FRAMES)
    check("已经在地板线之后的 quote（视频中段/后段）完全不受影响",
          later_quote[0]["mountFrame"] == 900, later_quote[0])


def test_no_op_on_empty_or_none():
    try:
        _floor_shift_graphics(None, floor=100)
        _floor_shift_graphics([], floor=100)
        _floor_shift_zone_headers(None, floor=100)
        _floor_shift_zone_headers([], floor=100)
        raised = False
    except Exception:
        raised = True
    check("None/空列表不报错", not raised)


def main():
    test_shifts_below_floor_preserving_duration()
    test_before_after_second_reveal_frame_shifts_too()
    test_row_offsets_stay_relative_and_valid()
    test_zone_headers_shift_from_to_frame()
    test_quote_min_start_floor_keeps_facecam_visible_at_video_open()
    test_no_op_on_empty_or_none()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All pipeline_runner tests passed.")


if __name__ == "__main__":
    main()

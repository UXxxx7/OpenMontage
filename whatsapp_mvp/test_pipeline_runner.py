# pipeline_runner 里纯数据变换函数的单测（不依赖真实转写/渲染）——目前只有
# Fix C2 的挂载时间整体平移。其余 pipeline_runner 逻辑深度耦合转写/渲染子
# 进程，走真实 /jobs 端到端验证（见 CLAUDE-v2.md 的验证阶梯），不在这里补单测。
#
# Run: uv run python -m whatsapp_mvp.test_pipeline_runner

from __future__ import annotations

from whatsapp_mvp.pipeline_runner import (
    _floor_shift_graphics, _floor_shift_zone_headers, _QUOTE_MIN_START_FRAMES,
    _shift_off_dominant_windows, _shift_off_dominant_windows_headers,
    _fill_intro_lead_dead_space, _restore_facecam_before_end,
    _FACECAM_RESTORE_BUFFER_FRAMES, _recompute_scenes_from_content,
    _mode_schedule_to_scenes, _TRANSITION_HOLD_FRAMES,
    _content_unchanged,
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


# Fix C24 回归测试——真实生产复现 job_452ef6c48100：COVERAGE/RISK 这两个
# zoneHeader 各自的整个显示区间都落在了一次视频中段的 Dominant 窗口内
# （mode_schedule 在片头之后按内容反复回到 Dominant 是设计如此，Fix C14 的
# 注释已经说明——问题是 content-zone 元素的固定 Y 坐标没有跟着躲开）。
_MID_VIDEO_MODE_SCHEDULE = [
    {"frame": 0, "mode": "dominant"},
    {"frame": 40, "mode": "workflow"},
    {"frame": 200, "mode": "dominant"},   # 卡片中途重新变大（内容驱动，真实时间点）
    {"frame": 592, "mode": "workflow"},   # 收回 docked——转场再花 _CARD_TRANSITION_FRAMES(20) 帧完成
]


def test_shift_off_dominant_windows_pushes_past_mid_video_hold():
    items = [{"mountFrame": 378, "endFrame": 592}]  # 落在 200-592 这次 dominant 窗口内
    _shift_off_dominant_windows(items, _MID_VIDEO_MODE_SCHEDULE)
    check("落在中段 dominant 窗口内的图形被推到卡片真正收起之后",
          items[0]["mountFrame"] == 592 + 20, items[0])
    check("平移保留原有停留时长", items[0]["endFrame"] - items[0]["mountFrame"] == 592 - 378, items[0])


def test_shift_off_dominant_windows_no_op_when_already_workflow():
    # endFrame=180 完全在下一次 dominant 增长开始(200-20=180)之前结束——
    # 真正的"整段都在 workflow 窗口内"no-op 场景，不触发 Fix C49 的截短。
    items = [{"mountFrame": 100, "endFrame": 180}]  # 落在 40-200 这段 workflow 窗口内
    _shift_off_dominant_windows(items, _MID_VIDEO_MODE_SCHEDULE)
    check("已经在 workflow 窗口内、且结束在下次增长之前的图形不受影响",
          items[0] == {"mountFrame": 100, "endFrame": 180}, items[0])


def test_shift_off_dominant_windows_caps_endFrame_before_regrow():
    """Fix C49 回归测试——跟 Fix C47 一模一样的形状，只是这次是普通的
    dataCard/countdown/gauge 而不是 zoneHeader：起点在 workflow 内，但
    endFrame 撞上了下一次真正的 dominant 增长（200-20=180）。"""
    items = [{"mountFrame": 100, "endFrame": 300}]  # 跟旧版本共享同一个坐标，但这次会被截短
    _shift_off_dominant_windows(items, _MID_VIDEO_MODE_SCHEDULE)
    check("endFrame 被截短到下次增长真正开始之前(200-20=180)",
          items[0] == {"mountFrame": 100, "endFrame": 180}, items[0])


def test_shift_off_dominant_windows_headers_same_behavior():
    headers = [{"fromFrame": 378, "toFrame": 592}]
    _shift_off_dominant_windows_headers(headers, _MID_VIDEO_MODE_SCHEDULE)
    check("zoneHeader 用 fromFrame/toFrame 字段也一样被推移",
          headers[0]["fromFrame"] == 612 and headers[0]["toFrame"] == 612 + (592 - 378), headers[0])


# Fix C47 回归测试——同一支 job_452ef6c48100，就在验证 C46 的同一次真实
# 渲染里复现：C24 只检查 fromFrame 那一刻的模式（起点在 workflow 就判定
# no-op），没检查 toFrame 之前模式会不会再变回 dominant。真实案例：
# COVERAGE 的 zoneHeader fromFrame=381 时确实是 workflow，但卡片在
# toFrame=592 之前的 572 帧（592 - _CARD_TRANSITION_FRAMES）就已经开始长回
# dominant——header 还在显示的最后 20 帧跟正在长大的卡片撞在一起，vision QA
# 真实抓到了标题文字叠在说话人身上的这一帧。
_COVERAGE_MODE_SCHEDULE = [
    {"frame": 0, "mode": "dominant"},
    {"frame": 180, "mode": "workflow"},
    {"frame": 592, "mode": "dominant"},  # 内容驱动的真实回涨时间点（Fix C14）
]


def test_shift_off_dominant_windows_headers_caps_toFrame_before_regrow():
    headers = [{"fromFrame": 381, "toFrame": 592}]  # 真实 COVERAGE zoneHeader
    _shift_off_dominant_windows_headers(headers, _COVERAGE_MODE_SCHEDULE)
    check("起点在 workflow 内，fromFrame 不被平移",
          headers[0]["fromFrame"] == 381, headers[0])
    check("toFrame 被截短到卡片真正开始长大之前(592-20=572)，不再跟长大中的卡片重叠",
          headers[0]["toFrame"] == 572, headers[0])


def test_shift_off_dominant_windows_headers_no_op_when_toFrame_before_regrow():
    headers = [{"fromFrame": 200, "toFrame": 400}]  # 整段都在 workflow 窗口内结束
    _shift_off_dominant_windows_headers(headers, _COVERAGE_MODE_SCHEDULE)
    check("toFrame 本来就在下一次长大之前，不被截短",
          headers[0] == {"fromFrame": 200, "toFrame": 400}, headers[0])


def test_shift_off_dominant_windows_headers_no_op_when_no_further_dominant():
    # 后面再没有 dominant 窗口——没有什么好躲的，保持原状。
    schedule = [{"frame": 0, "mode": "dominant"}, {"frame": 40, "mode": "workflow"}]
    headers = [{"fromFrame": 100, "toFrame": 900}]
    _shift_off_dominant_windows_headers(headers, schedule)
    check("后面没有 dominant 窗口时 toFrame 不被截短",
          headers[0] == {"fromFrame": 100, "toFrame": 900}, headers[0])


def test_shift_off_dominant_windows_leaves_unrescuable_item_alone():
    # 最后一段一直是 Dominant 到片尾，没有下一个 workflow 窗口可以躲。
    schedule = [{"frame": 0, "mode": "dominant"}, {"frame": 40, "mode": "workflow"},
                {"frame": 900, "mode": "dominant"}]
    items = [{"mountFrame": 950, "endFrame": 1000}]
    _shift_off_dominant_windows(items, schedule)
    check("找不到后续 workflow 窗口时保持原状（没有更好的位置可躲）",
          items[0] == {"mountFrame": 950, "endFrame": 1000}, items[0])


# Fix C25 回归测试——真实生产复现 job_452ef6c48100，用户反馈"it's David
# from... 没用啊"：intro 结束后的死空间恰好被全片第一句话（自我介绍）覆盖，
# machine 兜底截了一小段塞进卡片，跟片头 IntroTitle 已经展示过的身份信息
# 重复，还经常被字数上限砍在词中间。
_MINIMAL_INTRO_GAP_PROPS = {
    "durationSeconds": 20.0,
    "introOutFrame": 80,
    "scenes": [
        {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
        {"frame": 20, "x": 60, "y": 104, "w": 960, "h": 900},
    ],
    "dataCards": [{"title": "T", "x": 60, "y": 1170, "width": 960, "mountFrame": 300, "endFrame": 450,
                   "rows": [{"label": "X", "value": 1, "mountOffset": 0}]}],
}
_INTRO_GAP_FINDING = [{"check": "intro_lead_dead_space", "gap_start": 80, "gap_end": 200}]


def test_self_intro_repeat_is_not_inserted():
    captions = [
        {"startMs": 1000, "endMs": 6000, "text": "it's David from Pacific life quick reminder"},
        {"startMs": 6000, "endMs": 9000, "text": "your policy is coming up for renewal"},
    ]
    segments = [{"start": 1.0, "end": 6.0, "text": "it's David from Pacific life quick reminder"}]
    result = _fill_intro_lead_dead_space(dict(_MINIMAL_INTRO_GAP_PROPS), _INTRO_GAP_FINDING, captions, segments)
    check("gap 恰好被全片第一句话(自我介绍)覆盖时，不插入重复的兜底卡片",
          not result.get("topicCards"), result.get("topicCards"))


def test_non_first_caption_still_gets_fallback_card():
    captions = [
        {"startMs": -3000, "endMs": -1000, "text": "an earlier line before this gap, not the first caption"},
        {"startMs": 1000, "endMs": 6000, "text": "some genuinely later content overlapping the gap"},
    ]
    segments = [{"start": -3.0, "end": -1.0, "text": "an earlier line before this gap, not the first caption"}]
    result = _fill_intro_lead_dead_space(dict(_MINIMAL_INTRO_GAP_PROPS), _INTRO_GAP_FINDING, captions, segments)
    check("gap 被非首句字幕覆盖时，兜底逻辑照常插入卡片（既有行为不受影响）",
          bool(result.get("topicCards")), result.get("topicCards"))


def test_self_intro_repeat_across_multiple_phrase_captions_not_inserted():
    # Fix C43 real regression scenario (job_452ef6c48100): the self-intro
    # SENTENCE ("Hi there, it's David from Pacific Life, quick reminder") is
    # one spoken segment, but split into TWO phrase-level captions for
    # subtitle display. The gap (frames 80-200 = 2667-6667ms, same window
    # _INTRO_GAP_FINDING uses) starts partway through the sentence, so the
    # first phrase-caption ("Hi there,") ends BEFORE the gap even starts and
    # is correctly excluded from `overlapping` -- but overlapping[0] is then
    # the SECOND phrase-caption ("it's David from..."), not literally
    # captions[0]. The old identity check (`overlapping[0] is captions[0]`)
    # missed this entirely and let the repeat card through.
    captions = [
        {"startMs": 0, "endMs": 2500, "text": "Hi there,"},
        {"startMs": 2500, "endMs": 4600, "text": "it's David from Pacific Life, quick reminder"},
        {"startMs": 4600, "endMs": 9000, "text": "your policy is coming up for renewal"},
    ]
    segments = [{"start": 0.0, "end": 4.6, "text": "Hi there, it's David from Pacific Life, quick reminder"}]
    result = _fill_intro_lead_dead_space(dict(_MINIMAL_INTRO_GAP_PROPS), _INTRO_GAP_FINDING, captions, segments)
    check("gap 覆盖的是第一句话的后半段(不同的 phrase-caption 对象)时，仍然被识别为自我介绍重复，不插卡",
          not result.get("topicCards"), result.get("topicCards"))


_DURATION_FRAMES = 900  # 30s @ 30fps

_FACECAM_NEVER_RESTORED_PROPS = {
    "durationSeconds": 30.0,
    "scenes": [{"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100}],
    "sections": [{"fromFrame": 300, "toFrame": 900, "icon": "clock"}],
    "opacityKeyframes": [{"frame": 299, "opacity": 1.0}, {"frame": 314, "opacity": 0.0}],
}
_FACECAM_NEVER_RESTORED_FINDING = [{"check": "facecam_never_restored", "hidden_from_frame": 300}]


def test_restore_facecam_caps_section_with_runway_before_end():
    result = _restore_facecam_before_end(
        dict(_FACECAM_NEVER_RESTORED_PROPS), _FACECAM_NEVER_RESTORED_FINDING, _DURATION_FRAMES)
    new_end = result["sections"][0]["toFrame"]
    check("接管区间被裁到片尾前留出 buffer，不是继续跑到片尾",
          new_end == _DURATION_FRAMES - _FACECAM_RESTORE_BUFFER_FRAMES, new_end)
    # 真正要保证的是"说话人恢复可见"（opacity 淡回 1.0），不是卡片尺寸——
    # dominant(满尺寸)但可见完全没问题，intro 本来就是这个状态；只有"永远
    # 透明"才是 facecam_never_restored 要抓的 bug。
    restore_frames = [k["frame"] for k in result["opacityKeyframes"] if k["opacity"] == 1.0]
    check("裁剪后 opacityKeyframes 里有一个淡回 opacity=1.0 的关键帧，且落在片尾之前",
          any(f < _DURATION_FRAMES for f in restore_frames if f >= new_end), result["opacityKeyframes"])


def test_restore_facecam_no_op_without_matching_finding():
    result = _restore_facecam_before_end(dict(_FACECAM_NEVER_RESTORED_PROPS), [], _DURATION_FRAMES)
    check("没有 facecam_never_restored 发现时原样返回", result == _FACECAM_NEVER_RESTORED_PROPS)


def test_restore_facecam_no_op_when_no_room_to_cap():
    # hidden_from 已经离片尾很近（比 buffer 还短），裁了也没意义——原样返回。
    tight_finding = [{"check": "facecam_never_restored", "hidden_from_frame": _DURATION_FRAMES - 10}]
    result = _restore_facecam_before_end(dict(_FACECAM_NEVER_RESTORED_PROPS), tight_finding, _DURATION_FRAMES)
    check("隐藏区间起点离片尾太近、没有裁剪空间时原样返回", result == _FACECAM_NEVER_RESTORED_PROPS)


def test_recompute_scenes_from_content_covers_a_late_inserted_card():
    # Fix C38 real scenario (job_452ef6c48100): scenes already computed once
    # (only a gauge from 400-700 justified workflow mode), then a topicCard
    # gets inserted afterward (mimicking what C13 does) at 750-850 -- a
    # naive/stale scenes array would show the card going Dominant right at
    # this new card's own mount frame, since nothing recomputed after the
    # insertion.
    props = {
        "durationSeconds": 30.0,
        "gauges": [{"label": "x", "mountFrame": 400, "endFrame": 700, "x": 60, "y": 1040}],
        "scenes": [{"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
                   {"frame": 20, "x": 60, "y": 104, "w": 960, "h": 900},
                   {"frame": 710, "x": 60, "y": 104, "w": 960, "h": 1100}],  # stale: nothing covers 750-850 yet
    }
    props["topicCards"] = [{"headline": "late card", "mountFrame": 750, "endFrame": 850, "x": 60, "y": 1040}]
    result = _recompute_scenes_from_content(props, 900)

    def mode_at(frame):
        state = "dominant"
        for s in result["scenes"]:
            if s["frame"] <= frame:
                state = "workflow" if s["h"] < 1000 else "dominant"
            else:
                break
        return state

    check("late-inserted topicCard's own window is now covered by workflow mode (not Dominant)",
          mode_at(800) == "workflow", result["scenes"])


def test_recompute_scenes_from_content_reapplies_dominant_avoidance():
    """Fix C50 回归测试——真实生产复现（job_452ef6c48100，就在验证 C47/C49
    的下一次真实渲染里）：C47/C49 的避让检查只在 `_build()` 里跑了一次，用
    的是那一刻的 mode_schedule；但 `_build()` 最后还会再调一次
    `_recompute_scenes_from_content`（Fix C33/C38 本身要求"任何挪动内容区
    图形之后都要重算"，而 C47/C49 的避让本身就是"挪动内容区图形"），从
    这一刻起才算真正定型的 mode_schedule，从来没有被拿去重新检查过
    zoneHeaders 会不会撞上它。真实复现：一个 gauge 撑出 200-400 这段
    workflow 窗口，DETAILS 的 zoneHeader fromFrame=250(在窗口内，安全)，
    toFrame=450(超出窗口，撞上卡片从 400 帧开始长回 dominant)——旧代码
    这个 toFrame 永远不会被检查，因为避让只在 recompute *之前* 跑过一次。
    验证：既然避让已经搬进 `_recompute_scenes_from_content` 内部，直接调
    这个函数也能拿到修正后的 toFrame，不需要依赖调用方记得在正确的时机
    再调一次。"""
    props = {
        "durationSeconds": 30.0,
        "gauges": [{"label": "x", "mountFrame": 200, "endFrame": 400, "x": 60, "y": 1040}],
        "zoneHeaders": [{"title": "DETAILS", "fromFrame": 250, "toFrame": 450, "x": 60, "y": 1040}],
        "scenes": [],
    }
    result = _recompute_scenes_from_content(props, 900)
    check("zoneHeader 的 toFrame 被截短到卡片真正开始长回 dominant 之前(400-20=380)",
          result["zoneHeaders"][0]["toFrame"] == 380, result["zoneHeaders"])
    check("fromFrame 本来就安全，不受影响",
          result["zoneHeaders"][0]["fromFrame"] == 250, result["zoneHeaders"])


def test_transition_hold_shrinking_arrives_early_and_holds():
    # Original Fix C21 scenario: card is Dominant for a long time (a takeover
    # section runs late), then shrinks to Workflow -- should arrive at the
    # smaller size quickly and hold there, not slowly shrink the whole gap.
    schedule = [{"frame": 0, "mode": "dominant"}, {"frame": 592, "mode": "workflow", "contentWidth": 960}]
    scenes, _ = _mode_schedule_to_scenes(schedule)
    check("缩小方向：提前到达更小尺寸并一直停留（原 C21 行为不受影响）",
          scenes == [
              {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
              {"frame": 20, "x": 60, "y": 104, "w": 960, "h": 900},
              {"frame": 592, "x": 60, "y": 104, "w": 960, "h": 900},
          ], scenes)


def test_transition_hold_growing_stays_small_until_the_end():
    # Fix C39 real scenario (job_452ef6c48100): card is Workflow (docked) for
    # a long stretch because content covers it, THEN the schedule says
    # Dominant much later. The card must stay docked for nearly the whole
    # stretch and only grow right before the Dominant frame -- growing early
    # means the oversized card lands directly on top of content still on screen.
    ranges_schedule = [{"frame": 0, "mode": "dominant"},
                        {"frame": 390, "mode": "workflow", "contentWidth": 960},
                        {"frame": 850, "mode": "dominant"}]
    scenes, _ = _mode_schedule_to_scenes(ranges_schedule)
    check("长大方向：不提前长大，保持小尺寸直到贴近 cur 帧才长大",
          scenes[-2] == {"frame": 850 - _TRANSITION_HOLD_FRAMES, "x": 60, "y": 104, "w": 960, "h": 900},
          scenes)
    check("cur 自己那一帧仍然是长大后的尺寸", scenes[-1]["h"] == 1100, scenes)


def test_content_unchanged_ignores_derived_fields_but_detects_real_changes():
    # 派生字段（scenes/opacityKeyframes 由 _recompute_scenes_from_content 每次
    # 重算）不同不该算"内容变了"——不然这个检查永远判定"变了"，形同虚设。
    props_a = {
        "chapters": [{"label": "intro"}],
        "dataCards": [{"title": "Coverage", "value": 8400}],
        "scenes": [{"frame": 0, "h": 900}],
        "opacityKeyframes": [{"frame": 0, "opacity": 1}],
    }
    props_b_same_content_different_derived = {
        "chapters": [{"label": "intro"}],
        "dataCards": [{"title": "Coverage", "value": 8400}],
        "scenes": [{"frame": 999, "h": 500}],
        "opacityKeyframes": [{"frame": 50, "opacity": 0}],
    }
    check("derived-field-only diff counts as unchanged",
          _content_unchanged(props_a, props_b_same_content_different_derived) is True)

    props_c_real_content_change = {
        "chapters": [{"label": "intro"}],
        "dataCards": [{"title": "Coverage", "value": 6300}],  # 数值真的变了
        "scenes": [{"frame": 0, "h": 900}],
        "opacityKeyframes": [{"frame": 0, "opacity": 1}],
    }
    check("real content diff (dataCards value) is detected as changed",
          _content_unchanged(props_a, props_c_real_content_change) is False)

    props_d_missing_field = {"chapters": [{"label": "intro"}]}
    check("a field present in one and absent in the other counts as changed",
          _content_unchanged(props_a, props_d_missing_field) is False)


def main():
    test_shifts_below_floor_preserving_duration()
    test_before_after_second_reveal_frame_shifts_too()
    test_row_offsets_stay_relative_and_valid()
    test_zone_headers_shift_from_to_frame()
    test_quote_min_start_floor_keeps_facecam_visible_at_video_open()
    test_no_op_on_empty_or_none()
    test_shift_off_dominant_windows_pushes_past_mid_video_hold()
    test_shift_off_dominant_windows_no_op_when_already_workflow()
    test_shift_off_dominant_windows_caps_endFrame_before_regrow()
    test_shift_off_dominant_windows_headers_same_behavior()
    test_shift_off_dominant_windows_headers_caps_toFrame_before_regrow()
    test_shift_off_dominant_windows_headers_no_op_when_toFrame_before_regrow()
    test_shift_off_dominant_windows_headers_no_op_when_no_further_dominant()
    test_shift_off_dominant_windows_leaves_unrescuable_item_alone()
    test_self_intro_repeat_is_not_inserted()
    test_non_first_caption_still_gets_fallback_card()
    test_self_intro_repeat_across_multiple_phrase_captions_not_inserted()
    test_restore_facecam_caps_section_with_runway_before_end()
    test_restore_facecam_no_op_without_matching_finding()
    test_restore_facecam_no_op_when_no_room_to_cap()
    test_recompute_scenes_from_content_covers_a_late_inserted_card()
    test_recompute_scenes_from_content_reapplies_dominant_avoidance()
    test_transition_hold_shrinking_arrives_early_and_holds()
    test_transition_hold_growing_stays_small_until_the_end()
    test_content_unchanged_ignores_derived_fields_but_detects_real_changes()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All pipeline_runner tests passed.")


if __name__ == "__main__":
    main()

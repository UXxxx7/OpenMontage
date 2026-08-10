# content_planner 字段映射单测（对齐 feat/pipeline-remove-filler-apply-style 合并后的
# 完整 Data Display Analysis 输出形状：4 种 visual 类型 + mode_schedule），不依赖 LLM。
#
# Run: uv run python -m whatsapp_mvp.test_content_planner

from __future__ import annotations

from whatsapp_mvp.content_planner import (
    FPS, MOUNT_LEAD_FRAMES, MIN_GAP_AFTER_PREVIOUS_FRAMES, _to_frame_plan, _zero_value_titles,
    _ground_data_point_seconds, _number_candidates, _find_grounded_seconds, _workflow_mode_schedule,
    _find_grounded_word, _keyword_matches, _KEYWORD_EXIT_HOLD_FRAMES, _COUNT_UP_ROW_ANIM_FRAMES,
    _STACK_EXIT_BUFFER_FRAMES, _TAKEOVER_HARD_CAP_FRAMES, _resolve_same_slot_overlaps,
    _plan_gauge, _plan_process_timeline,
)

FAILED = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL") + f" [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def main():
    # 1. 章节字段映射 + 排序
    plan = _to_frame_plan({"chapters": [
        {"at_seconds": 5.0, "label": "B"}, {"at_seconds": 1.0, "label": "A"},
    ]}, duration=30.0)
    check("chapters: at_seconds->atFrame + 升序", [c["atFrame"] for c in plan["chapters"]] == [30, 150], plan["chapters"])

    # 2. 无数据点是正常结果，四种图形都为空，mode_schedule 全程 dominant
    plan = _to_frame_plan({"chapters": [], "data_points": []}, duration=30.0)
    check("空 data_points -> 四种图形均空", not any(
        plan[k] for k in ("data_cards", "gauges", "countdowns", "calendar_events")))
    check("空计划 mode_schedule 全程 dominant",
          plan["mode_schedule"] == [{"frame": 0, "mode": "dominant"}], plan["mode_schedule"])

    # 3. count_up 映射：mountFrame = 首行帧 - MOUNT_LEAD_FRAMES；进 workflow 窗口
    plan = _to_frame_plan({"chapters": [], "data_points": [{
        "visual": "count_up", "title": "T",
        "rows": [{"label": "A", "seconds": 10.0, "value": 100, "tone": "good"}],
    }]}, duration=30.0)
    card = plan["data_cards"][0]
    check("count_up mountFrame = 行帧 - lead", card["mountFrame"] == round(10.0 * FPS) - MOUNT_LEAD_FRAMES, card)
    check("count_up 触发 workflow 模式", any(m["mode"] == "workflow" for m in plan["mode_schedule"]), plan["mode_schedule"])

    # 4. 其余三种 visual 各自落到对应数组
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "gauge", "seconds": 5.0, "title": "R", "leftLabel": "SAFE", "rightLabel": "RISK", "value": 0.8},
        {"visual": "countdown", "seconds": 8.0, "value": 30, "unitLabel": "DAYS", "label": "LEFT", "headline": "h"},
        {"visual": "calendar", "seconds": 12.0, "year": 2026, "month": 7, "targetDay": 28, "eventLabel": "DUE"},
    ]}, duration=30.0)
    check("gauge/countdown/calendar 各归其位",
          len(plan["gauges"]) == 1 and len(plan["countdowns"]) == 1 and len(plan["calendar_events"]) == 1,
          {k: len(plan[k]) for k in ("gauges", "countdowns", "calendar_events")})

    # 5b. endFrame 接力钳制（merge runbook P2 任务）+ chronological mount floor
    # （P3 补的另一半）：同坑位的两个图形不能重叠，且后者不能提前到前者还没
    # 结束就上场——两者结合后，前者的 endFrame 到后者的 mountFrame 之间应该
    # 有至少 MIN_GAP_AFTER_PREVIOUS_FRAMES 的间隔，而不是像只有钳制时那样
    # 严格相等（严格相等会让两者的 15 帧淡出/上场动画紧贴甚至重叠）。
    # 时间上挨得近的两个图形现在 STACK（不同 y 车道共存，同一时刻整组退场），
    # 而不是旧的"前一个消失后下一个才上场"串行——那正是"一次只有一张孤零零
    # 卡片、画面大片留白"的系统性根因（确认过的用户反馈；参考成片
    # CoverageSection 一个章节内 3-4 个元素积累共存）。
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "count_up", "title": "A",
         "rows": [{"label": "X", "seconds": 5.0, "value": 1}]},
        {"visual": "count_up", "title": "B",
         "rows": [{"label": "Y", "seconds": 7.0, "value": 2}]},
    ]}, duration=60.0)
    a, b = sorted(plan["data_cards"], key=lambda c: c["mountFrame"])
    check("时间相近的两张卡堆叠共存（不同 y 车道），不再串行",
          b["y"] > a["y"] and b["mountFrame"] < a["endFrame"],
          {"a": (a["mountFrame"], a["endFrame"], a["y"]), "b": (b["mountFrame"], b["endFrame"], b["y"])})
    check("同一堆叠的元素整组退场（endFrame 一致）",
          a["endFrame"] == b["endFrame"], {"a_end": a["endFrame"], "b_end": b["endFrame"]})
    check("最后一张卡保留自己的停留窗口 endFrame", b.get("endFrame", 0) > b["mountFrame"], b.get("endFrame"))

    # 5c. 跨类型也一样堆叠（count_up 后接 gauge）
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "count_up", "title": "A", "rows": [{"label": "X", "seconds": 5.0, "value": 1}]},
        {"visual": "gauge", "seconds": 7.0, "title": "R", "leftLabel": "L", "rightLabel": "R", "value": 0.5},
    ]}, duration=60.0)
    card = plan["data_cards"][0]; gauge = plan["gauges"][0]
    check("跨类型堆叠: 仪表盘在卡片下方车道共存",
          gauge["y"] > card["y"] and card["endFrame"] == gauge["endFrame"],
          {"card": (card["mountFrame"], card["endFrame"], card["y"]),
           "gauge": (gauge["mountFrame"], gauge["endFrame"], gauge["y"])})

    # 5b-2. 带 "pill" 的数据点在同一堆叠里生成伴随主图形的强调 pill
    # （参考成片 CoverageSection 的 terracotta 收尾 pill），随堆叠整组退场。
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "count_up", "title": "A", "pill": "Policy Active — Renew in 30 Days",
         "rows": [{"label": "X", "seconds": 5.0, "value": 1}]},
    ]}, duration=60.0)
    card = plan["data_cards"][0]
    check("pill 生成且堆叠在主图形下方、随堆叠退场",
          len(plan["pills"]) == 1
          and plan["pills"][0]["y"] > card["y"]
          and plan["pills"][0]["mountFrame"] > card["mountFrame"]
          and plan["pills"][0]["endFrame"] == card["endFrame"],
          {"card": (card["mountFrame"], card["endFrame"], card["y"]), "pills": plan["pills"]})

    # 5b-2b. 回归测试——真实生产 bug（job_9923d959512d，props_lint 抓到）：pill
    # 的 y 计算漏加了 stack_header_offset（跟"fits"分支里 entry_y 的算法不
    # 一致），导致 pill 少偏移了一整个 ZoneHeader 高度(130px)，直接落在主图形
    # 自己的矩形范围内，而不是完全在它下方——日历/数据卡的配套文案条在真实
    # 渲染里会跟自己的主图形重叠。非 takeover/非 solo 的普通章节（有
    # ZoneHeader，stack_header_offset=130）必须验证 pill 完全在主图形的估算
    # 高度之下，不能只满足"数值上更大"这种弱检查。
    from whatsapp_mvp.content_planner import _est_height, _STACK_GAP, _ZONE_HEADER_HEIGHT
    plan_hdr = _to_frame_plan({"chapters": [{"at_seconds": 0, "label": "DETAILS"}], "data_points": [
        {"visual": "count_up", "title": "A", "pill": "Policy Active — Renew in 30 Days",
         "rows": [{"label": "X", "seconds": 5.0, "value": 1}]},
    ]}, duration=60.0)
    card_hdr = plan_hdr["data_cards"][0]
    pill_hdr = plan_hdr["pills"][0]
    card_height = _est_height("count_up", card_hdr)
    check("普通章节(带 ZoneHeader)下，pill 完全在主图形估算高度之下，不落进它自己的矩形里",
          pill_hdr["y"] == card_hdr["y"] + card_height + _STACK_GAP,
          {"card": card_hdr, "pill": pill_hdr, "card_height": card_height, "zone_header_height": _ZONE_HEADER_HEIGHT})

    # 5b-3. 确认过的真实生产 bug（David 视频真实渲染）：日历图形带 pill，紧接
    # 着来了一张数据卡（间隔只有 63 帧），_STACK_EXIT_BUFFER_FRAMES 把共享
    # endFrame 逼得比 pill 自己的 mountFrame（主图形 mountFrame+45）还早——
    # 产出一条 endFrame(269) < mountFrame(271) 的倒挂条目，渲染层这张 pill
    # 会立刻判定"已过期"直接不显示（不是崩溃，是静默的时长为负）。用真实数字
    # 复现，验证防御性清理已经把这类条目挡住，不会有任何 endFrame<=mountFrame
    # 的图形流出到 props 里。
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "calendar", "seconds": 9.2, "year": 2026, "month": 7, "targetDay": 28,
         "eventLabel": "Policy Renewal", "pill": "Policy Renews July 28, 2026"},
        {"visual": "count_up", "title": "B", "rows": [{"label": "Y", "seconds": 11.3, "value": 2}]},
    ]}, duration=60.0)
    all_entries = (plan["calendar_events"] + plan["data_cards"] + plan["pills"]
                   + plan["gauges"] + plan["countdowns"] + plan["quotes"])
    check("紧邻图形挤压 pill 时序时，不会产出 endFrame<=mountFrame 的倒挂条目",
          all(e["endFrame"] > e["mountFrame"] for e in all_entries),
          [(e.get("title") or e.get("text") or "calendar", e["mountFrame"], e["endFrame"]) for e in all_entries])

    # 5c-2. 时间隔得远（超过 join window）的两个图形仍然各归各的段落，
    # 前者必须在后者上场前完全退场（同 y 车道不重叠）。
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "count_up", "title": "A", "rows": [{"label": "X", "seconds": 5.0, "value": 1}]},
        {"visual": "count_up", "title": "B", "rows": [{"label": "Y", "seconds": 40.0, "value": 2}]},
    ]}, duration=60.0)
    a, b = sorted(plan["data_cards"], key=lambda c: c["mountFrame"])
    check("隔得远的图形不堆叠：前者在后者上场前退场",
          a["y"] == b["y"] and a["endFrame"] < b["mountFrame"],
          {"a": (a["mountFrame"], a["endFrame"], a["y"]), "b": (b["mountFrame"], b["endFrame"], b["y"])})

    # 5c-3. 章节边界：即使时间上挨得很近本会堆叠，跨章节也必须各归各的段落
    # ("跟着说话内容走"的机制化——章节变了=新段落，哪怕时间/高度都够堆)。
    plan = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "A"}, {"at_seconds": 10, "label": "B"}],
        "data_points": [
            {"visual": "count_up", "title": "A1", "rows": [{"label": "X", "seconds": 8.0, "value": 1}]},
            {"visual": "count_up", "title": "B1", "rows": [{"label": "Y", "seconds": 11.0, "value": 2}]},
        ],
    }, duration=30.0)
    a, b = sorted(plan["data_cards"], key=lambda c: c["mountFrame"])
    # Both start a fresh stack at the same base y (correct — matches the
    # "far apart in time" case above); the actual proof of NOT stacking is
    # that they never coexist on screen, unlike the same-chapter stacking
    # tests above where b's mountFrame lands strictly before a's endFrame.
    check("跨章节不堆叠（哪怕时间挨得很近）：两者不同屏共存",
          a["endFrame"] <= b["mountFrame"],
          {"a": (a["mountFrame"], a["endFrame"], a["y"]), "b": (b["mountFrame"], b["endFrame"], b["y"])})

    # 5c-4. 图形的固定时长若会拖过所在章节的边界，必须被截断在章节结束帧——
    # 泛化 _plan_process_timeline 已经在用的 atFrame->next atFrame 锚定，
    # 让"卡片时长跟转写脱节"对每种图形都失效，不只是 timeline 这一种。
    plan = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "A"}, {"at_seconds": 3, "label": "B"}],
        "data_points": [
            {"visual": "count_up", "title": "A1", "rows": [{"label": "X", "seconds": 1.0, "value": 1}]},
        ],
    }, duration=30.0)
    card = plan["data_cards"][0]
    chapter_b_at_frame = round(3.0 * FPS)
    check("固定时长图形的退场帧被裁在自己所在章节结束处，不拖到下一章",
          card["endFrame"] <= chapter_b_at_frame,
          {"endFrame": card["endFrame"], "chapter_b_atFrame": chapter_b_at_frame})

    # 5c-5. Quote 是全画布字卡：必须让 SpeakerCard 在这段时间隐藏
    # （mode_schedule 里对应帧段的 contentWidth 达到 SECTION_PIP_SENTINEL），
    # 不能像之前那样让字幕/引言文字直接叠在还显示着的说话人脸上
    # (确认过的真实 bug, job_24450b1eacfd / "preview(6)")。
    from whatsapp_mvp.content_planner import SECTION_PIP_SENTINEL
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "quote", "seconds": 5.0, "text": "a quote worth showing full canvas"},
    ]}, duration=30.0)
    quote = plan["quotes"][0]
    hides_card = any(
        m.get("mode") == "workflow" and m.get("contentWidth", 0) >= SECTION_PIP_SENTINEL
        and m["frame"] <= quote["mountFrame"] < (
            next((n["frame"] for n in plan["mode_schedule"] if n["frame"] > m["frame"]), quote["endFrame"] + 1)
        )
        for m in plan["mode_schedule"]
    )
    check("quote 期间 SpeakerCard 被隐藏（不再叠在脸上）", hides_card, plan["mode_schedule"])

    # 5c-6. 确认过的真实生产 bug（David 视频真实渲染）：一句 quote 恰好落在
    # 一个已经在全画布接管的章节（"takeover":true）里，会跟 SectionLayer 自己
    # 的标题+图标同屏叠在一起——两套"独占整个画布"的处理互相打架。接管章节
    # 本身就是这个时刻的视觉呈现（字幕已经带着同样的话），quote 不需要再单独
    # 渲染一份，应该被跳过而不是产出一张会叠加的卡片。
    plan = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "A", "takeover": True, "dark": True}],
        "data_points": [
            {"visual": "quote", "seconds": 5.0, "text": "this lands inside the takeover chapter"},
        ],
    }, duration=30.0)
    check("接管章节内的 quote 被跳过，不与 SectionLayer 自己的标题/图标叠加",
          len(plan["quotes"]) == 0, plan["quotes"])

    # 非接管章节里的 quote 不受影响，照常产出。
    plan = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "A", "takeover": False}],
        "data_points": [
            {"visual": "quote", "seconds": 5.0, "text": "this lands in a normal chapter"},
        ],
    }, duration=30.0)
    check("非接管章节内的 quote 照常产出", len(plan["quotes"]) == 1, plan["quotes"])

    # 6. intro/outro/双语章节映射（对齐 VeLL 参考成片的可自动化元素）
    plan = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "續期日期", "label_en": "RENEWAL"}],
        "intro": {"eyebrow": "policy renewal reminder", "title": "保單續期提醒", "subtitle": "Pacific Life"},
        "outro": {"kicker": "renew on time", "headline": "準時續保", "headline_accent": "保障不中斷",
                  "subtext": "有問題請聯絡我", "cta_label": "立即續保"},
    }, duration=60.0)
    check("chapter 带 labelEn", plan["chapters"][0].get("labelEn") == "RENEWAL", plan["chapters"][0])
    # Fix F1：没指定 variant 时默认落到 "title_card"（4 个 intro 变体里唯一
    # 原本就有的那个，行为不变），显式写进输出而不是留空——跟其它字段的默认
    # 值处理方式一致。
    check("intro 映射 + eyebrow 大写 + variant 默认 title_card", plan["intro"] == {
        "eyebrow": "POLICY RENEWAL REMINDER", "title": "保單續期提醒", "subtitle": "Pacific Life",
        "variant": "title_card"}, plan["intro"])
    check("outro 映射(cta_label->ctaLabel, accent 保留)", plan["outro"]["ctaLabel"] == "立即續保"
          and plan["outro"]["headlineAccent"] == "保障不中斷", plan["outro"])

    # 6b. Fix F1：intro 的 4 种视觉变体——之前只有 title_card(Pattern 2)被
    # 移植过来，2026-07-16 补齐另外 3 种（stats_hook/title_impact/chips）。
    for variant in ("stats_hook", "title_impact", "chips"):
        p = _to_frame_plan({
            "chapters": [{"at_seconds": 0, "label": "A"}],
            "intro": {"eyebrow": "E", "title": "T", "subtitle": "S", "variant": variant,
                      "brand_label": "ACME"},
        }, duration=30.0)
        check(f"intro variant '{variant}' 原样透传", p["intro"]["variant"] == variant, p["intro"])
        if variant == "title_impact":
            check("title_impact 带 brandLabel", p["intro"].get("brandLabel") == "ACME", p["intro"])
        else:
            check(f"{variant} 不带 brandLabel（只有 title_impact 用）", "brandLabel" not in p["intro"], p["intro"])
    p_bad = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "A"}],
        "intro": {"eyebrow": "E", "title": "T", "subtitle": "S", "variant": "not_a_real_variant"},
    }, duration=30.0)
    check("不认识的 variant 值退回默认 title_card，不让整条 plan 崩掉",
          p_bad["intro"]["variant"] == "title_card", p_bad["intro"])

    plan = _to_frame_plan({"chapters": [], "intro": {"eyebrow": "X"}, "outro": {"kicker": "Y"}}, duration=30.0)
    check("intro 无 title / outro 无 headline -> 不产出(None)", plan["intro"] is None and plan["outro"] is None)

    # 5d. mode_schedule 不能在挨得近的两个图形之间漏掉 workflow 收起——第二个
    # 图形的"提前收起"帧如果落在第一个图形"收起后长大"帧之前/相同，naive
    # 逐段生成 + strict-increasing dedup 会把 workflow 段悄悄丢掉，SpeakerCard
    # 全程停在 Dominant（卡片一直很大，第二个图形只能硬贴在卡片底部）——
    # 实测过这个 bug：日历后紧跟一张数据卡，数据卡完全没收起过。直接检查：
    # 数据卡上场那一帧，SpeakerCard 是不是真的已经处在 workflow。
    def _mode_at(schedule, frame):
        mode = "dominant"
        for m in schedule:
            if m["frame"] <= frame:
                mode = m["mode"]
            else:
                break
        return mode

    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "calendar", "seconds": 9.0, "year": 2026, "month": 7, "targetDay": 28, "eventLabel": "E"},
        {"visual": "count_up", "title": "T", "rows": [{"label": "X", "seconds": 11.5, "value": 1}]},
    ]}, duration=40.0)
    modes = plan["mode_schedule"]
    card_mount = plan["data_cards"][0]["mountFrame"]
    check("紧邻图形之间不漏 workflow 收起：数据卡上场时卡片必须已收起",
          _mode_at(modes, card_mount) == "workflow",
          {"mode_schedule": modes, "card_mount": card_mount})

    # 7. quote 类型：无数据视频的画布动画来源；进 workflow 窗口
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "quote", "seconds": 8.0, "text": "this changed everything", "attribution": "David"},
    ]}, duration=30.0)
    check("quote 映射 + 触发 workflow", len(plan["quotes"]) == 1
          and plan["quotes"][0]["text"] == "this changed everything"
          and any(m["mode"] == "workflow" for m in plan["mode_schedule"]),
          {"quotes": plan["quotes"], "modes": plan["mode_schedule"]})

    # 7b. contact_cue：确认过的真实用户反馈——QR/联系方式卡之前固定钉在片尾
    # 附近，跟视频里实际说"WhatsApp 我"的那一刻完全脱节。这里验证 mountFrame
    # 锚定在该数据点自己的 seconds（减 lead），而不是任何 duration 相关的值。
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "contact_cue", "seconds": 34.0},
    ]}, duration=44.0)
    check("contact_cue 映射到实际说话的那一刻，不是片尾偏移",
          plan["contact_cue"] is not None
          and plan["contact_cue"]["mountFrame"] == round(34.0 * FPS) - MOUNT_LEAD_FRAMES,
          plan["contact_cue"])

    # 无 contact_cue 数据点时该字段应为 None（下游会退回 outro/duration 兜底）
    plan_no_cue = _to_frame_plan({"chapters": [], "data_points": []}, duration=44.0)
    check("无 contact_cue 时字段为 None", plan_no_cue["contact_cue"] is None)

    # 7c. 确认过的真实生产 bug（David 视频真实渲染，frame 350，"Your Policy
    # Summary" 卡片显示 "Coverage: $0.0M"）：LLM 提取的 value 已经是"以百万为
    # 单位"的 1.5，但同时按 prompt 里"原始美元数值配 divideBy"的格式指引又带上
    # 了 divideBy=1000000，实际显示 1.5/1,000,000 保留 1 位小数 = "0.0"——原始
    # value 本身不是 0，旧的"只查 value==0"检查完全漏掉这种双重除法。
    raw_double_divided = {"data_points": [
        {"visual": "count_up", "title": "Your Policy Summary", "rows": [
            {"label": "Coverage", "seconds": 5.0, "value": 1.5,
             "prefix": "$", "divideBy": 1000000, "decimals": 1, "unit": "M"},
        ]},
    ]}
    check("双重除法导致显示为 0 的卡片被识别为坏值",
          _zero_value_titles(raw_double_divided) == ["Your Policy Summary"],
          _zero_value_titles(raw_double_divided))

    # 对照组：同样的 divideBy/decimals，但 value 是原始美元数值（1,500,000），
    # 显示为 "1.5" —— 不应被误判为坏值。
    raw_correct = {"data_points": [
        {"visual": "count_up", "title": "Your Policy Summary", "rows": [
            {"label": "Coverage", "seconds": 5.0, "value": 1500000,
             "prefix": "$", "divideBy": 1000000, "decimals": 1, "unit": "M"},
        ]},
    ]}
    check("正确的原始数值（除法后非 0）不被误判", _zero_value_titles(raw_correct) == [], raw_correct)

    # 7d. 确认过的真实生产 bug（David 视频真实渲染，job_f0c3412a1694）：一个
    # 完全没有可用 seconds 的脏数据点（count_up 的 rows 全部缺 seconds）让
    # _dp_seconds 按其自身设计返回 float("inf") 用于排序兜底；本次改动之前，
    # 章节归属查找会直接 round(inf * FPS)，OverflowError 不在下面 try/except
    # 捕获的 (KeyError, TypeError, ValueError) 之列，导致整条 apply_style 崩溃、
    # 降级成只有 remove_filler（零字幕/零图形）。这里断言这种脏数据点被安静
    # 跳过，plan_content 不再因此整体炸掉。
    plan = _to_frame_plan({"chapters": [{"at_seconds": 0, "label": "A"}], "data_points": [
        {"visual": "count_up", "title": "Broken", "rows": [{"label": "X", "value": 1}]},  # 缺 seconds
        {"visual": "count_up", "title": "Good", "rows": [{"label": "Y", "seconds": 5.0, "value": 2}]},
    ]}, duration=30.0)
    check("缺时间戳的脏数据点被跳过而不是让整个规划抛异常",
          len(plan["data_cards"]) == 1 and plan["data_cards"][0]["title"] == "Good", plan["data_cards"])

    # 8. 密度下限：空档检测 / 补规划 / 短片豁免
    #
    # 曾经这里还有一层机械兜底：补规划(REPLAN LLM)之后仍有空档就从转写原文
    # 硬切一段塞进 topic_card。用户明确反馈：这种卡片的内容就是原话，跟屏幕
    # 下方本来就在滚动的字幕一字不差，"为了有画面而有画面"，没有任何附加
    # 信息量——已删除(_fallback_topic_cards_for_gaps 连同 _slice_text_for_
    # window/_truncate_at_word_boundary 一并移除)。现在：LLM 补规划仍是
    # 第一选择(真正有信息量的图形)，补规划后仍有空档就接受现状(只有说话人
    # +字幕)，不再机械垫字幕卡片。
    from whatsapp_mvp.content_planner import _apply_richness_floor, _sparse_gaps
    # 60s 视频、无任何画布事件 -> 中段应报告空档
    bare = _to_frame_plan({"chapters": []}, duration=60.0)
    gaps = _sparse_gaps(bare, 60.0)
    check("裸计划在长视频上检出空档", len(gaps) >= 1, gaps)

    # 短片(12s)豁免：intro+outro 已覆盖，不该报空档
    check("短视频豁免密度检查", _sparse_gaps(_to_frame_plan({"chapters": []}, 12.0), 12.0) == [])

    # 禁用补规划(allow_replan=False)时，仍有空档就原样接受——不再机械垫字幕
    # 卡片。plan 应该原样返回(除了空 chapters 是同一个空列表这种无害差异，
    # 关键断言是没有任何新的 topic_card/quote 被塞进去，空档依旧被如实报告)。
    segs = [
        {"start": 4.0, "end": 7.0, "text": "this is the single most important thing to remember"},
    ]
    fixed = _apply_richness_floor({"chapters": []}, bare, segs, 60.0, allow_replan=False)
    check("禁用补规划时不再机械垫字幕卡片——没有凭空多出 topic_card",
          fixed["topic_cards"] == [], fixed["topic_cards"])
    check("空档如实保留(接受现状，而不是硬塞一张读起来跟字幕一样的卡片)",
          _sparse_gaps(fixed, 60.0) == gaps, {"before": gaps, "after": _sparse_gaps(fixed, 60.0)})

    # 已有覆盖的计划不触发下限（原计划原样返回）
    covered_raw = {"chapters": [], "data_points": [
        {"visual": "quote", "seconds": t, "text": f"line at {t} seconds long enough"} for t in (8.0, 20.0, 32.0, 44.0)
    ]}
    covered = _to_frame_plan(covered_raw, 60.0)
    same = _apply_richness_floor(covered_raw, covered, segs, 60.0, allow_replan=False)
    check("密度达标的计划不被改动", same is covered)

    # 5. 畸形条目容错：非 dict / 缺字段不炸、不产出
    plan = _to_frame_plan({"chapters": [{"label": "no at_seconds"}],
                           "data_points": ["not a dict", {"visual": "gauge"}]}, duration=30.0)
    check("畸形 chapter/data_points 跳过不炸",
          plan["chapters"] == [] and not plan["gauges"] and not plan["data_cards"])

    # 6. Phase 2 (arsenal expansion): step_list / topic_card / corner_card / zone_headers

    # step_list: 每一步的 activateOffset 相对卡片自己的 mountFrame，按各自 seconds 计算
    plan = _to_frame_plan({"chapters": [{"at_seconds": 0, "label": "STEPS"}], "data_points": [
        {"visual": "step_list", "title": "T", "steps": [
            {"label": "one", "seconds": 5.0},
            {"label": "two", "seconds": 7.0},
            {"label": "three", "seconds": 9.0},
        ]},
    ]}, duration=30.0)
    check("step_list 映射出 3 步", len(plan["step_lists"]) == 1 and len(plan["step_lists"][0]["steps"]) == 3,
          plan["step_lists"])
    sl = plan["step_lists"][0]
    check("step_list 首步 activateOffset == MOUNT_LEAD_FRAMES（卡片提前 lead 帧上场，首步在自己被说到的那一刻激活）",
          sl["steps"][0]["activateOffset"] == MOUNT_LEAD_FRAMES, sl)
    check("step_list 后续步骤的 activateOffset 随各自 seconds 递增",
          sl["steps"][1]["activateOffset"] > sl["steps"][0]["activateOffset"]
          and sl["steps"][2]["activateOffset"] > sl["steps"][1]["activateOffset"], sl)

    # step_list: 少于 2 步不产出
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "step_list", "steps": [{"label": "only one", "seconds": 5.0}]},
    ]}, duration=30.0)
    check("step_list 少于 2 步不产出", plan["step_lists"] == [], plan["step_lists"])

    # topic_card: 无 headline 不产出；有 headline 正常映射
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "topic_card", "headline": "A good tip", "sub": "detail", "icon": "lightbulb", "seconds": 10.0},
    ]}, duration=30.0)
    check("topic_card 正常映射", len(plan["topic_cards"]) == 1
          and plan["topic_cards"][0]["headline"] == "A good tip"
          and plan["topic_cards"][0]["icon"] == "lightbulb", plan["topic_cards"])
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "topic_card", "sub": "detail only, no headline", "seconds": 10.0},
    ]}, duration=30.0)
    check("topic_card 无 headline 不产出", plan["topic_cards"] == [], plan["topic_cards"])

    # corner_card: chat/progress 两种变体映射；不进入内容区堆叠系统（无 y 字段）
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "corner_card", "variant": "chat", "appName": "WhatsApp", "message": "hi there", "seconds": 8.0},
        {"visual": "corner_card", "variant": "progress", "label": "Uploading", "percent": 60, "seconds": 20.0},
    ]}, duration=30.0)
    check("corner_card 两种变体都映射且不占用内容区（无 y 字段）",
          len(plan["corner_cards"]) == 2 and all("y" not in c for c in plan["corner_cards"]),
          plan["corner_cards"])
    check("corner_card chat 变体字段正确",
          plan["corner_cards"][0]["variant"] == "chat" and plan["corner_cards"][0]["message"] == "hi there",
          plan["corner_cards"][0])
    check("corner_card progress 变体字段正确",
          plan["corner_cards"][1]["variant"] == "progress" and plan["corner_cards"][1]["percent"] == 60,
          plan["corner_cards"][1])

    # corner_card: 落在接管章节内时被跳过（SpeakerCard 此时被隐藏，锚不上）
    plan = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "A", "takeover": True, "dark": True}],
        "data_points": [
            {"visual": "corner_card", "variant": "progress", "label": "X", "percent": 50, "seconds": 5.0},
        ],
    }, duration=30.0)
    check("接管章节内的 corner_card 被跳过（SpeakerCard 此时不可见）", plan["corner_cards"] == [], plan["corner_cards"])

    # gauge: 缺 title 时整卡跳过（Fix C40 —— 真实生产复现，job_452ef6c48100
    # 交付的 gauge title 是空字符串，标题栏整条留白）
    gauge_missing_title = _plan_gauge(
        {"seconds": 10.0, "leftLabel": "SAFE", "rightLabel": "RISK", "value": 0.7}, min_mount_frame=0)
    check("gauge 缺 title 时不产出（不是留一个标题栏空白的半成品卡）",
          gauge_missing_title is None, gauge_missing_title)
    gauge_with_title = _plan_gauge(
        {"seconds": 10.0, "title": "Lapse Risk", "leftLabel": "SAFE", "rightLabel": "RISK", "value": 0.7},
        min_mount_frame=0)
    check("gauge 有 title 时正常产出", gauge_with_title is not None and gauge_with_title["title"] == "Lapse Risk",
          gauge_with_title)

    # zone_headers: 非接管章节里有数据点 -> 产出一个跟章节同跨度的 header；
    # 接管章节里有数据点 -> 不产出（SectionLayer 自己已经有大标题）
    plan = _to_frame_plan({
        "chapters": [
            {"at_seconds": 0, "label": "NORMAL", "label_en": "NORMAL EN"},
            {"at_seconds": 10, "label": "TAKEOVER", "takeover": True, "dark": True},
        ],
        "data_points": [
            {"visual": "count_up", "title": "T", "rows": [{"label": "X", "seconds": 2.0, "value": 1}]},
            {"visual": "gauge", "seconds": 12.0, "title": "G", "leftLabel": "A", "rightLabel": "B", "value": 0.5},
        ],
    }, duration=30.0)
    # fromFrame anchors to the first stack's own mount frame (when the card
    # actually finishes shrinking), NOT the chapter's raw atFrame=0 — a
    # header at atFrame would render on top of the still-Dominant-sized card
    # if the chapter starts before its first data point mounts (confirmed
    # real bug via stills, job_4c36cb17acb2-style synthetic test).
    #
    # toFrame is the STACK's own exit frame (Fix C1), NOT the chapter span —
    # confirmed real bug (job_e44166eb8c38): a header spanning the whole
    # chapter stayed on screen long after its own card had exited, including
    # through a later stretch where the SpeakerCard had regrown to Dominant
    # size, painting the header directly on top of the now-large facecam.
    # This count_up's own stack exits at mount+HOLD_AFTER_LAST_ROW_FRAMES(90),
    # well before the next chapter at 300 — the header must match that,
    # not stretch to 300.
    count_up_mount = round(2.0 * FPS) - MOUNT_LEAD_FRAMES
    count_up_exit = round(2.0 * FPS) + 90  # HOLD_AFTER_LAST_ROW_FRAMES
    check("非接管章节产出 zone_header，起点=首个堆叠的 mountFrame，止于该堆叠自己的退场帧（不是章节跨度）",
          len(plan["zone_headers"]) == 1 and plan["zone_headers"][0]["title"] == "NORMAL"
          and plan["zone_headers"][0]["fromFrame"] == count_up_mount
          and plan["zone_headers"][0]["toFrame"] == count_up_exit,
          plan["zone_headers"])
    check("zone_header 带上 titleEn", plan["zone_headers"][0].get("titleEn") == "NORMAL EN", plan["zone_headers"][0])

    # C1 核心回归：同一个非接管章节里有两段相隔很远的内容（中间有大段没有
    # 图形的间隙，SpeakerCard 会在间隙里回到 Dominant），必须产出两个独立的
    # header 窗口，中间的间隙没有 header——而不是一个跨越整个章节、盖住间隙里
    # 重新变大的卡片的 header。
    plan_gap = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "TIPS"}],
        "data_points": [
            {"visual": "topic_card", "headline": "first tip", "seconds": 5.0},
            {"visual": "topic_card", "headline": "second tip, far later", "seconds": 30.0},
        ],
    }, duration=60.0)
    check("同一章节两段相隔很远的内容 -> 两个独立 header 窗口",
          len(plan_gap["zone_headers"]) == 2, plan_gap["zone_headers"])
    if len(plan_gap["zone_headers"]) == 2:
        h0, h1 = plan_gap["zone_headers"]
        check("两个 header 窗口之间有间隙（第一个先结束，第二个后开始，不重叠也不相连）",
              h0["toFrame"] < h1["fromFrame"], plan_gap["zone_headers"])

    # 确认过的真实 bug（Phase 2 stills 验证发现）：章节起点 atFrame 明显早于
    # 该章节第一个数据点实际上场的时刻时，header 如果锚在 atFrame 上会画在
    # 还没收起的大卡片上面（SpeakerCard 直到第一个内容堆叠触发 workflow_range
    # 才真正缩小）。这里用一个起点相隔较远的例子直接锁定修复。
    plan = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "LATE"}],
        "data_points": [
            {"visual": "topic_card", "headline": "first content arrives late", "seconds": 8.0},
        ],
    }, duration=30.0)
    expected_mount = round(8.0 * FPS) - MOUNT_LEAD_FRAMES
    check("章节起点远早于首个数据点时，zone_header 不提前画在未收起的卡片上",
          plan["zone_headers"][0]["fromFrame"] == expected_mount
          and plan["zone_headers"][0]["fromFrame"] > 0,
          plan["zone_headers"])

    # 确认过的真实生产 bug（David 真实渲染，job_55689b544652）：CONTACT 章节
    # 紧跟在一个 takeover 章节（RISK）后面，CONTACT 自己第一个数据点的原始
    # 说话帧离章节边界很近，减去 MOUNT_LEAD_FRAMES 之后算出来的 mountFrame
    # 落回了 RISK 的地盘——"CONTACT" 标题在 RISK 自己的全画布图标还没淡出
    # 之前就先冒出来了。header 起点必须钳在本章 atFrame，不能早于它。
    plan = _to_frame_plan({
        "chapters": [
            {"at_seconds": 693 / FPS, "label": "RISK", "takeover": True, "dark": True, "warn": True},
            {"at_seconds": 1002 / FPS, "label": "CONTACT"},
        ],
        "data_points": [
            {"visual": "gauge", "seconds": 730 / FPS, "title": "G", "leftLabel": "A", "rightLabel": "B", "value": 0.4},
            {"visual": "contact_cue", "seconds": 1010 / FPS},
        ],
    }, duration=1331 / FPS)
    contact_header = next((h for h in plan["zone_headers"] if h["title"] == "CONTACT"), None)
    check("CONTACT 紧跟 takeover 章节时，header 起点被钳在本章 atFrame（不早于 1002）",
          contact_header is not None and contact_header["fromFrame"] == 1002, plan["zone_headers"])

    # 纯接管章节（无 label 意外情况除外）不产出 zone_header
    plan = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "ONLY TAKEOVER", "takeover": True, "dark": True}],
        "data_points": [
            {"visual": "gauge", "seconds": 5.0, "title": "G", "leftLabel": "A", "rightLabel": "B", "value": 0.5},
        ],
    }, duration=30.0)
    check("纯接管章节不产出 zone_header", plan["zone_headers"] == [], plan["zone_headers"])

    # 8. Fix B：timing FROM the dialogue —— 数据点的 seconds 校准到词级时间戳。
    # 确认过的真实生产 bug（David 视频真实渲染）：countdown 说的是"30 days"，
    # LLM 给的 seconds 是 4.7，但转写里"30"这个词真正的时间戳是 7.5s——校准后
    # mountFrame 应该基于 7.5s，不是 4.7s。
    real_word_timestamps = [
        {"word": "Quick", "start": 3.4, "end": 3.7},
        {"word": "reminder:", "start": 3.7, "end": 4.1},
        {"word": "Your", "start": 4.1, "end": 4.3},
        {"word": "policy", "start": 4.3, "end": 4.7},
        {"word": "is", "start": 4.7, "end": 4.8},
        {"word": "coming", "start": 4.8, "end": 5.1},
        {"word": "up", "start": 5.1, "end": 5.2},
        {"word": "for", "start": 6.4, "end": 6.6},
        {"word": "renewal", "start": 6.6, "end": 7.0},
        {"word": "in", "start": 7.0, "end": 7.1},
        {"word": "30", "start": 7.5, "end": 7.7},
        {"word": "days,", "start": 7.7, "end": 8.4},
    ]
    raw_countdown = {"data_points": [
        {"visual": "countdown", "seconds": 4.7, "value": 30, "unitLabel": "DAYS",
         "label": "RENEWAL", "headline": "Renewal in 30 days"},
    ]}
    _ground_data_point_seconds(raw_countdown, real_word_timestamps)
    check("countdown 的 seconds 校准到真正说\"30\"这个词的时间戳(7.5s)，不是 LLM 估计的 4.7s",
          raw_countdown["data_points"][0]["seconds"] == 7.5, raw_countdown["data_points"][0])

    grounded_plan = _to_frame_plan(raw_countdown, duration=30.0)
    expected_mount = round(7.5 * FPS) - MOUNT_LEAD_FRAMES
    check("校准后 mountFrame 基于 7.5s（175），不是基于 4.7s 的旧值（91）",
          grounded_plan["countdowns"][0]["mountFrame"] == expected_mount, grounded_plan["countdowns"])

    # 找不到匹配的词时保持原样，不校准（不是报错、也不是随便挪到最近的数字）
    raw_no_match = {"data_points": [
        {"visual": "countdown", "seconds": 4.7, "value": 99, "unitLabel": "DAYS",
         "label": "X", "headline": "Y"},
    ]}
    _ground_data_point_seconds(raw_no_match, real_word_timestamps)
    check("转写里没有匹配的数字时，seconds 保持原样",
          raw_no_match["data_points"][0]["seconds"] == 4.7, raw_no_match["data_points"][0])

    # count_up 的每一行独立校准：divideBy/decimals 格式化后的显示值也要能匹配
    # （真实场景："$1.5 million" 格式化成 value=1500000, divideBy=1000000,
    # decimals=1 -> 转写里 ASR 识别出的词是"1.5"）。
    count_up_words = [
        {"word": "covers", "start": 11.0, "end": 11.3},
        {"word": "you", "start": 11.3, "end": 11.4},
        {"word": "for", "start": 11.4, "end": 11.5},
        {"word": "1.5", "start": 11.6, "end": 12.0},
        {"word": "million,", "start": 12.0, "end": 12.5},
    ]
    raw_count_up = {"data_points": [
        {"visual": "count_up", "title": "Coverage", "rows": [
            {"label": "Coverage", "seconds": 8.0, "value": 1500000, "divideBy": 1000000, "decimals": 1},
        ]},
    ]}
    _ground_data_point_seconds(raw_count_up, count_up_words)
    check("count_up 行的 seconds 校准到格式化后的显示值('1.5')对应的词",
          raw_count_up["data_points"][0]["rows"][0]["seconds"] == 11.6, raw_count_up["data_points"][0])

    # 8x 秒的窗口之外的匹配不算数——不能把风马牛不相及的同名数字乱配对
    far_words = [{"word": "30", "start": 200.0, "end": 200.3}]
    grounded_far = _find_grounded_seconds(4.7, _number_candidates(30), far_words)
    check("超出 8 秒匹配窗口的候选词不会被采用", grounded_far is None, grounded_far)

    # 9. Fix C3：合并短暂的 dominant 抖动。两段 workflow 之间只隔了 10 帧
    # （确认过的真实场景：scenes 里 206->216 的鼓包），卡片不该长回 Dominant
    # 又立刻缩回去——应该桥接成一段连续的 workflow。
    jitter_schedule = _workflow_mode_schedule(
        [(50, 200, 960), (220, 400, 960)], duration_frames=500,
    )
    check("短间隙(10 帧 < 45 帧)被桥接：schedule 里没有 200/210 附近的 dominant 条目",
          not any(e["mode"] == "dominant" and 190 <= e["frame"] <= 220 for e in jitter_schedule),
          jitter_schedule)
    check("桥接后卡片从 40 帧连续 workflow 到 400 帧（只有开头/结尾两次真正的模式切换）",
          [e["mode"] for e in jitter_schedule] == ["dominant", "workflow", "dominant"], jitter_schedule)

    # 对照组：间隙足够长（超过 45 帧）时不桥接，这是真实的、需要卡片长回来的空档。
    no_jitter_schedule = _workflow_mode_schedule(
        [(50, 200, 960), (260, 400, 960)], duration_frames=500,
    )
    check("间隙足够长(>=45 帧)时正常长回 Dominant，不桥接",
          any(e["mode"] == "dominant" and e["frame"] == 200 for e in no_jitter_schedule),
          no_jitter_schedule)

    # 10. Fix D1：接管时长上限（8s 硬上限 + 内容结束后 2s 停留，取更早者）。
    # 确认过的真实生产 bug（job_e44166eb8c38）：WARNING 接管从 771 帧一路
    # 延伸到片尾(1526)，说话人被隐藏 25.2s 且再也没有恢复；接管里唯一的
    # 图形(gauge)早就结束了，后面 12.3s 是纯黑背景配一个静止图标。这里用
    # 同样形状的真实数值复现：章节起点 700 帧、gauge 在 27.0s(frame810)
    # 说出、总时长 50s(1500 帧，章节是最后一章，natural_end=1500)。
    plan_d1 = _to_frame_plan({
        "chapters": [{"at_seconds": 700 / FPS, "label": "WARNING", "takeover": True, "dark": True, "warn": True}],
        "data_points": [
            {"visual": "gauge", "seconds": 27.0, "title": "G", "leftLabel": "A", "rightLabel": "B", "value": 0.8},
        ],
    }, duration=50.0)
    check("接管跨度被裁到 8s 硬上限(700+240=940)，不是章节自然结束(1500)",
          len(plan_d1["sections"]) == 1 and plan_d1["sections"][0]["toFrame"] == 940,
          plan_d1["sections"])

    # 11. Fix D2：全片隐藏时长预算(30%)——两个接管都被 D1 裁过之后，加起来
    # 仍然超过 30%，摘除时长更长的那个（金句从不摘除）。
    plan_d2 = _to_frame_plan({
        "chapters": [
            {"at_seconds": 0, "label": "A", "takeover": True, "dark": True},
            {"at_seconds": 500 / FPS, "label": "B", "takeover": True, "dark": True},
        ],
        "data_points": [
            {"visual": "gauge", "seconds": 10.0, "title": "GA", "leftLabel": "X", "rightLabel": "Y", "value": 0.5},
            {"visual": "gauge", "seconds": 18.5, "title": "GB", "leftLabel": "X", "rightLabel": "Y", "value": 0.5},
        ],
    }, duration=1400 / FPS)
    check("总隐藏时长超预算时，摘除时长更长的接管('A', 240 帧)，只剩'B'(225 帧)",
          len(plan_d2["sections"]) == 1 and plan_d2["sections"][0]["title"] == "B"
          and plan_d2["sections"][0]["fromFrame"] == 500 and plan_d2["sections"][0]["toFrame"] == 725,
          plan_d2["sections"])
    check("摘除接管后，对应的 sentinel workflow_range 也一并移除（mode_schedule 里章节 A 的时间段不再隐藏卡片）",
          not any(m["mode"] == "workflow" and m.get("contentWidth", 0) >= 10_000_000 and m["frame"] < 250
                  for m in plan_d2["mode_schedule"]),
          plan_d2["mode_schedule"])

    # 11b. 回归测试——真实用户反馈("what happened to the timeline animation
    # we had?")：真实一跑(job_88b957f807b9/job_0aaef74e8865，MrBeast 视频)
    # 里，一段多阶段时间线("从立项到上线要五个月")占了全片 77% 的时长，超过
    # 30% 隐藏预算后被 D2 整段摘除——时间线的可视内容(sec["timeline"])是
    # 焊死在这个 section 对象本身上的，不像 gauge/data_card 那样活在独立
    # 列表里"删了接管、图形还在别处正常显示"，整段摘除等于这 18 秒里除了
    # 字幕什么都不剩。用 30s 的视频复现同样形状(时间线占比 77%)，验证时间线
    # 被裁短保留、不是被摘除。
    #
    # Fix D5（2026-07-16）：这条测试原本断言裁到固定的 8s 硬上限
    # （_TAKEOVER_HARD_CAP_FRAMES）——但真实生产复测（MrBeast backtest，
    # job_95e1e08b0995，23.6s 视频）发现固定 8s 本身就已经超过 26.7s 以内
    # 任何视频总时长的 30%，导致"先裁 8s、再检查预算、仍然超预算、直接整段
    # 摘除"——时间线在那条真实视频的全部 4 次规划里都被摘除，从未播出过。
    # 现在改成裁到预算实际允许的长度（这条测试的 900 帧/30% 预算 = 270 帧，
    # 比固定 8s(240 帧)更宽松，因为这条视频的总时长本身就比 8s 富余不少）。
    plan_timeline_budget = _to_frame_plan({
        "chapters": [
            {"at_seconds": 0, "label": "PROCESS", "takeover": True, "dark": True},
            {"at_seconds": 540 / FPS, "label": "COST"},
        ],
        "process_timeline": {
            "chapter_label": "PROCESS", "heading": "FROM IDEA TO UPLOAD",
            "stages": [
                {"label": "IDEATION", "seconds": 3.0, "prefix": "", "target": 2, "unit": "MONTHS"},
                {"label": "PRODUCTION", "seconds": 10.0, "prefix": "", "target": 4, "unit": "MONTHS"},
            ],
        },
        "data_points": [],
    }, duration=900 / FPS)
    check("超预算的时间线接管被裁到预算允许的实际长度(270 帧 = 30% of 900)，不是整段摘除——"
          "section 仍然存在且带着 timeline 数据，裁得比固定 8s 硬上限更宽松",
          len(plan_timeline_budget["sections"]) == 1
          and "timeline" in plan_timeline_budget["sections"][0]
          and plan_timeline_budget["sections"][0]["fromFrame"] == 0
          and plan_timeline_budget["sections"][0]["toFrame"] == 270
          and plan_timeline_budget["sections"][0]["toFrame"] > _TAKEOVER_HARD_CAP_FRAMES,
          plan_timeline_budget["sections"])
    check("裁短对应的 sentinel workflow_range 也跟着收窄到实际裁短的长度，说话人在裁掉的那部分不再被挡住",
          not any(m["mode"] == "workflow" and m.get("contentWidth", 0) >= 10_000_000
                  and m["frame"] >= 270 + 30
                  for m in plan_timeline_budget["mode_schedule"]),
          plan_timeline_budget["mode_schedule"])

    # 11c. Fix D5 专项回归——真实 job_95e1e08b0995（MrBeast backtest，23.6s
    # 视频，710 帧）复现：固定 8s 硬上限(240 帧)本身就是 710 帧的 33.8%，
    # 已经超过 30% 预算，"裁到 8s、再检查预算"这条路在这个时长上永远走不通
    # ——不管重规划多少轮，第二轮都会因为"裁无可裁"直接整段摘除。用真实
    # 视频完全一样的时长复现，断言时间线现在被裁到预算实际允许的 213 帧
    # (30% of 710，向下取整)，而不是被摘除；同时确认这个长度比固定 8s 硬
    # 上限更短——证明是"按预算算出来的"，不是巧合等于某个别的常量。
    plan_short_video_timeline = _to_frame_plan({
        "chapters": [
            {"at_seconds": 0, "label": "TIMELINE", "takeover": True, "dark": True},
            {"at_seconds": 591 / FPS, "label": "COST"},
        ],
        "process_timeline": {
            "chapter_label": "TIMELINE", "heading": "FROM IDEA TO UPLOAD",
            "stages": [
                {"label": "IDEA", "seconds": 6.0, "prefix": "", "target": 2, "unit": "MO"},
                {"label": "PRODUCTION", "seconds": 12.0, "prefix": "", "target": 3, "unit": "MO"},
            ],
        },
        "data_points": [],
    }, duration=710 / FPS)
    check("MrBeast 真实视频时长(23.6s/710 帧)复现：时间线接管不再被整段摘除，"
          "裁到预算允许的 213 帧(30% of 710)，比固定 8s 硬上限(240 帧)更短",
          len(plan_short_video_timeline["sections"]) == 1
          and "timeline" in plan_short_video_timeline["sections"][0]
          and plan_short_video_timeline["sections"][0]["toFrame"] == 213
          and plan_short_video_timeline["sections"][0]["toFrame"] < _TAKEOVER_HARD_CAP_FRAMES,
          plan_short_video_timeline["sections"])

    # 11d. Fix E1/E2 回归——真实 job_73e873e4f7e1（用户下载 preview(11).mp4 后
    # 逐帧检查抓到的两个问题）：
    #   E1: process_timeline 节点间隔从 30 帧收紧变成"还没长完线就开始长下
    #       一段"，用户原话"everything just appears and disappears too fast"。
    #   E2: 一张 dataCard 跟一张 topicCard 完全同坑位(x,y)、时间又重叠，
    #       topicCard 画在后面(盖住 dataCard)，"Total Duration: 5 months"这
    #       张卡整个存活期间从没被看到过——不是视觉上乱，是内容彻底不可见。
    plan_e1 = _to_frame_plan({
        "chapters": [
            {"at_seconds": 0, "label": "PLAN"},
            {"at_seconds": 5.4, "label": "STEPS", "takeover": True},
            {"at_seconds": 9.5, "label": "TOTAL"},
        ],
        "process_timeline": {
            "chapter_label": "STEPS", "heading": "PIPELINE",
            "stages": [
                {"label": "IDEA", "seconds": 6.4, "target": 4, "unit": "WEEKS", "prefix": "~"},
                {"label": "FILMING", "seconds": 6.8, "target": 2, "unit": "WEEKS", "prefix": ""},
                {"label": "EDITING", "seconds": 7.2, "target": 3, "unit": "WEEKS", "prefix": ""},
            ],
        },
    }, duration=17.0)
    e1_nodes = plan_e1["sections"][0]["timeline"]["nodes"]
    e1_gaps = [e1_nodes[i]["revealFrame"] - e1_nodes[i - 1]["revealFrame"] for i in range(1, len(e1_nodes))]
    check("E1: 时间线相邻节点间隔 >= 60 帧(2s)，不是被压缩到 30 帧",
          all(g >= 60 for g in e1_gaps), e1_gaps)

    dc = [{"title": "From Start to Finish", "x": 60, "y": 1170, "width": 960,
           "mountFrame": 235, "endFrame": 335, "rows": []}]
    tc = [{"headline": "We work 3-4 months in advance", "x": 60, "y": 1170, "width": 960,
           "mountFrame": 120, "endFrame": 282, "sub": "x"}]
    _resolve_same_slot_overlaps(dc, [], [], [], [], [], [], tc)
    e2_overlap = dc[0]["mountFrame"] < tc[0]["endFrame"] and tc[0]["mountFrame"] < dc[0]["endFrame"]
    check("E2: 同坑位(x,y)且时间重叠的 dataCard/topicCard 被拆开——"
          "dataCard 顺延到 topicCard 退场之后，不再被完全盖住",
          not e2_overlap and dc[0]["mountFrame"] > tc[0]["endFrame"], (dc, tc))
    check("E2: dataCard 自己的展示时长(100 帧)顺延后保持不变，只是平移，不是被压缩",
          dc[0]["endFrame"] - dc[0]["mountFrame"] == 100, dc)

    # 12. Fix E：关键词校准入场+收尾（不只是数字）。合成一段跟真实场景同形状
    # 的转写："if your policy lapses...which could affect both your coverage
    # and your rate."——LLM 自己估的 seconds 落在句子中间，用 keyword/
    # end_keyword 校准到真正的触发词/收尾词。
    risk_words = [
        {"word": "if", "start": 25.3, "end": 25.5},
        {"word": "your", "start": 25.5, "end": 25.7},
        {"word": "policy", "start": 25.7, "end": 26.0},
        {"word": "lapses,", "start": 26.0, "end": 26.4},
        {"word": "you'd", "start": 26.6, "end": 26.8},
        {"word": "have", "start": 26.8, "end": 27.0},
        {"word": "to", "start": 27.0, "end": 27.1},
        {"word": "go", "start": 27.1, "end": 27.3},
        {"word": "through", "start": 27.3, "end": 27.6},
        {"word": "underwriting", "start": 27.6, "end": 28.2},
        {"word": "again", "start": 28.2, "end": 28.5},
        {"word": "which", "start": 28.8, "end": 29.0},
        {"word": "could", "start": 29.0, "end": 29.2},
        {"word": "affect", "start": 29.2, "end": 29.5},
        {"word": "both", "start": 29.5, "end": 29.7},
        {"word": "your", "start": 29.7, "end": 29.9},
        {"word": "coverage", "start": 29.9, "end": 30.3},
        {"word": "and", "start": 30.3, "end": 30.4},
        {"word": "your", "start": 30.4, "end": 30.5},
        {"word": "rate.", "start": 30.5, "end": 30.9},
    ]

    # 12a. _find_grounded_word 直接测试：数字谓词和关键词谓词都返回完整的
    # 词 dict（含 start/end），不只是起始时间。
    num_match = _find_grounded_word(4.7, lambda w: w.strip(",.") == "30", [{"word": "30", "start": 7.5, "end": 7.7}])
    check("_find_grounded_word 数字谓词返回完整词 dict",
          num_match is not None and num_match["start"] == 7.5 and num_match["end"] == 7.7, num_match)
    kw_match = _find_grounded_word(29.0, _keyword_matches("policy", take="first"), risk_words)
    check("_find_grounded_word 关键词谓词命中 'policy'，返回完整词 dict",
          kw_match is not None and kw_match["word"] == "policy" and kw_match["start"] == 25.7, kw_match)

    # 12b. gauge：keyword 校准入场（不是 LLM 自己估的句中位置），end_keyword
    # 校准收尾（不是固定 160 帧动画时长）。
    raw_gauge = {"data_points": [
        {"visual": "gauge", "seconds": 29.0, "title": "Lapse Risk", "leftLabel": "SAFE", "rightLabel": "AT RISK",
         "value": 0.9, "keyword": "policy", "end_keyword": "rate"},
    ]}
    _ground_data_point_seconds(raw_gauge, risk_words)
    check("gauge 的 seconds 校准到 'policy' 的起始时间(25.7s)，不是 LLM 猜的句中位置(29.0s)",
          raw_gauge["data_points"][0]["seconds"] == 25.7, raw_gauge["data_points"][0])
    gauge_plan = _to_frame_plan(raw_gauge, duration=40.0)
    expected_gauge_mount = round(25.7 * FPS) - MOUNT_LEAD_FRAMES
    expected_gauge_end = round(30.9 * FPS) + _KEYWORD_EXIT_HOLD_FRAMES
    check("gauge mountFrame 基于 'policy' 的起始时间，不是句中猜测的位置",
          gauge_plan["gauges"][0]["mountFrame"] == expected_gauge_mount, gauge_plan["gauges"])
    check("gauge endFrame 基于 'rate' 的结束时间 + 收尾缓冲，不是固定的 160 帧动画时长",
          gauge_plan["gauges"][0]["endFrame"] == expected_gauge_end, gauge_plan["gauges"])

    # 12c. count_up 行：end_keyword 校准卡片的自然退场，而不是"最后一行说完
    # 再固定停留 90 帧"。
    premium_words = [
        {"word": "premium", "start": 14.7, "end": 15.0},
        {"word": "is", "start": 15.0, "end": 15.1},
        {"word": "$8,400.00.", "start": 15.1, "end": 15.6},
    ]
    raw_count_up_ek = {"data_points": [
        {"visual": "count_up", "title": "Plan", "rows": [
            {"label": "Premium", "seconds": 12.0, "value": 8400, "prefix": "$", "decimals": 0,
             "end_keyword": "8,400"},
        ]},
    ]}
    _ground_data_point_seconds(raw_count_up_ek, premium_words)
    plan_ek = _to_frame_plan(raw_count_up_ek, duration=30.0)
    card = plan_ek["data_cards"][0]
    expected_card_end = round(15.6 * FPS) + _KEYWORD_EXIT_HOLD_FRAMES
    check("count_up 卡片的 endFrame 基于 '$8,400.00.' 的结束时间 + 收尾缓冲，不是固定 90 帧停留",
          card["endFrame"] == expected_card_end, card)

    # 12d. quote：不需要新的 LLM 字段——直接用 text 自己的第一个词/最后一个词
    # 校准入场/收尾。
    quote_words = [
        {"word": "Renewing", "start": 23.2, "end": 23.5},
        {"word": "on", "start": 23.5, "end": 23.6},
        {"word": "time", "start": 23.6, "end": 23.8},
        {"word": "really", "start": 23.8, "end": 24.1},
        {"word": "matters.", "start": 24.1, "end": 24.6},
    ]
    raw_quote = {"data_points": [
        {"visual": "quote", "seconds": 20.0, "text": "Renewing on time really matters."},
    ]}
    _ground_data_point_seconds(raw_quote, quote_words)
    check("quote 的 seconds 校准到自己文本第一个词'Renewing'的起始时间(23.2s)，不是 LLM 猜的 20.0s",
          raw_quote["data_points"][0]["seconds"] == 23.2, raw_quote["data_points"][0])
    quote_plan = _to_frame_plan(raw_quote, duration=30.0)
    expected_quote_mount = round(23.2 * FPS) - MOUNT_LEAD_FRAMES
    expected_quote_end = round(24.6 * FPS) + _KEYWORD_EXIT_HOLD_FRAMES
    check("quote mountFrame 基于文本自己第一个词的起始时间",
          quote_plan["quotes"][0]["mountFrame"] == expected_quote_mount, quote_plan["quotes"])
    check("quote endFrame 基于文本自己最后一个词'matters.'的结束时间 + 收尾缓冲，不是固定 140 帧",
          quote_plan["quotes"][0]["endFrame"] == expected_quote_end, quote_plan["quotes"])

    # 12e. 向后兼容：没有 keyword/end_keyword 时行为跟 Fix E 之前完全一样
    # （回退到固定时长），不会因为新字段缺失而报错或改变既有输出。
    raw_no_keyword = {"data_points": [
        {"visual": "gauge", "seconds": 5.0, "title": "G", "leftLabel": "A", "rightLabel": "B", "value": 0.5},
    ]}
    _ground_data_point_seconds(raw_no_keyword, risk_words)  # should be a no-op for this dp (no keyword field)
    plan_no_kw = _to_frame_plan(raw_no_keyword, duration=20.0)
    fallback_mount = round(5.0 * FPS) - MOUNT_LEAD_FRAMES
    check("无 keyword/end_keyword 时，gauge 完全回退到 Fix E 之前的固定时长行为",
          plan_no_kw["gauges"][0]["mountFrame"] == fallback_mount
          and plan_no_kw["gauges"][0]["endFrame"] == fallback_mount + 70 + 90,  # GAUGE_ANIMATION_FRAMES+HOLD
          plan_no_kw["gauges"])

    # 12f. 回归测试——真实一跑(job_b943ce1d3606)抓到的 bug：count_up 卡片
    # 如果只有*一行*拿到 end_keyword 校准（且校准得比另一行自己的出场时间更
    # 早），旧逻辑会直接拿这一行的收尾时间当作整张卡片的 endFrame，结果卡片
    # 在另一行（没校准、出场更晚）自己的数字还没滚动之前就已经被判定该收起
    # 了。Premium 行（无 end_keyword，自己在 16.0s 才出场)不能被 Coverage 行
    # （有 end_keyword，收尾校准到 11.5s）的早收尾顶掉。
    starvation_words = [{"word": "million", "start": 11.3, "end": 11.5}]
    raw_starvation = {"data_points": [
        {"visual": "count_up", "title": "Plan", "rows": [
            {"label": "Coverage", "seconds": 11.0, "value": 1.5, "prefix": "$", "decimals": 1,
             "end_keyword": "million"},
            {"label": "Premium", "seconds": 16.0, "value": 8400, "prefix": "$", "decimals": 0},
        ]},
    ]}
    _ground_data_point_seconds(raw_starvation, starvation_words)
    plan_starvation = _to_frame_plan(raw_starvation, duration=30.0)
    card_s = plan_starvation["data_cards"][0]
    premium_row = next(r for r in card_s["rows"] if r["label"] == "Premium")
    premium_reveal_frame = card_s["mountFrame"] + premium_row["mountOffset"]
    check("没校准的 Premium 行不会被 Coverage 行更早的校准收尾顶掉——卡片"
          "endFrame 必须晚于 Premium 自己的出场帧",
          card_s["endFrame"] > premium_reveal_frame, card_s)
    check("Premium 行出场之后卡片至少停留完整的滚动动画+收尾缓冲(40+30帧)",
          card_s["endFrame"] >= premium_reveal_frame + _COUNT_UP_ROW_ANIM_FRAMES + _KEYWORD_EXIT_HOLD_FRAMES,
          card_s)

    # 13. 回归测试——真实用户反馈("appeared it nicely, but disappearing too
    # fast")：countdown/calendar 这类紧挨着的两段内容，前一段(countdown)自己
    # 关键词校准过的收尾时间已经是跟着台词走的真实值了，但堆叠系统一直是
    # 无脑地用"下一段(calendar)想上场的时间 - 缓冲"去砍前一段，完全不管前一段
    # 本来想播到什么时候。两边现在都是台词校准来的真实时间，冲突时应该优先
    # 让前一段播完，代价是让下一段稍微晚一点上场。
    countdown_dp = {"visual": "countdown", "seconds": 6.0, "value": 30, "unitLabel": "DAYS", "label": "RENEWAL"}
    countdown_dp["_grounded_end_seconds"] = 8.0  # 模拟 end_keyword="days" 校准到的收尾词结束时间
    calendar_dp = {"visual": "calendar", "seconds": 7.0, "year": 2026, "month": 7, "targetDay": 28,
                   "eventLabel": "Renewal"}
    handoff_plan = _to_frame_plan({"chapters": [], "data_points": [countdown_dp, calendar_dp]}, duration=30.0)
    cd = handoff_plan["countdowns"][0]
    cal = handoff_plan["calendar_events"][0]
    expected_cd_end = round(8.0 * FPS) + _KEYWORD_EXIT_HOLD_FRAMES
    check("countdown 播完自己校准过的收尾时间，不被 calendar 的上场时间砍短",
          cd["endFrame"] == expected_cd_end, cd)
    check("calendar 改为晚一点上场（等 countdown 播完 + 退场缓冲），而不是把 countdown 砍短",
          cal["mountFrame"] == cd["endFrame"] + _STACK_EXIT_BUFFER_FRAMES, {"countdown": cd, "calendar": cal})
    check("calendar 自己的展示时长没有因为被推迟而被压缩",
          cal["endFrame"] - cal["mountFrame"] >= 100, cal)

    # 冲突太大(超过 _STACK_HANDOFF_MAX_DELAY_FRAMES)时放弃推迟，退回旧的砍短
    # 行为——不能让一次异常的校准结果把下一段拖很久很久都不上场。
    from whatsapp_mvp.content_planner import _STACK_HANDOFF_MAX_DELAY_FRAMES
    countdown_far = {"visual": "countdown", "seconds": 6.0, "value": 30, "unitLabel": "DAYS", "label": "RENEWAL"}
    countdown_far["_grounded_end_seconds"] = 6.0 + (_STACK_HANDOFF_MAX_DELAY_FRAMES + 60) / FPS
    calendar_soon = {"visual": "calendar", "seconds": 7.0, "year": 2026, "month": 7, "targetDay": 28,
                     "eventLabel": "Renewal"}
    far_plan = _to_frame_plan({"chapters": [], "data_points": [countdown_far, calendar_soon]}, duration=60.0)
    cd_far = far_plan["countdowns"][0]
    cal_soon = far_plan["calendar_events"][0]
    grounded_target_frame = round(countdown_far["_grounded_end_seconds"] * FPS)
    check("冲突超过上限时不推迟下一段太久——calendar 没有被拖到接近 countdown 完整校准目标那么晚",
          cal_soon["mountFrame"] < grounded_target_frame - _STACK_HANDOFF_MAX_DELAY_FRAMES,
          {"countdown": cd_far, "calendar": cal_soon, "grounded_target_frame": grounded_target_frame})
    check("这种情况下 countdown 被砍短（回退到旧行为），而不是播完整个校准目标",
          cd_far["endFrame"] < grounded_target_frame - _STACK_HANDOFF_MAX_DELAY_FRAMES, cd_far)

    # 14. 回归测试——真实用户反馈("what happened to the calendar, what
    # happened to the numbers")：真实一跑(job_1237c9c59bc0)里 LLM 没规划出
    # 日历/数据卡，密度下限补规划(REPLAN，产出标记 _gap_fill 的数据点)在 11s
    # 后才出现，此时按旧逻辑它跟已经孤零零晾在那的 countdown 属于同一堆叠
    # (同章节、在 8s 的 JOIN WINDOW 内、高度也够)——两者被绑定共享同一个退场
    # 时间，倒计时活活多播了 12 秒，观众看到的是一张过时的"30 DAYS"卡片陪着
    # 一条毫不相关的"$8,400 保费"文字同时挂在画面上。补规划产出的内容必须
    # 强制开新的一段，让 countdown 在自己该退场的时候就退场。
    countdown_alone = {"visual": "countdown", "seconds": 6.0, "value": 30, "unitLabel": "DAYS", "label": "RENEWAL"}
    gap_fill_card = {"visual": "topic_card", "seconds": 10.0, "headline": "the premium is $8,400", "icon": "sparkle",
                      "_gap_fill": True}
    isolation_plan = _to_frame_plan({"chapters": [], "data_points": [countdown_alone, gap_fill_card]}, duration=30.0)
    cd_alone = isolation_plan["countdowns"][0]
    gap_card = isolation_plan["topic_cards"][0]
    check("孤零零的 countdown 在自己的自然收尾时间退场，不会被后面才出现的补规划内容拖住",
          cd_alone["endFrame"] < round(10.0 * FPS), cd_alone)
    check("_gap_fill 标记的 topic_card 强制开一段新的堆叠(跟 countdown 同一条车道 y，不是拼在它下面)，"
          "而不是悄悄拼进 countdown 还开着的那一段",
          gap_card["y"] == cd_alone["y"] and gap_card["mountFrame"] >= cd_alone["endFrame"],
          {"countdown": cd_alone, "gap_card": gap_card})

    # 16. 规划质量标准循环的标准函数（_plan_quality_failures）——纯函数、
    # 确定性，criterion loop 每轮规划后跑，全部通过才提前退出。真实用户
    # 要求："ALL VIDEOS SENT WILL HAVE A PLANNING TOWARDS HOW THE ANIMATIONS
    # WILL GO... HAVE A CRITERION ON THE FEATURE SO THAT THE BOT WOULD GO
    # THROUGH IT EVERYTIME"。
    from whatsapp_mvp.content_planner import _plan_quality_failures, _MIN_DURATION_FOR_VISUALS_S

    # 16a. 确认过的真实生产失败（job_1dd6e4748b31，Dickson 视频）：35s 口播
    # 规划出 0 个图形——必须被标准 1（零图形）+ 标准 2（长空档）双双抓住。
    empty_plan_35s = _to_frame_plan({"chapters": [{"at_seconds": 0, "label": "A"}]}, duration=35.0)
    f_empty = _plan_quality_failures({"data_points": []}, empty_plan_35s, 35.0)
    check("35s 视频规划出 0 个图形被质量标准抓住（零图形 + 长空档）",
          len(f_empty) >= 2 and any("ZERO visual" in f for f in f_empty)
          and any("No visual event" in f for f in f_empty), f_empty)

    # 16b. 覆盖良好的计划全部通过（20s 视频 + 一张 5s 处的 topic_card，
    # 头尾豁免区之外的中段全被盖住）。
    good_raw = {"data_points": [{"visual": "topic_card", "seconds": 5.0, "headline": "a real point", "icon": "check"}]}
    good_plan = _to_frame_plan({"chapters": [], **good_raw}, duration=20.0)
    check("覆盖良好的计划质量标准全部通过（返回空失败列表）",
          _plan_quality_failures(good_raw, good_plan, 20.0) == [],
          _plan_quality_failures(good_raw, good_plan, 20.0))

    # 16c. 0 值卡被标准 3 抓住（value=1.5 又配 divideBy=1000000 → 显示 $0.0M，
    # 真实生产 bug 的复现形状）。
    zero_raw = {"data_points": [
        {"visual": "topic_card", "seconds": 5.0, "headline": "x", "icon": "check"},
        {"visual": "count_up", "title": "Coverage", "rows": [
            {"label": "C", "seconds": 6.0, "value": 1.5, "divideBy": 1000000, "decimals": 1}]},
    ]}
    zero_plan = _to_frame_plan({"chapters": [], **zero_raw}, duration=20.0)
    f_zero = _plan_quality_failures(zero_raw, zero_plan, 20.0)
    check("显示为 0 的数字卡被质量标准抓住", any("ZERO after" in f for f in f_zero), f_zero)

    # 16d. 短视频（<15s）豁免零图形标准（intro/outro 已够撑画面），空档检查
    # 也因为短视频豁免不触发——空计划应该整体通过。
    short_plan = _to_frame_plan({"chapters": []}, duration=12.0)
    check("短视频(<15s)空计划豁免质量标准",
          _plan_quality_failures({"data_points": []}, short_plan, 12.0) == []
          and 12.0 < _MIN_DURATION_FOR_VISUALS_S,
          _plan_quality_failures({"data_points": []}, short_plan, 12.0))

    # 16e. Fix C41 —— job_452ef6c48100 真实用户反馈：转写里明说了具体金额
    # （"one and a half million"/"$8,400"），但某一轮重规划的输出只有
    # topic_card，一张数字卡都没有——标准 4 必须抓住这种情况。
    dollar_segments = [
        {"start": 4.6, "end": 11.2, "text": "Your policy is coming up for renewal in 30 days"},
        {"start": 11.2, "end": 17.4, "text": "Your current plan covers you for one and a half million"
                                              " and your annual premium is"},
        {"start": 17.4, "end": 23.5, "text": "$8,400. I've put the full breakdown in this video."},
    ]
    topic_card_only_raw = {"data_points": [
        {"visual": "topic_card", "seconds": 5.0, "headline": "Quick policy reminder", "icon": "check"},
        {"visual": "topic_card", "seconds": 18.0, "headline": "Full breakdown in this video", "icon": "sparkle"},
    ]}
    topic_card_only_plan = _to_frame_plan({"chapters": [], **topic_card_only_raw}, duration=30.0)
    f_dollar = _plan_quality_failures(topic_card_only_raw, topic_card_only_plan, 30.0, dollar_segments)
    check("转写里说了具体金额但计划里一张数字卡都没有时被标准 4 抓住",
          any("$8,400" in f or "explicitly says" in f for f in f_dollar), f_dollar)

    with_count_up_raw = {"data_points": [
        {"visual": "count_up", "title": "Your Plan", "rows": [
            {"label": "Coverage", "seconds": 12.0, "value": 1500000, "divideBy": 1000000, "decimals": 1,
             "prefix": "$", "unit": "M"},
        ]},
    ]}
    with_count_up_plan = _to_frame_plan({"chapters": [], **with_count_up_raw}, duration=30.0)
    f_with_card = _plan_quality_failures(with_count_up_raw, with_count_up_plan, 30.0, dollar_segments)
    check("转写里说了金额、计划里确实有对应数字卡时不误报标准 4",
          not any("explicitly says" in f for f in f_with_card), f_with_card)

    f_no_segments = _plan_quality_failures(topic_card_only_raw, topic_card_only_plan, 30.0)
    check("不传 segments 时标准 4 完全不触发（向后兼容，不影响没有转写上下文的既有调用）",
          not any("explicitly says" in f for f in f_no_segments), f_no_segments)

    # 16f2. 标准 5（新增，2026-07-21 真实复现）——同一支 job_452ef6c48100，
    # 同一份转写只说过 "$8,400"/"one and a half million"，但某几轮重规划把
    # count_up 行编造成了 "$6,300"/"$1.1M"（两个数字转写里都不存在，不是
    # retake 残留）。标准 4 对这种情况完全没有覆盖，因为它只查"有没有数字
    # 卡"，卡确实有，只是数字是编的。
    hallucinated_raw = {"data_points": [
        {"visual": "count_up", "title": "Coverage", "rows": [
            {"label": "Coverage", "seconds": 12.0, "value": 1100000, "divideBy": 1000000,
             "decimals": 1, "prefix": "$", "unit": "M"}]},
        {"visual": "count_up", "title": "Premium", "rows": [
            {"label": "Premium", "seconds": 18.0, "value": 6300, "divideBy": 1, "decimals": 0,
             "prefix": "$"}]},
    ]}
    hallucinated_plan = _to_frame_plan({"chapters": [], **hallucinated_raw}, duration=30.0)
    f_hallucinated = _plan_quality_failures(hallucinated_raw, hallucinated_plan, 30.0, dollar_segments)
    check("编造出转写里不存在的数字（$6,300/$1.1M）被标准 5 抓住",
          any("do not match ANY number" in f for f in f_hallucinated), f_hallucinated)

    correct_raw = {"data_points": [
        {"visual": "count_up", "title": "Coverage", "rows": [
            {"label": "Coverage", "seconds": 12.0, "value": 1500000, "divideBy": 1000000,
             "decimals": 1, "prefix": "$", "unit": "M"}]},
        {"visual": "count_up", "title": "Premium", "rows": [
            {"label": "Premium", "seconds": 18.0, "value": 8400, "divideBy": 1, "decimals": 0,
             "prefix": "$"}]},
    ]}
    correct_plan = _to_frame_plan({"chapters": [], **correct_raw}, duration=30.0)
    f_correct = _plan_quality_failures(correct_raw, correct_plan, 30.0, dollar_segments)
    check("转写里实际说的数字（$8,400/$1.5M，含词面大数）不被标准 5 误报",
          not any("do not match ANY number" in f for f in f_correct), f_correct)

    # 16f3. 标准 5（真实复现，job_5b0ec0b914ee，2026-07-27）——apply_style 自己
    # 内部为字幕做的第二次转写（增强链之后重新跑一遍 faster-whisper，同一段
    # 音频）把 "$1.5 million" 转写成了没有 $ 号的 "1.5 million"（同一模型，纯
    # ASR 输出格式运行间抖动）。LLM 规划的 Coverage=1500000 完全正确，但当时
    # 的提取逻辑只认 "$<数字>" 和纯词面数字（"one and a half million"）两种
    # 形式，"<数字> million" 这种数字紧跟量级词、没有货币符号的第三种口语
    # 形式两边都没覆盖，导致正确答案被反复误判为"编造"，烧光预算触发降级。
    bare_scale_segments = [
        {"start": 11.2, "end": 17.4, "text": "Your current plan covers you for 1.5 million "
                                              "and your annual premium is $8,400."},
    ]
    f_bare_scale = _plan_quality_failures(correct_raw, correct_plan, 30.0, bare_scale_segments)
    check("ASR 把 '$1.5 million' 转写成没有 $ 号的 '1.5 million' 时，正确的 Coverage 卡不被标准 5 误报",
          not any("do not match ANY number" in f for f in f_bare_scale), f_bare_scale)

    # 16f1. 标准 6（新增，2026-07-23，真实 WhatsApp 交付的预览复现）——真实
    # segment 数据（job_452ef6c48100 的 _op_nofiller_transcript.json 原文，
    # "30 days on the 28th of July" 跟 "renewal in" 分属两个 ASR 分段，验证过
    # 不是简化过的测试夹具）：转写里 "$8,400" 和 "one and a half million" 同一
    # 句话都说了，规划却只留下 Premium 卡，Coverage 的 $1.5M 消失——标准 4/5
    # 都不会抓到这种情况（各自的检查角度不覆盖"漏了另一个数字"）。
    real_segments = [
        {"start": 0.21, "end": 7.99, "text": "Hi there, it's David from Pacific life quick "
                                              "reminder your policy is coming up for renewal in"},
        {"start": 7.99, "end": 11.23, "text": "30 days on the 28th of July"},
        {"start": 11.23, "end": 17.43, "text": "Your current plan covers you for one and a half "
                                                "million and your annual premium is"},
        {"start": 17.43, "end": 23.54, "text": "$8,400 I've put the full breakdown in this video. "
                                                "So you have everything in one place"},
    ]
    premium_only_raw = {"data_points": [
        {"visual": "count_up", "title": "Premium", "rows": [
            {"label": "Premium", "seconds": 18.0, "value": 8400, "divideBy": 1, "decimals": 0,
             "prefix": "$"}]},
        {"visual": "calendar", "seconds": 9.0, "year": 2026, "month": 7, "targetDay": 28,
         "eventLabel": "RENEWAL"},
    ]}
    premium_only_plan = _to_frame_plan({"chapters": [], **premium_only_raw}, duration=30.0)
    f_premium_only = _plan_quality_failures(premium_only_raw, premium_only_plan, 30.0, real_segments)
    check("Coverage 的 $1.5M 有说但没卡时被标准 6 抓住（标准 4/5 都不会触发）",
          any("1.5e+06" in f or "1.5" in f for f in f_premium_only)
          and not any("explicitly says" in f for f in f_premium_only)
          and not any("do not match ANY number" in f for f in f_premium_only),
          f_premium_only)

    both_covered_raw = {"data_points": [
        {"visual": "count_up", "title": "Your Coverage & Premium", "rows": [
            {"label": "Coverage", "seconds": 12.0, "value": 1500000, "divideBy": 1000000,
             "decimals": 1, "prefix": "$", "unit": "M"},
            {"label": "Premium", "seconds": 18.0, "value": 8400, "divideBy": 1, "decimals": 0,
             "prefix": "$"},
        ]},
        {"visual": "calendar", "seconds": 9.0, "year": 2026, "month": 7, "targetDay": 28,
         "eventLabel": "RENEWAL"},
    ]}
    both_covered_plan = _to_frame_plan({"chapters": [], **both_covered_raw}, duration=30.0)
    f_both_covered = _plan_quality_failures(both_covered_raw, both_covered_plan, 30.0, real_segments)
    check("Coverage 和 Premium 都有对应卡片时标准 6 不误报",
          not any("NO matching count_up" in f for f in f_both_covered), f_both_covered)

    # 16f1b. 标准 7（新增，同一次真实复现）——转写里 "30 days" 和 "July" 同一个
    # ASR 分段里共现，规划只产出日历卡（July 28），倒计时("30 DAYS")整个消失。
    calendar_only_raw = {"data_points": [
        {"visual": "calendar", "seconds": 9.0, "year": 2026, "month": 7, "targetDay": 28,
         "eventLabel": "RENEWAL"},
    ]}
    calendar_only_plan = _to_frame_plan({"chapters": [], **calendar_only_raw}, duration=30.0)
    f_calendar_only = _plan_quality_failures(calendar_only_raw, calendar_only_plan, 30.0, real_segments)
    check("倒计时措辞跟月份名同段共现，但只有日历卡没有倒计时卡时被标准 7 抓住",
          any("countdown" in f and "calendar" in f for f in f_calendar_only), f_calendar_only)

    both_countdown_and_calendar_raw = {"data_points": [
        {"visual": "calendar", "seconds": 9.0, "year": 2026, "month": 7, "targetDay": 28,
         "eventLabel": "RENEWAL"},
        {"visual": "countdown", "seconds": 8.0, "value": 30, "unitLabel": "DAYS", "label": "RENEWAL",
         "headline": "Your policy renews in 30 days"},
    ]}
    both_cd_plan = _to_frame_plan({"chapters": [], **both_countdown_and_calendar_raw}, duration=30.0)
    f_both_cd = _plan_quality_failures(both_countdown_and_calendar_raw, both_cd_plan, 30.0, real_segments)
    check("日历卡和倒计时卡都有时标准 7 不误报",
          not any("ZERO countdown cards" in f for f in f_both_cd), f_both_cd)

    unrelated_days_raw = {"data_points": [
        {"visual": "topic_card", "seconds": 5.0, "headline": "I've done this for ten years", "icon": "check"},
    ]}
    unrelated_segments = [
        {"start": 0.0, "end": 5.0, "text": "I've been doing this job for ten years now."},
    ]
    unrelated_plan = _to_frame_plan({"chapters": [], **unrelated_days_raw}, duration=20.0)
    f_unrelated = _plan_quality_failures(unrelated_days_raw, unrelated_plan, 20.0, unrelated_segments)
    check("无关的时长描述句（没有月份名同段共现）不触发标准 7 的误报",
          not any("ZERO countdown cards" in f for f in f_unrelated), f_unrelated)

    # 16f1c. 标准 8（新增，2026-07-23，用户直接反馈"the outro is not there
    # again"）——真实转写全文（job_452ef6c48100 的 _op_nofiller_transcript.json
    # 完整片段列表，不是截断片段）：末段明显在做收尾（"Looking forward to
    # keeping you..."/"Take care."），落在视频最后 1/4 时间段内。
    full_real_segments = [
        {"start": 0.21, "end": 7.99, "text": "Hi there, it's David from Pacific life quick "
                                              "reminder your policy is coming up for renewal in"},
        {"start": 7.99, "end": 11.23, "text": "30 days on the 28th of July"},
        {"start": 11.23, "end": 17.43, "text": "Your current plan covers you for one and a half "
                                                "million and your annual premium is"},
        {"start": 17.43, "end": 23.54, "text": "$8,400 I've put the full breakdown in this video. "
                                                "So you have everything in one place"},
        {"start": 23.54, "end": 29.18, "text": "Renewing on time really matters. If your policy "
                                                "lapses, you'd have to go"},
        {"start": 29.18, "end": 33.74, "text": "through underwriting again, which could affect "
                                                "both your coverage and your rate."},
        {"start": 34.38, "end": 38.96, "text": "If you have any questions, just WhatsApp me "
                                                "directly. If you have any"},
        {"start": 38.96, "end": 44.98, "text": "questions, just WhatsApp me directly or scan the "
                                                "QR code below. I'll get back"},
        {"start": 44.98, "end": 49.0, "text": "you right away. Looking forward to keeping you and "
                                               "your family protected."},
        {"start": 49.46, "end": 49.84, "text": "Take care."},
    ]
    no_outro_raw = {"data_points": [
        {"visual": "count_up", "title": "Your Coverage & Premium", "rows": [
            {"label": "Coverage", "seconds": 12.0, "value": 1500000, "divideBy": 1000000,
             "decimals": 1, "prefix": "$", "unit": "M"},
            {"label": "Premium", "seconds": 18.0, "value": 8400, "divideBy": 1, "decimals": 0,
             "prefix": "$"}]},
    ]}
    no_outro_plan = _to_frame_plan({"chapters": [], **no_outro_raw}, duration=50.0)
    f_no_outro = _plan_quality_failures(no_outro_raw, no_outro_plan, 50.0, full_real_segments)
    check("转写末段明显在收尾但计划没有 outro 时被标准 8 抓住（真实转写数据）",
          any("NO outro" in f for f in f_no_outro), f_no_outro)

    with_outro_raw = {**no_outro_raw, "outro": {
        "kicker": "CONTACT", "headline": "Looking forward to keeping you",
        "subtext": "I'll get back to you right away", "cta_label": "Scan QR",
    }}
    with_outro_plan = _to_frame_plan({"chapters": [], **with_outro_raw}, duration=50.0)
    f_with_outro = _plan_quality_failures(with_outro_raw, with_outro_plan, 50.0, full_real_segments)
    check("outro 已经有 headline 时标准 8 不误报", not any("NO outro" in f for f in f_with_outro), f_with_outro)

    short_no_outro_plan = _to_frame_plan({"chapters": [], **no_outro_raw}, duration=10.0)
    f_short = _plan_quality_failures(no_outro_raw, short_no_outro_plan, 10.0,
                                      [{"start": 0.0, "end": 9.0, "text": "Take care, thanks for watching!"}])
    check("视频短于 outro 门槛(12s)时标准 8 不强求（呼应 pipeline_runner 自己的 duration_frames>=360 门槛）",
          not any("NO outro" in f for f in f_short), f_short)

    early_closing_plan = _to_frame_plan({"chapters": [], **no_outro_raw}, duration=50.0)
    f_early = _plan_quality_failures(
        no_outro_raw, early_closing_plan, 50.0,
        [{"start": 2.0, "end": 6.0, "text": "Feel free to contact me or scan the QR code anytime."},
         {"start": 40.0, "end": 45.0, "text": "So that's the coverage breakdown for this year."}],
    )
    check("收尾措辞出现在视频前段（不是最后 1/4）时标准 8 不误报", not any("NO outro" in f for f in f_early), f_early)

    # 16f. Fix C42 —— process_timeline.chapter_label 跟 chapters[].label 精确
    # 匹配失败时，退回到唯一被标记 takeover 的章节，而不是整段丢弃。
    timeline_chapters = [
        {"label": "Intro", "atFrame": 0, "_takeover": False},
        {"label": "Production Timeline", "atFrame": 300, "_takeover": True},
        {"label": "Outro", "atFrame": 900, "_takeover": False},
    ]
    raw_pt_mismatched_label = {
        "chapter_label": "TIMELINE",  # 跟章节真实的 "Production Timeline" 对不上
        "heading": "FROM IDEA TO UPLOAD",
        "stages": [
            {"label": "IDEA", "seconds": 12.0, "target": 2, "unit": "MONTHS"},
            {"label": "FILMING", "seconds": 20.0, "target": 3, "unit": "WEEKS"},
        ],
    }
    tl = _plan_process_timeline(raw_pt_mismatched_label, timeline_chapters, duration_frames=1200)
    check("chapter_label 精确匹配失败但只有一个 takeover 章节时，退回用它而不是整段丢弃",
          tl is not None and tl["chapter_index"] == 1, tl)
    check("退回匹配后，对应章节确实被标记为 takeover 且无 icon",
          timeline_chapters[1]["_takeover"] is True and timeline_chapters[1]["_icon"] is None,
          timeline_chapters[1])

    # 有 0 个或 >=2 个 takeover 章节时，退回匹配没有唯一目标——保持原有行为，
    # 老实返回 None，不瞎猜。
    no_takeover_chapters = [{"label": "Intro", "atFrame": 0, "_takeover": False}]
    check("没有任何 takeover 章节时，精确匹配失败就老实返回 None（不瞎猜）",
          _plan_process_timeline(raw_pt_mismatched_label, no_takeover_chapters, 1200) is None)
    two_takeover_chapters = [
        {"label": "A", "atFrame": 0, "_takeover": True},
        {"label": "B", "atFrame": 300, "_takeover": True},
    ]
    check("有多个 takeover 章节时无法唯一确定退回目标，老实返回 None（不瞎猜）",
          _plan_process_timeline(raw_pt_mismatched_label, two_takeover_chapters, 1200) is None)

    # 17. Fix C26 回归测试——真实生产复现 job_452ef6c48100，用户截图抓到
    # "$8,400"这一行数字滚动到一半就被卡片收起切掉：一张卡片里两行数字，
    # 一行(Coverage)校准过、说得慢；另一行(Premium)也校准过，但"$8,400"这个
    # 短语说得很快，校准出的"说完这个词"时间点离它自己的出场帧很近——旧逻辑
    # 只用"说完 + 停留缓冲"决定卡片收起时间，完全不管这一行自己的滚动动画
    # (_COUNT_UP_ROW_ANIM_FRAMES=40 帧)有没有时间播完。
    from whatsapp_mvp.content_planner import _MIN_VISUAL_HOLD_FRAMES
    coverage_row = {"label": "COVERAGE", "seconds": 18.0, "value": 1500000, "prefix": "$",
                     "divideBy": 1000000, "decimals": 1, "unit": "M"}
    coverage_row["_grounded_end_seconds"] = 18.6
    premium_row = {"label": "PREMIUM", "seconds": 24.2, "value": 8400, "prefix": "$", "decimals": 0}
    premium_row["_grounded_end_seconds"] = 24.4  # "$8,400" 说得很快——校准收尾离出场只差 0.2s(6 帧)
    count_up_dp = {"visual": "count_up", "title": "Your Coverage", "rows": [coverage_row, premium_row]}
    cutoff_plan = _to_frame_plan({"chapters": [], "data_points": [count_up_dp]}, duration=45.0)
    cutoff_card = cutoff_plan["data_cards"][0]
    premium_out = next(r for r in cutoff_card["rows"] if r["label"] == "PREMIUM")
    premium_row_frame = cutoff_card["mountFrame"] + premium_out["mountOffset"]
    check("说得很快的行(校准收尾离出场很近)，卡片仍然等它自己的滚动动画播完才收起",
          cutoff_card["endFrame"] >= premium_row_frame + _COUNT_UP_ROW_ANIM_FRAMES,
          {"card": cutoff_card, "premium_row_frame": premium_row_frame})

    # 18. Fix C27 回归测试——真实生产复现 job_452ef6c48100，用户反馈"日历
    # 消失得太快"：一个数据点被关键词校准到自己章节快结束时才挂载(这条日历
    # 的"28th of July"是 DEADLINE 章节最后一句话)，章节边界裁剪(_flush_stack
    # 的"不能播到下一个话题里"保护)把它砍到只剩十几帧——比这份代码自己认定
    # 的最短可读时长(_MIN_VISUAL_HOLD_FRAMES=45 帧/1.5s)还短得多。
    tight_chapter_plan = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "DEADLINE"}, {"at_seconds": 11.2, "label": "COVERAGE"}],
        "data_points": [{
            "visual": "calendar", "seconds": 10.6, "year": 2026, "month": 7, "targetDay": 28,
            "eventLabel": "Renewal",
        }],
    }, duration=45.0)
    tight_cal = tight_chapter_plan["calendar_events"][0]
    check("章节边界快到卡死的元素，展示时长仍不低于 _MIN_VISUAL_HOLD_FRAMES 这个下限"
          "（或者卡在下一段真的紧跟着要用的地盘之前，两者取较严格的那个）",
          tight_cal["endFrame"] - tight_cal["mountFrame"] >= _MIN_VISUAL_HOLD_FRAMES
          or tight_cal["endFrame"] == round(11.2 * FPS),
          tight_cal)
    check("展示时长比修复前的 18 帧(旧行为，无下限保护)有实质性改善",
          tight_cal["endFrame"] - tight_cal["mountFrame"] > 18, tight_cal)

    # 19. Fix C28 回归测试——真实生产复现 job_f7b171f8d952(Dixon 视频)：全片
    # 没有一个数字/日期/风险值，_sparse_gaps 连续 3 轮重规划都没被修好。
    from whatsapp_mvp.content_planner import _sparse_gap_quote_candidates

    gaps = [(90, 360), (600, 930)]  # 两段空档(3s-12s, 20s-31s @30fps)
    segments = [
        {"text": "um", "start": 0.0, "end": 1.0},  # 太短的过渡词，不该被选中
        {"text": "yeah so basically", "start": 4.0, "end": 5.5},  # 空档1里较短的一句
        {"text": "I can help you build a digital human avatar from scratch",
         "start": 6.0, "end": 9.0},  # 空档1里更长、更实质的一句——应该被选中
        {"text": "this line sits between the two gaps and should never be picked",
         "start": 15.0, "end": 18.0},  # 落在两段空档之间，不该被选中
        {"text": "once it's done you can use it for marketing videos",
         "start": 22.0, "end": 25.0},  # 空档2里的一句——应该被选中
    ]
    candidates = _sparse_gap_quote_candidates(gaps, segments)
    check("两段空档各选出一条候选", len(candidates) == 2, candidates)
    check("空档1选中的是更长、更实质的那句，不是过渡词", "digital human avatar" in candidates[0]["text"], candidates)
    check("空档2选中的是落在它自己范围内的那句", "marketing videos" in candidates[1]["text"], candidates)
    check("落在两段空档之间的句子没有被任何一个空档选中",
          all("between the two gaps" not in c["text"] for c in candidates), candidates)

    no_fit_candidates = _sparse_gap_quote_candidates(
        [(90, 360)],
        [{"text": "x" * 200, "start": 5.0, "end": 8.0}],  # 超过 80 字符上限
    )
    check("空档里唯一的句子塞不进 80 字符上限时，宁可不选，不腰斩",
          no_fit_candidates == [], no_fit_candidates)

    empty_gap_candidates = _sparse_gap_quote_candidates([(90, 360)], [])
    check("完全没有转写数据时不报错、返回空列表", empty_gap_candidates == [], empty_gap_candidates)

    # 19b. Fix C28 端到端接线测试——mock 掉 LLM 调用，模拟"纯叙事内容，LLM
    # 三轮都正确地规划不出任何数字/日期"这个真实场景(job_f7b171f8d952)，
    # 验证 plan_content 自己真的会触发这条确定性兜底，而不是只在独立的
    # helper 单测里工作。
    import whatsapp_mvp.content_planner as cp_module
    from whatsapp_mvp.content_planner import plan_content

    def _fake_llm_always_empty(label, system_prompt, user_message, *, temperature, model=None):
        return {"chapters": [{"at_seconds": 0, "label": "TALK"}], "data_points": []}

    long_narrative_segments = [
        {"text": "let me walk you through how this whole system actually works end to end",
         "start": 5.0, "end": 9.0},
    ]
    original_call_llm_json = cp_module._call_llm_json
    cp_module._call_llm_json = _fake_llm_always_empty
    try:
        e2e_plan = plan_content(long_narrative_segments, duration=45.0)
    finally:
        cp_module._call_llm_json = original_call_llm_json
    check("LLM 三轮都规划不出任何东西时，plan_content 自己触发确定性兜底插入 quote",
          len(e2e_plan.get("quotes") or []) >= 1, e2e_plan.get("quotes"))
    if e2e_plan.get("quotes"):
        check("兜底插入的 quote 内容确实来自转写原文，不是编造的",
              "end to end" in e2e_plan["quotes"][0]["text"], e2e_plan["quotes"][0])

    # 20. Fix C30 回归测试——真实生产复现 job_f7b171f8d952(Dixon 视频)：
    # content_planner 已经正确规划出 step_list + corner_card 覆盖某段时间，
    # 但 _coverage_spans 从没把这两种图形类型算进"已覆盖"范围——criterion loop
    # 因此死活觉得这段时间"还是空的"，三轮重规划全部误判失败，即使 LLM 一直
    # 严格照着 SYSTEM_PROMPT 的指引在做正确的事。
    from whatsapp_mvp.content_planner import _coverage_spans, _sparse_gaps

    plan_with_step_list_only = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "A"}],
        "data_points": [{
            "visual": "step_list", "title": "steps",
            "steps": [
                {"label": "一", "seconds": 12.0},
                {"label": "二", "seconds": 16.0},
                {"label": "三", "seconds": 20.0},
            ],
        }],
    }, duration=30.0)
    check("step_list 本身被规划出来了", len(plan_with_step_list_only["step_lists"]) == 1,
          plan_with_step_list_only["step_lists"])
    spans = _coverage_spans(plan_with_step_list_only)
    step_list_entry = plan_with_step_list_only["step_lists"][0]
    check("step_list 的挂载区间出现在覆盖范围里，不再被当成空档",
          (step_list_entry["mountFrame"], step_list_entry["endFrame"]) in spans, spans)

    plan_with_corner_card_only = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "A"}],
        "data_points": [{
            "visual": "corner_card", "variant": "chat", "seconds": 12.0,
            "appName": "WhatsApp", "message": "test",
        }],
    }, duration=30.0)
    check("corner_card 本身被规划出来了", len(plan_with_corner_card_only["corner_cards"]) == 1,
          plan_with_corner_card_only["corner_cards"])
    cc_spans = _coverage_spans(plan_with_corner_card_only)
    cc_entry = plan_with_corner_card_only["corner_cards"][0]
    check("corner_card 的挂载区间出现在覆盖范围里，不再被当成空档",
          (cc_entry["mountFrame"], cc_entry["endFrame"]) in cc_spans, cc_spans)

    # 21. xiaojin arsenal round 2 (2026-07-23) — 6 new content-zone visual
    # types (comparison/ranked_list/checklist/location_pin/testimonial/
    # icon_cluster). Same regression class as test 20 above (Fix C30): each
    # new type must (a) map correctly, (b) skip cleanly when its required
    # field is missing, and (c) actually count toward _coverage_spans/
    # _plan_quality_failures' total_visuals — a type that plans correctly but
    # never reaches those two functions produces the exact "criterion loop
    # thinks this span is still empty" bug C30 fixed for step_list/corner_card.
    new_type_points = {
        "comparison": {
            "visual": "comparison", "seconds": 5.0, "title": "OLD VS NEW",
            "columns": [
                {"label": "旧方案", "label_en": "BEFORE", "accent": "bad", "items": ["手动剪辑", "耗时长"]},
                {"label": "新方案", "label_en": "AFTER", "accent": "good", "items": ["AI 自动化", "更快"]},
            ],
        },
        "ranked_list": {
            "visual": "ranked_list", "seconds": 6.0, "title": "TOP USAGE",
            "items": [
                {"label": "字幕", "label_en": "CAPTIONS", "value": 94, "suffix": "%"},
                {"label": "品牌", "label_en": "BRAND", "value": 81, "suffix": "%"},
            ],
        },
        "checklist": {
            "visual": "checklist", "title": "READY",
            "items": [
                {"label": "字幕已生成", "label_en": "CAPTIONS", "seconds": 7.0},
                {"label": "样式已渲染", "label_en": "STYLE", "seconds": 9.0},
            ],
        },
        "location_pin": {
            "visual": "location_pin", "seconds": 8.0, "place": "香港", "place_en": "HONG KONG",
            "sub": "服务范围",
        },
        "testimonial": {
            "visual": "testimonial", "seconds": 9.0,
            "quote": "剪辑速度快了不止一倍。", "name": "David Chan", "role": "客户",
        },
        "icon_cluster": {
            "visual": "icon_cluster", "seconds": 10.0, "title": "支持的来源",
            "items": [
                {"icon": "chat", "label": "WhatsApp", "label_en": "CHAT"},
                {"icon": "camera", "label": "拍摄", "label_en": "CAMERA"},
            ],
        },
    }
    plan_key_by_visual = {
        "comparison": "comparisons", "ranked_list": "ranked_lists", "checklist": "checklists",
        "location_pin": "location_pins", "testimonial": "testimonials", "icon_cluster": "icon_clusters",
    }
    for visual, dp in new_type_points.items():
        plan_key = plan_key_by_visual[visual]
        solo_plan = _to_frame_plan({
            "chapters": [{"at_seconds": 0, "label": "A"}],
            "data_points": [dp],
        }, duration=30.0)
        check(f"{visual} 正常映射到 plan['{plan_key}']", len(solo_plan[plan_key]) == 1, solo_plan[plan_key])
        entry = solo_plan[plan_key][0]
        check(f"{visual} 计入 _coverage_spans（不会被当成空档）",
              (entry["mountFrame"], entry["endFrame"]) in _coverage_spans(solo_plan),
              _coverage_spans(solo_plan))
        failures = _plan_quality_failures({"data_points": [dp]}, solo_plan, 30.0)
        check(f"{visual} 计入 total_visuals（不会被质量标准误判为零图形）",
              not any("ZERO visual moments" in f for f in failures), failures)

    # 必填字段缺失时整卡跳过，不产出半成品（同 Rule "required text field" 的既有原则）。
    missing_field_points = {
        "comparison": {"visual": "comparison", "seconds": 5.0, "columns": [{"label": "A", "items": ["x"]}]},  # 只有 1 列
        "ranked_list": {"visual": "ranked_list", "seconds": 6.0, "items": [{"label": "A", "value": 1}]},  # 只有 1 项
        "checklist": {"visual": "checklist", "items": [{"label": "A", "seconds": 7.0}]},  # 只有 1 项
        "location_pin": {"visual": "location_pin", "seconds": 8.0},  # 没有 place
        "testimonial": {"visual": "testimonial", "seconds": 9.0, "quote": "hi"},  # 没有 name
        "icon_cluster": {"visual": "icon_cluster", "seconds": 10.0, "items": [{"icon": "star", "label": "A"}]},  # 只有 1 项
    }
    for visual, dp in missing_field_points.items():
        plan_key = plan_key_by_visual[visual]
        bad_plan = _to_frame_plan({
            "chapters": [{"at_seconds": 0, "label": "A"}],
            "data_points": [dp],
        }, duration=30.0)
        check(f"{visual} 缺必填内容时整卡跳过，不产出半成品", bad_plan[plan_key] == [], bad_plan[plan_key])

    # 21b. xiaojin arsenal round 3 (2026-07-23) — 6 more content-zone visual
    # types (progress_bar/pros_cons/milestone_track/trust_badge/bar_chart/
    # milestone_unlock). Same regression class as round 2 above.
    new_type_points_r3 = {
        "progress_bar": {
            "visual": "progress_bar", "seconds": 5.0, "title": "RENEWAL STEPS",
            "label": "Document review", "percent": 80, "sub": "4 of 5 steps complete",
        },
        "pros_cons": {
            "visual": "pros_cons", "seconds": 6.0, "title": "Renew vs Lapse",
            "pros_label": "RENEW", "cons_label": "LAPSE",
            "pros": ["Same rate locked in", "Coverage stays active"],
            "cons": ["Rates may increase", "New underwriting required"],
        },
        "milestone_track": {
            "visual": "milestone_track", "title": "Policy Timeline",
            "milestones": [
                {"label": "Purchased", "sublabel": "2023", "seconds": 7.0},
                {"label": "Renewal Due", "sublabel": "2026", "seconds": 9.0},
            ],
        },
        "trust_badge": {
            "visual": "trust_badge", "seconds": 8.0, "title": "Credentials",
            "badges": [
                {"icon": "shield", "primary": "Licensed Agent", "secondary": "CA LICENSE #88291"},
            ],
        },
        "bar_chart": {
            "visual": "bar_chart", "seconds": 9.0, "title": "Avg Claim Payout",
            "items": [
                {"label": "BASIC", "value": 5000, "display_value": "$5K"},
                {"label": "PREMIUM", "value": 40000, "display_value": "$40K"},
            ],
        },
        "milestone_unlock": {
            "visual": "milestone_unlock", "seconds": 10.0, "value": 1000,
            "suffix": "+", "label": "Families Protected", "icon": "award",
        },
    }
    plan_key_by_visual_r3 = {
        "progress_bar": "progress_bars", "pros_cons": "pros_cons",
        "milestone_track": "milestone_tracks", "trust_badge": "trust_badges",
        "bar_chart": "bar_charts", "milestone_unlock": "milestone_unlocks",
    }
    for visual, dp in new_type_points_r3.items():
        plan_key = plan_key_by_visual_r3[visual]
        solo_plan = _to_frame_plan({
            "chapters": [{"at_seconds": 0, "label": "A"}],
            "data_points": [dp],
        }, duration=30.0)
        check(f"{visual} 正常映射到 plan['{plan_key}']", len(solo_plan[plan_key]) == 1, solo_plan[plan_key])
        entry = solo_plan[plan_key][0]
        check(f"{visual} 计入 _coverage_spans（不会被当成空档）",
              (entry["mountFrame"], entry["endFrame"]) in _coverage_spans(solo_plan),
              _coverage_spans(solo_plan))
        failures = _plan_quality_failures({"data_points": [dp]}, solo_plan, 30.0)
        check(f"{visual} 计入 total_visuals（不会被质量标准误判为零图形）",
              not any("ZERO visual moments" in f for f in failures), failures)

    missing_field_points_r3 = {
        "progress_bar": {"visual": "progress_bar", "seconds": 5.0, "label": "Steps"},  # 没有 percent
        "pros_cons": {"visual": "pros_cons", "seconds": 6.0, "pros_label": "RENEW",
                      "cons_label": "LAPSE", "pros": ["x"], "cons": []},  # cons 是空的
        "milestone_track": {"visual": "milestone_track",
                             "milestones": [{"label": "A", "seconds": 7.0}]},  # 只有 1 个
        "trust_badge": {"visual": "trust_badge", "seconds": 8.0, "badges": [{"icon": "shield"}]},  # 缺 primary/secondary
        "bar_chart": {"visual": "bar_chart", "seconds": 9.0,
                      "items": [{"label": "A", "value": 1}]},  # 只有 1 项
        "milestone_unlock": {"visual": "milestone_unlock", "seconds": 10.0, "value": 1000},  # 没有 label
    }
    for visual, dp in missing_field_points_r3.items():
        plan_key = plan_key_by_visual_r3[visual]
        bad_plan = _to_frame_plan({
            "chapters": [{"at_seconds": 0, "label": "A"}],
            "data_points": [dp],
        }, duration=30.0)
        check(f"{visual} 缺必填内容时整卡跳过，不产出半成品", bad_plan[plan_key] == [], bad_plan[plan_key])

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All content_planner field-mapping tests passed.")


if __name__ == "__main__":
    main()

# content_planner 字段映射单测（对齐 feat/pipeline-remove-filler-apply-style 合并后的
# 完整 Data Display Analysis 输出形状：4 种 visual 类型 + mode_schedule），不依赖 LLM。
#
# Run: uv run python -m whatsapp_mvp.test_content_planner

from __future__ import annotations

from whatsapp_mvp.content_planner import FPS, MOUNT_LEAD_FRAMES, MIN_GAP_AFTER_PREVIOUS_FRAMES, _to_frame_plan

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

    # 6. intro/outro/双语章节映射（对齐 VeLL 参考成片的可自动化元素）
    plan = _to_frame_plan({
        "chapters": [{"at_seconds": 0, "label": "續期日期", "label_en": "RENEWAL"}],
        "intro": {"eyebrow": "policy renewal reminder", "title": "保單續期提醒", "subtitle": "Pacific Life"},
        "outro": {"kicker": "renew on time", "headline": "準時續保", "headline_accent": "保障不中斷",
                  "subtext": "有問題請聯絡我", "cta_label": "立即續保"},
    }, duration=60.0)
    check("chapter 带 labelEn", plan["chapters"][0].get("labelEn") == "RENEWAL", plan["chapters"][0])
    check("intro 映射 + eyebrow 大写", plan["intro"] == {
        "eyebrow": "POLICY RENEWAL REMINDER", "title": "保單續期提醒", "subtitle": "Pacific Life"}, plan["intro"])
    check("outro 映射(cta_label->ctaLabel, accent 保留)", plan["outro"]["ctaLabel"] == "立即續保"
          and plan["outro"]["headlineAccent"] == "保障不中斷", plan["outro"])

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

    # 8. 密度下限：空档检测 / 确定性兜底 / 短片豁免
    from whatsapp_mvp.content_planner import (
        _apply_richness_floor, _fallback_quotes_for_gaps, _sparse_gaps, RICHNESS_WINDOW_FRAMES,
    )
    # 60s 视频、无任何画布事件 -> 中段应报告空档
    bare = _to_frame_plan({"chapters": []}, duration=60.0)
    gaps = _sparse_gaps(bare, 60.0)
    check("裸计划在长视频上检出空档", len(gaps) >= 1, gaps)

    # 短片(12s)豁免：intro+outro 已覆盖，不该报空档
    check("短视频豁免密度检查", _sparse_gaps(_to_frame_plan({"chapters": []}, 12.0), 12.0) == [])

    # 确定性兜底(禁用重规划)：从空档内最长转写句合成金句。段落间距 <8s
    # (RICHNESS_WINDOW_FRAMES)，确保稀疏的 60s 裸计划里每个窗口都有转写句可挑，
    # 不然"零残余空档"这个断言本身就不成立（8s 窗口下 60s 稀疏视频需要 ~7 个
    # 窗口都有覆盖，而不是旧 12s 阈值下的 ~4 个）。
    segs = [
        {"start": 4.0, "end": 7.0, "text": "this is the single most important thing to remember"},
        {"start": 12.0, "end": 15.0, "text": "another decently long spoken line right here"},
        {"start": 20.0, "end": 23.0, "text": "a third full sentence in the middle of the video"},
        {"start": 28.0, "end": 31.0, "text": "and a fourth one further along in the timeline"},
        {"start": 36.0, "end": 39.0, "text": "getting close to the end but still going strong"},
        {"start": 44.0, "end": 47.0, "text": "one more full sentence near the end of it"},
        {"start": 51.0, "end": 54.0, "text": "and the final full sentence right before the outro"},
    ]
    fixed = _apply_richness_floor({"chapters": []}, bare, segs, 60.0, allow_replan=False)
    check("兜底金句用了空档内的原话", len(fixed["quotes"]) >= 2
          and fixed["quotes"][0]["text"].startswith("this is the single"), fixed["quotes"])
    check("兜底后无残余空档（有转写句可用的时段）", _sparse_gaps(fixed, 60.0) == [],
          {"before": gaps, "after": _sparse_gaps(fixed, 60.0)})

    # 确认过的真实生产 bug：转写句只是"跨过"空档边界（不是完全落在窗口内）时，
    # 旧的 containment 检查（start>=a_s and end<=b_s）会漏掉它——真实案例是一段
    # 27s-39s 的空档，里面全是话，但每句话的起止都稍微越出窗口边界，旧逻辑判定
    # "无可用转写句"，视频原样带着空白播出。这里构造同样的越界场景验证修复：
    # 句子横跨窗口起点（cursor 之前开始，窗口内结束）。
    overlap_gaps = [(round(12.0 * FPS), round(35.0 * FPS))]
    overlap_segs = [
        {"start": 9.0, "end": 15.0, "text": "a sentence that starts before the gap and ends inside it"},
        {"start": 30.0, "end": 40.0, "text": "a sentence that starts inside the gap and ends after it"},
    ]
    overlap_fixed = _fallback_quotes_for_gaps(overlap_gaps, overlap_segs, set())
    check("空档边界重叠(非完全包含)的转写句也能被兜底捡到",
          len(overlap_fixed) >= 1, overlap_fixed)

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

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All content_planner field-mapping tests passed.")


if __name__ == "__main__":
    main()

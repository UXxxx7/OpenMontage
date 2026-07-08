# content_planner 字段映射单测（对齐 feat/pipeline-remove-filler-apply-style 合并后的
# 完整 Data Display Analysis 输出形状：4 种 visual 类型 + mode_schedule），不依赖 LLM。
#
# Run: uv run python -m whatsapp_mvp.test_content_planner

from __future__ import annotations

from whatsapp_mvp.content_planner import FPS, MOUNT_LEAD_FRAMES, _to_frame_plan

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

    # 5b. endFrame 接力钳制（merge runbook P2 任务）：同坑位的两个图形，
    # 前者的 endFrame 必须被钳到后者的 mountFrame，否则永久重叠。
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "count_up", "title": "A",
         "rows": [{"label": "X", "seconds": 5.0, "value": 1}]},
        {"visual": "count_up", "title": "B",
         "rows": [{"label": "Y", "seconds": 7.0, "value": 2}]},
    ]}, duration=60.0)
    a, b = sorted(plan["data_cards"], key=lambda c: c["mountFrame"])
    check("被接替的卡 endFrame == 后一张的 mountFrame", a["endFrame"] == b["mountFrame"],
          {"a_end": a["endFrame"], "b_mount": b["mountFrame"]})
    check("最后一张卡保留自己的停留窗口 endFrame", b.get("endFrame", 0) > b["mountFrame"], b.get("endFrame"))

    # 5c. 跨类型同坑位也钳制（count_up 后接 gauge）
    plan = _to_frame_plan({"chapters": [], "data_points": [
        {"visual": "count_up", "title": "A", "rows": [{"label": "X", "seconds": 5.0, "value": 1}]},
        {"visual": "gauge", "seconds": 7.0, "title": "R", "leftLabel": "L", "rightLabel": "R", "value": 0.5},
    ]}, duration=60.0)
    card = plan["data_cards"][0]; gauge = plan["gauges"][0]
    check("跨类型接力: 卡的 endFrame == 仪表盘 mountFrame", card["endFrame"] == gauge["mountFrame"],
          {"card_end": card["endFrame"], "gauge_mount": gauge["mountFrame"]})

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

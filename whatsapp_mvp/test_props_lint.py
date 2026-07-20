# props_lint 单测——包括对真实生产 props 的自检验收（Fix C4 的验收标准：
# 跑在 job_e44166eb8c38 的真实（未修复）props 上必须抓到全部 3 个已确认的
# 真实视觉 bug）。
#
# Run: uv run python -m whatsapp_mvp.test_props_lint

from __future__ import annotations

from whatsapp_mvp.props_lint import lint_props

FAILED = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL") + f" [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


# 真实生产 props（job_e44166eb8c38，用户实际审阅过、报告了 4 个问题的那个
# 版本）——只保留 lint_props 会用到的字段。数值是从
# storage/jobs/job_e44166eb8c38/_op_apply_style_props.json 原样摘录的。
REAL_BUGGY_PROPS = {
    "durationSeconds": 50.873,
    "colorMode": "warm",
    "scenes": [
        {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
        {"frame": 100, "x": 60, "y": 104, "w": 960, "h": 900},
        {"frame": 206, "x": 60, "y": 104, "w": 960, "h": 1100},
        {"frame": 216, "x": 60, "y": 104, "w": 960, "h": 900},
        {"frame": 269, "x": 60, "y": 104, "w": 960, "h": 1100},
        {"frame": 279, "x": 60, "y": 104, "w": 960, "h": 900},
        {"frame": 531, "x": 60, "y": 104, "w": 960, "h": 1100},
        {"frame": 761, "x": 60, "y": 104, "w": 960, "h": 900},
    ],
    "opacityKeyframes": [{"frame": 760, "opacity": 1.0}, {"frame": 776, "opacity": 0.0}],
    "sections": [{"fromFrame": 771, "toFrame": 1526, "title": "WARNING", "icon": "warning",
                  "colorMode": "dark", "warn": True}],
    "pills": [
        {"text": "Renew by July 28th", "x": 60, "width": 960, "y": 1360, "mountFrame": 136, "endFrame": 206},
        {"text": "You're covered for $1.5M", "x": 60, "width": 960, "y": 1384, "mountFrame": 334, "endFrame": 531},
        {"text": "Renew to avoid underwriting", "x": 60, "width": 960, "y": 1390, "mountFrame": 973, "endFrame": 1157},
    ],
    "zoneHeaders": [{"title": "RENEWAL", "fromFrame": 102, "toFrame": 771, "x": 60, "y": 1040}],
    "outro": {"kicker": "CONTACT", "headline": "Questions? WhatsApp me!", "fromFrame": 1376},
    "dataCards": [{
        "title": "Coverage & Premium", "x": 60, "y": 1170, "width": 960, "mountFrame": 289, "endFrame": 531,
        "rows": [
            {"label": "Coverage", "value": 1.5, "tone": "accent", "mountOffset": 0, "prefix": "$", "decimals": 1},
            {"label": "Annual Premium", "value": 8400.0, "tone": "normal", "mountOffset": 152,
             "labelEn": "PREMIUM", "prefix": "$", "decimals": 0},
        ],
    }],
    "gauges": [{"title": "Lapse Risk", "leftLabel": "RENEW", "rightLabel": "LAPSE", "value": 0.8,
                "x": 60, "y": 1040, "width": 960, "mountFrame": 928, "endFrame": 1157}],
    "countdowns": [{"value": 30.0, "unitLabel": "DAYS", "label": "RENEWAL",
                     "headline": "Renew your policy in", "x": 60, "y": 1170, "width": 960,
                     "mountFrame": 91, "endFrame": 206, "headlineAccent": "30 days"}],
    "calendarEvents": [{"year": 2026, "month": 7, "targetDay": 28, "eventLabel": "Policy Renewal",
                         "x": 180, "y": 1170, "width": 720, "mountFrame": 226, "endFrame": 269}],
}


def test_real_buggy_props_flags_all_three_known_bugs():
    findings = lint_props(REAL_BUGGY_PROPS)
    checks = {f["check"] for f in findings}

    check("抓到 header-over-card（zoneHeader 画在还没收起的大卡片上）",
          any(f["check"] == "element_over_card" and f.get("card_rect") and "zoneHeader" in str(f) for f in findings)
          or any(f["check"] == "element_over_card" and "RENEWAL" in f.get("detail", "") for f in findings),
          checks)
    check("抓到 countdown-under-card（倒计时画在还没收起的大卡片上）",
          any(f["check"] == "element_over_card" and "countdown" in f.get("detail", "") for f in findings),
          checks)
    check("抓到说话人隐藏时长超预算（>30%）", "facecam_hidden_budget_exceeded" in checks, checks)
    check("抓到单次连续隐藏超过 8s", "facecam_hidden_too_long" in checks, checks)
    check("抓到说话人再也没有恢复", "facecam_never_restored" in checks, checks)

    hidden_finding = next(f for f in findings if f["check"] == "facecam_never_restored")
    check("隐藏起点是第 769 帧附近（真实数值 opacity 在 776 帧到 0，逐帧采样在 <0.5 处标记为 769）",
          760 <= hidden_finding["hidden_from_frame"] <= 776, hidden_finding)

    budget_finding = next(f for f in findings if f["check"] == "facecam_hidden_budget_exceeded")
    check("总隐藏占比约 49-50%（真实报告的是 49.5%）",
          49.0 <= budget_finding["total_hidden_frames"] / budget_finding["duration_frames"] * 100 <= 50.5,
          budget_finding)

    check("抓到 outro 落在隐藏区间里", "outro_during_hidden_facecam" in checks, checks)
    check("抓到接管区间里的死空间（gauge 结束到片尾之间一大段没有任何图形）",
          "takeover_dead_space" in checks, checks)


def test_clean_props_no_findings():
    """健康的 props（卡片始终可见、图形只在卡片已经收起后才出现、没有互相
    重叠、intro 结束后很快就有内容、密度达标）不应该产生任何 finding。
    这个 fixture 需要在每次新增 props_lint 检查时重新核对——Fix C10 加了
    intro_lead_dead_space 检查后，这里补上 introOutFrame（原来没设，默认值
    0 会被当成"intro 结束到 150 帧全空"误判）和第二项内容（凑够 C6 的密度
    下限，20s 需要至少 2 项）。"""
    clean_props = {
        "durationSeconds": 20.0,
        "introOutFrame": 100,
        "scenes": [
            {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
            {"frame": 100, "x": 60, "y": 104, "w": 960, "h": 900},
        ],
        "dataCards": [{"title": "T", "x": 60, "y": 1170, "width": 960, "mountFrame": 150, "endFrame": 400,
                       "rows": [{"label": "X", "value": 1, "mountOffset": 0}]}],
        "topicCards": [{"headline": "Y", "x": 60, "y": 1400, "width": 960, "mountFrame": 200, "endFrame": 350}],
    }
    findings = lint_props(clean_props)
    check("干净的 props 不产生任何 finding", findings == [], findings)


def test_element_overlap_detected_and_not_false_positive():
    same_slot = {
        "durationSeconds": 20.0,
        "topicCards": [
            {"headline": "A", "x": 60, "y": 1040, "width": 960, "mountFrame": 0, "endFrame": 100},
            {"headline": "B", "x": 60, "y": 1040, "width": 960, "mountFrame": 50, "endFrame": 150},
        ],
    }
    findings = lint_props(same_slot)
    check("同一坑位、时间重叠的两个元素被识别为 element_overlap",
          any(f["check"] == "element_overlap" for f in findings), findings)

    different_slot = {
        "durationSeconds": 20.0,
        "topicCards": [
            {"headline": "A", "x": 60, "y": 1040, "width": 960, "mountFrame": 0, "endFrame": 100},
        ],
        "gauges": [
            {"title": "G", "leftLabel": "L", "rightLabel": "R", "value": 0.5,
             "x": 60, "y": 1400, "width": 960, "mountFrame": 50, "endFrame": 150},
        ],
    }
    findings2 = lint_props(different_slot)
    check("不同车道(y 不同、不重叠)的元素即使时间重叠也不误报",
          not any(f["check"] == "element_overlap" for f in findings2), findings2)


def test_element_mounts_during_card_transition():
    """Fix C7：卡片正在两个 scene 关键帧之间变形(w/h 改变)时，不应该有新元素
    挂载——即使矩形完全不重叠，两个动画同时发生本身就是问题（CLAUDE-v2.md
    的"sequential handoff"标准）。"""
    during_transition = {
        "durationSeconds": 20.0,
        "scenes": [
            {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
            {"frame": 100, "x": 60, "y": 104, "w": 960, "h": 900},
        ],
        # y=1400 跟卡片矩形完全不相交，纯粹测"同时挂载"这条规则，不是几何重叠
        "topicCards": [{"headline": "A", "x": 60, "y": 1400, "width": 960, "mountFrame": 50, "endFrame": 150}],
    }
    findings = lint_props(during_transition)
    check("卡片转场期间挂载的新元素被抓到，即使矩形不重叠",
          any(f["check"] == "element_mounts_during_card_transition" for f in findings), findings)

    after_transition = {
        "durationSeconds": 20.0,
        "scenes": [
            {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
            {"frame": 100, "x": 60, "y": 104, "w": 960, "h": 900},
        ],
        "topicCards": [{"headline": "A", "x": 60, "y": 1400, "width": 960, "mountFrame": 150, "endFrame": 250}],
    }
    findings2 = lint_props(after_transition)
    check("转场结束后才挂载的元素不误报",
          not any(f["check"] == "element_mounts_during_card_transition" for f in findings2), findings2)


def test_low_visual_richness():
    """Fix C6 回归测试——用真实 job_95e1e08b0995（MrBeast backtest）最终交付
    的那版稀疏 props 的形状复现：23.6s 视频只有 1 张 beforeAfter 卡，应该被
    抓到；同一时长配上足够的图形内容则不应该误报。"""
    sparse = {
        "durationSeconds": 23.655,
        "beforeAfter": [{"kicker": "X", "leftLabel": "A", "leftValue": 1, "rightLabel": "B", "rightValue": 2,
                          "x": 60, "y": 1170, "width": 960, "mountFrame": 541, "secondRevealFrame": 580, "endFrame": 650}],
    }
    findings = lint_props(sparse)
    check("MrBeast 真实稀疏方案(23.6s 只有 1 项内容)被抓到 low_visual_richness",
          any(f["check"] == "low_visual_richness" for f in findings), findings)

    rich = {
        "durationSeconds": 23.655,
        "beforeAfter": sparse["beforeAfter"],
        "dataCards": [{"title": "T", "x": 60, "y": 1170, "width": 960, "mountFrame": 100, "endFrame": 300,
                       "rows": [{"label": "X", "value": 1, "mountOffset": 0}]}],
    }
    findings2 = lint_props(rich)
    check("同一时长配上第二项内容(2 项，达到 23.655/12≈2 的下限)不误报",
          not any(f["check"] == "low_visual_richness" for f in findings2), findings2)


def test_section_takeover_lacks_content():
    """Fix D6 回归测试——真实 backtest 截图确认的 bug：一个"流程"全画布接管
    既没有 timeline 也没有 icon，SectionLayer 只渲染标题+装饰性光斑，说话人
    被撤走却填不满画布。用真实 job_2dd37e39dfa6 的接管形状复现。"""
    empty_takeover = {
        "durationSeconds": 31.331,
        "sections": [{"fromFrame": 258, "toFrame": 498, "title": "流程", "eyebrow": "PROCESS"}],
    }
    findings = lint_props(empty_takeover)
    check("既没有 timeline 也没有 icon 的接管被抓到",
          any(f["check"] == "section_takeover_lacks_content" for f in findings), findings)

    with_timeline = {
        "durationSeconds": 31.331,
        "sections": [{"fromFrame": 258, "toFrame": 498, "title": "流程", "eyebrow": "PROCESS",
                      "timeline": {"heading": "H", "nodes": [{"label": "A", "revealFrame": 300, "prefix": "",
                                                               "target": 1, "unit": "STEP"}]}}],
    }
    check("带 timeline 的接管不误报",
          not any(f["check"] == "section_takeover_lacks_content" for f in lint_props(with_timeline)),
          with_timeline)

    with_icon = {
        "durationSeconds": 31.331,
        "sections": [{"fromFrame": 258, "toFrame": 498, "title": "流程", "icon": "clock"}],
    }
    check("带 icon 的接管不误报",
          not any(f["check"] == "section_takeover_lacks_content" for f in lint_props(with_icon)),
          with_icon)


def test_intro_lead_dead_space():
    """Fix C10 回归测试——真实生产 bug job_dc6a22198c6d：intro 在第 80 帧结束，
    第一个内容区元素（倒计时）190 帧才挂载，中间 110 帧(3.7s)画面上只有说话
    人和字幕。vision QA 正确抓到"空画布"（高严重度）导致这条视频最终整个
    apply_style 降级交付（用户只收到裸剪的视频，没有任何品牌样式）——props_lint
    当时完全没查过这个模式（takeover_dead_space 只查 hidden_spans），白白
    多烧一轮渲染+vision 调用才发现，且没能在 vision 重试预算内修好。"""
    # Fix C19（2026-07-17，同一 job_dc6a22198c6d 形状在真实生产里复现——
    # job_b7e1b7f96481，用户 WhatsApp 上真实收到的降级交付）：这个 fixture
    # 原本断言 [80,190] 整段(110 帧)都算死空间，但 scenes 里过渡在第 180 帧
    # 才结束——80-180 这一段卡片本身还在变形，190 帧的内容紧跟着过渡结束
    # 只隔 10 帧就挂载，是"过渡+立刻有内容"的正常情况，不是可避免的空白。
    # gap_start 现在会顶到过渡结束的那一帧（180），10 帧的间隔在 60 帧阈值
    # 之内，不应该再报——这条 finding 曾经因为把过渡期也算进"空白"，导致
    # pipeline_runner.py 的确定性保底（Fix C13/C13b）算出的候选卡片位置也
    # 跟着算错、频繁跟旁边的真实内容撞车，安全阀拒绝插入，intro_lead_dead_space
    # 原样交付给用户，最终触发 vision QA 的"空画布"判定——降级交付原样重演。
    no_longer_flagged_props = {
        "durationSeconds": 43.233,
        "introOutFrame": 80,
        "scenes": [
            {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
            {"frame": 180, "x": 60, "y": 104, "w": 960, "h": 900},
        ],
        "countdowns": [{"value": 30.0, "unitLabel": "DAYS", "label": "RENEWAL", "x": 60, "y": 1170,
                         "width": 960, "mountFrame": 190, "endFrame": 367}],
        "dataCards": [{"title": "YOUR COVERAGE", "x": 60, "y": 1170, "width": 960,
                        "mountFrame": 557, "endFrame": 602,
                        "rows": [{"label": "Coverage", "value": 1.5, "mountOffset": 0}]}],
    }
    check("过渡结束(180)到内容挂载(190)只差10帧——不再误报为死空间",
          not any(f["check"] == "intro_lead_dead_space" for f in lint_props(no_longer_flagged_props)),
          lint_props(no_longer_flagged_props))

    # 真正的死空间：过渡在第 180 帧就结束了，但第一个内容一直到第 400 帧
    # 才挂载——过渡结束后还有 220 帧(7.3s)是真的什么都没有，这条必须继续抓到。
    real_gap_props = {
        "durationSeconds": 43.233,
        "introOutFrame": 80,
        "scenes": [
            {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
            {"frame": 180, "x": 60, "y": 104, "w": 960, "h": 900},
        ],
        "countdowns": [{"value": 30.0, "unitLabel": "DAYS", "label": "RENEWAL", "x": 60, "y": 1170,
                         "width": 960, "mountFrame": 400, "endFrame": 567}],
        "dataCards": [{"title": "YOUR COVERAGE", "x": 60, "y": 1170, "width": 960,
                        "mountFrame": 557, "endFrame": 602,
                        "rows": [{"label": "Coverage", "value": 1.5, "mountOffset": 0}]}],
    }
    findings = lint_props(real_gap_props)
    gap_finding = next((f for f in findings if f["check"] == "intro_lead_dead_space"), None)
    check("过渡结束后仍有真正的死空间时照常抓到", gap_finding is not None, findings)
    if gap_finding:
        check("死空间区间是过渡结束到内容挂载 [180, 400]（220 帧/7.3s），不是 [80,400]",
              gap_finding["gap_start"] == 180 and gap_finding["gap_end"] == 400, gap_finding)

    tight_props = {
        "durationSeconds": 20.0,
        "introOutFrame": 80,
        "topicCards": [{"headline": "A", "x": 60, "y": 1400, "width": 960, "mountFrame": 110, "endFrame": 250}],
        "dataCards": [{"title": "T", "x": 60, "y": 1170, "width": 960, "mountFrame": 300, "endFrame": 450,
                       "rows": [{"label": "X", "value": 1, "mountOffset": 0}]}],
    }
    check("intro 结束后 30 帧(1s)内就有内容——不到 60 帧阈值，不误报",
          not any(f["check"] == "intro_lead_dead_space" for f in lint_props(tight_props)),
          lint_props(tight_props))

    no_elements_props = {"durationSeconds": 10.0, "introOutFrame": 80}
    check("整片没有任何内容区元素时不因为这条检查额外报错(交给 richness/zero-visual 检查处理)",
          not any(f["check"] == "intro_lead_dead_space" for f in lint_props(no_elements_props)),
          lint_props(no_elements_props))


def main():
    test_real_buggy_props_flags_all_three_known_bugs()
    test_clean_props_no_findings()
    test_element_overlap_detected_and_not_false_positive()
    test_element_mounts_during_card_transition()
    test_low_visual_richness()
    test_section_takeover_lacks_content()
    test_intro_lead_dead_space()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All props_lint tests passed.")


if __name__ == "__main__":
    main()

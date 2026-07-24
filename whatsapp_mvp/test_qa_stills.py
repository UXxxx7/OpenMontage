# qa_stills 单测——Fix C22 回归测试。
#
# Run: uv run python -m whatsapp_mvp.test_qa_stills

from __future__ import annotations

import whatsapp_mvp.qa_stills as qa_stills
from whatsapp_mvp.qa_stills import pick_qa_frames

FAILED = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL") + f" [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


# 真实生产 props（job_ac00838adea9，反复触发"品牌样式渲染"降级交付、也就是
# 用户在 WhatsApp 上收到的那条"提醒：品牌样式渲染这一步没成功…"消息）——
# 只保留 pick_qa_frames 会用到的字段。scenes 数值是从
# storage/jobs/job_ac00838adea9/_op_apply_style_props.json 原样摘录的。
REAL_JOB_AC008 = {
    "durationSeconds": 46.036,
    "introOutFrame": 80,
    "scenes": [
        {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
        {"frame": 180, "x": 60, "y": 104, "w": 960, "h": 900},
        {"frame": 612, "x": 60, "y": 104, "w": 960, "h": 1100},
        {"frame": 698, "x": 60, "y": 104, "w": 960, "h": 900},
        {"frame": 948, "x": 60, "y": 104, "w": 960, "h": 900},
        {"frame": 1094, "x": 60, "y": 104, "w": 960, "h": 1100},
        {"frame": 1162, "x": 60, "y": 104, "w": 960, "h": 900},
        {"frame": 1285, "x": 60, "y": 104, "w": 960, "h": 1100},
    ],
}


# 真实生产 props（job_452ef6c48100，2026-07-21 复现的"品牌样式渲染"降级——
# 数据卡显示 "$6,300"/"$1.1M"，但转写只说过 "$8,400"/"one and a half
# million"）——只保留 pick_qa_frames 会用到的字段，原样摘自
# storage/jobs/job_452ef6c48100/_op_apply_style_props.json。
REAL_JOB_452EF6 = {
    "durationSeconds": 50.042,
    "introOutFrame": 80,
    "scenes": [
        {"frame": 0, "w": 960, "h": 1100}, {"frame": 20, "w": 960, "h": 900},
        {"frame": 180, "w": 960, "h": 900}, {"frame": 592, "w": 960, "h": 900},
        {"frame": 612, "w": 960, "h": 1100}, {"frame": 632, "w": 960, "h": 900},
        {"frame": 700, "w": 960, "h": 900}, {"frame": 950, "w": 960, "h": 900},
        {"frame": 1098, "w": 960, "h": 900}, {"frame": 1118, "w": 960, "h": 1100},
    ],
    "dataCards": [{
        "mountFrame": 378,
        "rows": [{"label": "Coverage", "mountOffset": 0}, {"label": "Annual Premium", "mountOffset": 186}],
    }],
}


def test_data_card_sampled_after_count_up_animation_settles():
    """Fix C46 回归测试——真实生产 bug job_452ef6c48100：InfoCard.tsx 的
    count_up 数字用 interpolate(rowLocal, [0, 40], [0, row.value], ...)
    动画，40 帧才到最终值，但旧代码只在 mountFrame+last_row+30 处采样——
    早了 10 帧，抓到的是还在往上数、尚未到达目标值的数字。真实复现：
    最后一行 mountOffset=186，卡片 mountFrame=378，旧代码采样第
    378+186+30=594 帧（動畫進度 30/40=75%），把 "$8,400" 读成"$6,300"
    （8400*0.75=6300，分毫不差），vision QA 因此把这个纯粹的动画进度当成
    了内容错误，触发降级交付——不是 content_planner 提取错了数字。"""
    frames = pick_qa_frames(REAL_JOB_452EF6)
    check("不再采样動畫進度 75% 的旧位置(378+186+30=594)——那一帧数字还没数完",
          594 not in frames, frames)
    check("改采样動畫已經定格之後的帧(378+186+45=609)",
          609 in frames, frames)


def test_other_rules_frame_pushed_out_of_count_up_window_too():
    """Fix C48 回归测试——真实生产复现（同一支 job_452ef6c48100，就在验证
    C46 之后的下一次真实渲染里）：这次不是 C46 已经堵住的"数据卡专属"采样
    规则，是转场窗口规则（win_end+8）独立算出的帧，恰好也落进了同一个
    count_up 行还在数数的窗口，读到 "$4,200"——正好是 "$8,400" 的 50%，
    也就是 40 帧动画走到第 20 帧时的读数，不是 C46 修的那个 75% 位置。
    构造一个转场窗口，让它的 win_end+8 落进 count_up 窗口内，验证统一
    后处理（不管哪条规则产生的帧）都会把它推出去，而不是只堵一条路。"""
    props = {
        "durationSeconds": 50.0,
        "introOutFrame": 20,
        "dataCards": [{
            "mountFrame": 400,
            "rows": [{"label": "Annual Premium", "mountOffset": 0, "value": 8400}],
        }],
        "scenes": [
            {"frame": 0, "w": 960, "h": 1100},
            {"frame": 300, "w": 960, "h": 900},
            {"frame": 412, "w": 960, "h": 1100},  # win_end+8 = 420, lands inside [400, 445)
        ],
    }
    frames = pick_qa_frames(props)
    check("转场规则独立算出的帧(412+8=420)落进了 count_up 窗口，不会原样保留",
          420 not in frames, frames)
    check("被推到 count_up 窗口结束点(400+45=445)，数字已经数完",
          445 in frames, frames)


def test_frame_zero_not_sampled_as_pre_transition_check():
    """Fix C22 回归测试——真实生产 bug job_ac00838adea9（同一模式也在
    job_1b7254abcd66 上复现过）：scenes[0] 恒为 frame=0，_transition_windows
    的第一个窗口因此永远是 (0, X)，旧代码无条件对每一条视频都采样
    max(0, 0-8)=0——也就是 SpeakerCard 进场动画都还没开始画的那一帧。打开
    这一帧的真实渲染结果（f0.png）看到的是纯背景色，不是"内容稀疏"，是
    真的什么都没有——但这不是内容规划能修的 bug（content_planner 对
    SpeakerCard 自身进场时机毫无控制权），vision QA 对同一帧的判断还不稳定
    （job_localdemowalk 判成 low severity 放行，这条真实 job 判成 high
    severity 触发降级），于是"重试后仍发现问题"每次都原样重演，最终耗尽
    重试预算触发 _DEGRADABLE_OPS 降级——用户收到的就是那条反复出现的
    WhatsApp 提醒。"""
    frames = pick_qa_frames(REAL_JOB_AC008)
    check("不再采样第 0 帧（进场动画还没开始画的那一帧，转场前置检查对第一个窗口没有意义）",
          0 not in frames, frames)
    check("转场结束侧(180+8=188)照常采样——确认进场收尾后内容确实落位",
          188 in frames, frames)
    # 第二个窗口 (612, 698) 是一次真正的内容中段转场（Workflow->Dominant 再
    # ->Workflow），前后两侧都该照常采样，不受 C22 影响。
    check("非首个窗口的转场前侧(612-8=604)照常采样",
          604 in frames, frames)
    check("非首个窗口的转场结束侧(698+8=706)照常采样",
          706 in frames, frames)


def test_transition_starting_at_frame_zero_without_other_frames():
    """最小场景：只有两个 scene 关键帧、第一个就在 frame 0——曾经的 bug 会让
    这种最简单的情况也采样到第 0 帧。"""
    minimal_props = {
        "durationSeconds": 10.0,
        "introOutFrame": 80,
        "scenes": [
            {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
            {"frame": 120, "x": 60, "y": 104, "w": 960, "h": 900},
        ],
    }
    frames = pick_qa_frames(minimal_props)
    check("唯一一个转场窗口就是首个窗口时，第 0 帧仍然不被采样",
          0 not in frames, frames)


# 真实生产 props（同一个 job_ac00838adea9，在第一次尝试 Fix C22 之后做的一次
# 真实 live 重跑里被 content_planner 重新规划出来的版本——intro 收起过渡只
# 用了 20 帧(0->20)而不是原来的 180 帧）。这不是内容规划能控制的确定性输入，
# 但足以复现下面这个 bug：第一次 Fix C22 只patch了 _transition_windows 那条
# 路径，这条 props 通过完全不同的 full_frames 中点规则又把第 0 帧带回来了。
REAL_JOB_AC008_RERUN_SHORT_INTRO = {
    "durationSeconds": 46.036,
    "introOutFrame": 80,
    "scenes": [
        {"frame": 0, "x": 60, "y": 104, "w": 960, "h": 1100},
        {"frame": 20, "x": 60, "y": 104, "w": 960, "h": 900},
        {"frame": 180, "x": 60, "y": 104, "w": 960, "h": 900},
        {"frame": 698, "x": 60, "y": 104, "w": 960, "h": 900},
        {"frame": 954, "x": 60, "y": 104, "w": 960, "h": 900},
    ],
}


def test_full_frames_midpoint_does_not_resolve_to_frame_zero():
    """Fix C22 的第二部分——第一次修复只堵了 _transition_windows 那一条采样
    路径，同一个 job 在真实 live 重跑（真实渲染 + 真实视觉复审调用，不是
    props 层面的单测）里，content_planner 这次规划出的 intro 收起过渡只有
    20 帧长(scenes[0]->scenes[1])，短到第 30 帧就已经 docked。`full_frames`
    （"全屏区间中点"规则，扫 range(0, duration, 30) 里没有 dock 的帧）在这种
    情况下算出来只有 [0] 一个元素——中点公式 full_frames[len//2] 因此又等于
    0，跟转场前置采样完全是两条独立的代码路径，却复现了同一个"送去 vision QA
    的第 0 帧永远是纯背景色"的问题。用真实复现过这个 case 的 scenes 结构验证：
    只在函数出口统一过滤掉第 0 帧（而不是逐条规则打补丁）才能同时堵住这两条
    路径，也堵住任何未来可能出现的第三条。"""
    frames = pick_qa_frames(REAL_JOB_AC008_RERUN_SHORT_INTRO)
    check("intro 收起过渡很短(0->20帧)、full_frames 只剩 [0] 时，第 0 帧仍然不被采样",
          0 not in frames, frames)
    check("intro 结束侧(20+8=28)照常采样——确认收起完成后内容确实落位",
          28 in frames, frames)


# Fix C23 回归测试——真实生产复现 job_452ef6c48100：VISION_LLM_MODEL=
# glm-4v-flash 对 frame_index 0 报了三条 high severity 问题（脸部裁切、两处
# 文字截断），但打开实际渲染的 f28.png 核实，两处"文字截断"完全不存在——
# 两段文字在图上都清晰完整。这套单测用假的 _vision_review 模拟"模型在两次
# 独立调用里给出不同答案"，验证只有两次都判定同一 frame_index 为 high 的
# 发现才会被采信。


def _fake_vision_review(responses):
    """按调用顺序依次返回 responses 里的字典，用于替换 qa_stills._vision_review。"""
    calls = {"n": 0}

    def _fake(still_paths):
        i = calls["n"]
        calls["n"] += 1
        return responses[i] if i < len(responses) else responses[-1]

    return _fake, calls


def test_unconfirmed_high_finding_is_dropped():
    """第一次调用报 frame_index 0 是 high；第二次独立调用完全没提这条
    （模型噪音，不可复现）——不应该被采信为真实问题。"""
    fake, calls = _fake_vision_review([
        {"findings": [{"frame_index": 0, "issue": "文字被截断", "severity": "high"}], "overall": "x"},
        {"findings": [], "overall": "looks fine"},
    ])
    original = qa_stills._vision_review
    qa_stills._vision_review = fake
    try:
        result = qa_stills._vision_review_confirmed(["f0.png"])
    finally:
        qa_stills._vision_review = original
    check("二次复核未复现的 high 发现被丢弃", result["findings"] == [], result)
    check("调用了两次视觉复审（第一次有 high 发现才需要确认）", calls["n"] == 2, calls)


def test_confirmed_high_finding_is_kept():
    """两次独立调用都判定同一 frame_index 是 high——判定为真实问题，保留。"""
    fake, calls = _fake_vision_review([
        {"findings": [{"frame_index": 0, "issue": "说话人取景偏紧", "severity": "high"}], "overall": "x"},
        {"findings": [{"frame_index": 0, "issue": "脸部太靠近卡片边缘", "severity": "high"}], "overall": "y"},
    ])
    original = qa_stills._vision_review
    qa_stills._vision_review = fake
    try:
        result = qa_stills._vision_review_confirmed(["f0.png"])
    finally:
        qa_stills._vision_review = original
    check("两次都复现的 high 发现被保留", len(result["findings"]) == 1, result)
    check("保留的是第一次调用的原始描述", result["findings"][0]["issue"] == "说话人取景偏紧", result)


def test_low_severity_findings_never_need_confirmation():
    """low 严重度发现不驱动任何重试/降级决策，不需要二次确认——但如果同一次
    调用里还有 high 发现，仍然会触发确认调用（这里验证 low 本身在这种情况下
    也原样保留，不会被第二次调用意外过滤掉）。"""
    fake, calls = _fake_vision_review([
        {"findings": [
            {"frame_index": 0, "issue": "对比度略低", "severity": "low"},
            {"frame_index": 1, "issue": "脸部被裁切", "severity": "high"},
        ], "overall": "x"},
        {"findings": [], "overall": "looks fine on second look"},
    ])
    original = qa_stills._vision_review
    qa_stills._vision_review = fake
    try:
        result = qa_stills._vision_review_confirmed(["f0.png", "f1.png"])
    finally:
        qa_stills._vision_review = original
    check("low 严重度发现始终保留，不受二次确认影响",
          any(f["severity"] == "low" for f in result["findings"]), result)
    check("未复现的 high 发现被丢弃，只剩 low",
          all(f["severity"] == "low" for f in result["findings"]), result)


def test_no_high_findings_skips_confirmation_call():
    """第一次调用完全没有 high 发现时，不需要多花一次确认调用——干净路径
    成本不变。"""
    fake, calls = _fake_vision_review([
        {"findings": [{"frame_index": 0, "issue": "对比度略低", "severity": "low"}], "overall": "clean"},
    ])
    original = qa_stills._vision_review
    qa_stills._vision_review = fake
    try:
        result = qa_stills._vision_review_confirmed(["f0.png"])
    finally:
        qa_stills._vision_review = original
    check("没有 high 发现时只调用一次，不做二次确认", calls["n"] == 1, calls)
    check("原始 low 发现原样返回", result["findings"][0]["severity"] == "low", result)


def test_second_call_failure_drops_all_high_findings():
    """确认调用本身失败（网络异常/未配置，_vision_review 返回 None）时，宁可
    保守地丢弃所有未经确认的 high 发现，不能让它们在没有二次确认的情况下
    仍然触发重规划/降级。"""
    fake, calls = _fake_vision_review([
        {"findings": [{"frame_index": 0, "issue": "脸部被裁切", "severity": "high"}], "overall": "x"},
        None,
    ])
    original = qa_stills._vision_review
    qa_stills._vision_review = fake
    try:
        result = qa_stills._vision_review_confirmed(["f0.png"])
    finally:
        qa_stills._vision_review = original
    check("确认调用失败时未经确认的 high 发现被丢弃", result["findings"] == [], result)


def main():
    test_data_card_sampled_after_count_up_animation_settles()
    test_other_rules_frame_pushed_out_of_count_up_window_too()
    test_frame_zero_not_sampled_as_pre_transition_check()
    test_transition_starting_at_frame_zero_without_other_frames()
    test_full_frames_midpoint_does_not_resolve_to_frame_zero()
    test_unconfirmed_high_finding_is_dropped()
    test_confirmed_high_finding_is_kept()
    test_low_severity_findings_never_need_confirmation()
    test_no_high_findings_skips_confirmation_call()
    test_second_call_failure_drops_all_high_findings()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All qa_stills tests passed.")


if __name__ == "__main__":
    main()

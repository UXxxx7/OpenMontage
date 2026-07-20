# qa_stills 单测——Fix C22 回归测试。
#
# Run: uv run python -m whatsapp_mvp.test_qa_stills

from __future__ import annotations

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


def main():
    test_frame_zero_not_sampled_as_pre_transition_check()
    test_transition_starting_at_frame_zero_without_other_frames()
    test_full_frames_midpoint_does_not_resolve_to_frame_zero()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All qa_stills tests passed.")


if __name__ == "__main__":
    main()

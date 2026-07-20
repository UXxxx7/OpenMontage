# plan_filler_removal 的事后复核 + 单调重试 + 确定性重复短语兜底单测——不依赖
# 真实 LLM，monkeypatch content_planner 自己命名空间里的 call_llm_chat（不是
# llm_client.call_llm_chat，因为 content_planner.py 是
# `from .llm_client import call_llm_chat`，patch 后者对已绑定的名字没有效果）。
#
# Run: uv run python -m whatsapp_mvp.test_filler_review

from __future__ import annotations

import json

import whatsapp_mvp.content_planner as content_planner
from whatsapp_mvp.content_planner import (
    FILLER_SYSTEM_PROMPT,
    VERIFY_FILLER_SYSTEM_PROMPT,
    _cut_duplicate_phrases,
)

FAILED = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL") + f" [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


# 5 个词的假转写："Hi um there uh yeah" —— um(1) / uh(3) 是要剪掉的口误。
WORDS = [
    {"word": "Hi", "start": 0.0, "end": 0.3},
    {"word": "um", "start": 0.3, "end": 0.6},
    {"word": "there", "start": 0.6, "end": 1.0},
    {"word": "uh", "start": 1.0, "end": 1.3},
    {"word": "yeah", "start": 1.3, "end": 1.6},
]


def _is_filler_call(system_prompt):
    return system_prompt == FILLER_SYSTEM_PROMPT


def _is_verify_call(system_prompt):
    return system_prompt == VERIFY_FILLER_SYSTEM_PROMPT


def test_clean_first_pass():
    calls = []

    def fake(system_prompt, user_message, *, temperature=0.1, model=None):
        calls.append(system_prompt)
        if _is_filler_call(system_prompt):
            return json.dumps({"cut_word_indices": [1, 3]})
        if _is_verify_call(system_prompt):
            return json.dumps({"clean": True})
        raise AssertionError("unexpected system_prompt")

    content_planner.call_llm_chat = fake
    result = content_planner.plan_filler_removal(WORDS, duration=1.6)
    check("clean-first-pass: 恰好 2 次 LLM 调用（判断+复核）", len(calls) == 2, calls)
    # FILLER_CUT_PAD_SECONDS=0.08：每段各留一点 padding（钳制到相邻间隙的一半），
    # 不是原始词边界本身——数值对应 _pad_keep_ranges 的就地计算结果。
    check("clean-first-pass: 结果非空且不重试（含 padding）", result == [
        {"start_seconds": 0.0, "end_seconds": 0.38},
        {"start_seconds": 0.52, "end_seconds": 1.08},
        {"start_seconds": 1.22, "end_seconds": 1.6},
    ], result)


def test_flagged_then_fixed_on_retry():
    """第 1 轮漏判一个口误（"uh"）；复核抓到后，重试轮只喂"当前仍保留的词"
    （不是全量转写）——fake 的重试响应下标是相对这个子集的，验证映射回原始
    下标的逻辑正确，且第 1 轮已经剪对的 "um" 不需要重新确认。"""
    calls = []

    def fake(system_prompt, user_message, *, temperature=0.1, model=None):
        calls.append(system_prompt)
        if _is_filler_call(system_prompt):
            if "NOTE: a previous pass" in user_message:
                # 重试轮看到的是 kept_words=[Hi, there, uh, yeah]（"um" 已经
                # 在第 1 轮被剪掉，根本不在这个子集里）—— "uh" 在子集里的相对
                # 下标是 2，不是原始转写里的 3。
                return json.dumps({"cut_word_indices": [2]})
            return json.dumps({"cut_word_indices": [1]})  # first pass: misses "uh"
        if _is_verify_call(system_prompt):
            if "uh" in user_message:
                return json.dumps({"clean": False, "issue": "leftover 'uh' filler word"})
            return json.dumps({"clean": True})
        raise AssertionError("unexpected system_prompt")

    content_planner.call_llm_chat = fake
    result = content_planner.plan_filler_removal(WORDS, duration=1.6)
    check("flagged-then-fixed: 恰好 4 次 LLM 调用（判断+复核+重判+复核）", len(calls) == 4, calls)
    check("flagged-then-fixed: 最终结果剪掉了 um 和 uh（两次判断的并集）", result == [
        {"start_seconds": 0.0, "end_seconds": 0.38},
        {"start_seconds": 0.52, "end_seconds": 1.08},
        {"start_seconds": 1.22, "end_seconds": 1.6},
    ], result)


def test_monotonic_cut_never_resurrected():
    """核心回归测试——对应真实生产 bug（job_e44166eb8c38）：服务端日志显示
    3 轮判断剪掉的词数是 24 -> 28 -> 15，说明旧实现的第 3 轮是对全量转写从头
    判断，把第 2 轮已经正确剪掉的重录又判定为"没问题"，交付了两处遗留重复。

    这里用 7 个词（two/three 是两处该剪的口误）模拟同样的形状：第 1 轮只抓到
    "two"，遗漏"three"；重试轮在"仍保留的 6 个词"范围内正确抓到了"three"
    （相对下标 4，映射回原始下标 5）。断言最终结果里 two 和 three 都被剪掉，
    直接验证"已判定剪掉的词不会被后续轮次判回来"这个不变量。
    """
    seven_words = [
        {"word": "one", "start": 0.0, "end": 0.5},
        {"word": "TWO", "start": 0.5, "end": 1.0},
        {"word": "three_ok", "start": 1.0, "end": 1.5},
        {"word": "four", "start": 1.5, "end": 2.0},
        {"word": "five", "start": 2.0, "end": 2.5},
        {"word": "THREE", "start": 2.5, "end": 3.0},
        {"word": "seven", "start": 3.0, "end": 3.5},
    ]
    calls = []

    def fake(system_prompt, user_message, *, temperature=0.1, model=None):
        calls.append(system_prompt)
        if _is_filler_call(system_prompt):
            if "NOTE: a previous pass" in user_message:
                # kept subset at this point = [one, three_ok, four, five, THREE, seven]
                # (TWO already cut in round 1) — THREE is at relative index 4.
                return json.dumps({"cut_word_indices": [4]})
            return json.dumps({"cut_word_indices": [1]})  # round 1: cuts TWO only
        if _is_verify_call(system_prompt):
            if "THREE" in user_message:
                return json.dumps({"clean": False, "issues": ["leftover 'THREE' filler word"]})
            return json.dumps({"clean": True})
        raise AssertionError("unexpected system_prompt")

    content_planner.call_llm_chat = fake
    result = content_planner.plan_filler_removal(seven_words, duration=3.5)
    kept_text = " ".join(
        w["word"] for w in seven_words
        if any(r["start_seconds"] - 1e-6 <= w["start"] and w["end"] <= r["end_seconds"] + 1e-6 for r in result)
    )
    check("monotonic: TWO 和 THREE 都被剪掉（第 2 轮没有把第 1 轮的裁剪判回来）",
          "TWO" not in kept_text and "THREE" not in kept_text, kept_text)
    check("monotonic: three_ok 保留（只有真正被判定的口误才会被剪）",
          "three_ok" in kept_text, kept_text)


def test_best_result_delivered_not_last():
    """A2：重试耗尽时交付"遗留问题最少"的一次尝试，不是无条件交付最后一轮。
    构造一个故意对抗性的场景：第 1 轮什么都不剪（1 个遗留问题）；重试轮剪掉了
    "B"，但复核反而报告更多问题（2 个）——最终结果必须回退到"什么都不剪"的
    状态（0 处裁剪），而不是"剪了 B 但问题更多"的最后一轮状态。
    """
    four_words = [
        {"word": "A", "start": 0.0, "end": 0.5},
        {"word": "B", "start": 0.5, "end": 1.0},
        {"word": "C", "start": 1.0, "end": 1.5},
        {"word": "D", "start": 1.5, "end": 2.0},
    ]
    calls = []

    def fake(system_prompt, user_message, *, temperature=0.1, model=None):
        calls.append(system_prompt)
        if _is_filler_call(system_prompt):
            if "NOTE: a previous pass" in user_message and "B" in user_message:
                return json.dumps({"cut_word_indices": [1]})  # cuts B (relative index within kept subset)
            return json.dumps({"cut_word_indices": []})  # nothing further to cut once B is gone
        if _is_verify_call(system_prompt):
            if "B" in user_message:
                return json.dumps({"clean": False, "issues": ["problem near B"]})
            # B 被剪掉之后，这个假 reviewer 反而报告了 2 个问题——模拟"这次裁剪
            # 把情况弄得更糟"的场景，用来验证"挑遗留问题最少的一次"这个机制。
            return json.dumps({"clean": False, "issues": ["worse problem 1", "worse problem 2"]})
        raise AssertionError("unexpected system_prompt")

    content_planner.call_llm_chat = fake
    result = content_planner.plan_filler_removal(four_words, duration=2.0)
    check("best-result: 最终交付的是问题最少的状态（什么都没剪），不是最后一轮",
          result == [], result)


def test_flagged_still_flagged_after_all_retries():
    """复核每一轮都报告同一个遗留问题、且判断轮从未真正剪掉那个词——必须在
    FILLER_VERIFY_MAX_RETRIES 次重试后停止（不无限重试），且不抛异常。"""
    calls = []

    def fake(system_prompt, user_message, *, temperature=0.1, model=None):
        calls.append(system_prompt)
        if _is_filler_call(system_prompt):
            return json.dumps({"cut_word_indices": []})  # 从不剪 "uh"，即使重试也一样
        if _is_verify_call(system_prompt):
            return json.dumps({"clean": False, "issue": "leftover 'uh' filler word"})
        raise AssertionError("unexpected system_prompt")

    content_planner.call_llm_chat = fake
    try:
        result = content_planner.plan_filler_removal(WORDS, duration=1.6)
        raised = False
    except Exception:
        raised = True
        result = None
    check("still-flagged: 不抛异常，直接返回", not raised)
    # 1 次初始判断 + 3 次复核（attempt 0/1/2）+ 2 次重试判断（attempt 0/1 之后）= 6
    check("still-flagged: 不会无限重试（初始 1 + 复核 3 + 重试 2 = 6 次调用）", len(calls) == 6, calls)
    check("still-flagged: 从未真正剪掉任何词时返回 []（视为无需改动）", result == [], result)


def test_llm_unconfigured():
    def fake(system_prompt, user_message, *, temperature=0.1, model=None):
        return None

    content_planner.call_llm_chat = fake
    result = content_planner.plan_filler_removal(WORDS, duration=1.6)
    check("LLM 不可用: 返回空列表，不炸", result == [], result)


# --- _cut_duplicate_phrases 直接测试（确定性兜底，Fix A3）-------------------

def _w(word, start, end):
    return {"word": word, "start": start, "end": end}


# 真实生产 bug 复现（job_e44166eb8c38 真实播出内容）："I've put the full
# breakdown in this video." 完整说了两遍，第二次紧接着继续（"...so you have
# everything in one place."）——第一次是废弃的半途重录。
DUP_TAKE_1 = [
    _w("I've", 18.7, 18.9), _w("put", 18.9, 19.1), _w("the", 19.1, 19.2), _w("full", 19.2, 19.5),
    _w("breakdown", 19.5, 20.0), _w("in", 20.0, 20.1), _w("this", 20.1, 20.3), _w("video.", 20.3, 20.5),
    _w("I've", 20.8, 21.0), _w("put", 21.0, 21.2), _w("the", 21.2, 21.3), _w("full", 21.3, 21.6),
    _w("breakdown", 21.6, 22.0), _w("in", 22.0, 22.1), _w("this", 22.1, 22.2), _w("video", 22.2, 22.4),
    _w("so", 22.4, 22.5), _w("you", 22.5, 22.6), _w("have", 22.6, 22.8), _w("everything", 22.8, 23.2),
    _w("in", 23.2, 23.3), _w("one", 23.3, 23.5), _w("place.", 23.5, 24.6),
]

# 第二处真实重复："If you have any questions, just WhatsApp me directly."
DUP_TAKE_2 = [
    _w("If", 35.9, 36.0), _w("you", 36.0, 36.1), _w("have", 36.1, 36.3), _w("any", 36.3, 36.5),
    _w("questions,", 36.5, 36.9), _w("just", 37.1, 37.3), _w("WhatsApp", 37.3, 37.7), _w("me", 37.7, 37.8),
    _w("directly.", 37.8, 38.9),
    _w("If", 39.9, 40.0), _w("you", 40.0, 40.1), _w("have", 40.1, 40.3), _w("any", 40.3, 40.5),
    _w("questions,", 40.5, 40.9), _w("just", 41.2, 41.4), _w("WhatsApp", 41.4, 41.8), _w("me", 41.8, 41.9),
    _w("directly", 41.9, 42.5), _w("or", 42.5, 42.6), _w("scan", 42.6, 42.9), _w("the", 42.9, 43.0),
    _w("QR", 43.0, 43.3), _w("code.", 43.3, 43.9),
]

NEG_FAR_APART = [
    _w("this", 0.0, 0.3), _w("is", 0.3, 0.5), _w("a", 0.5, 0.6), _w("test", 0.6, 1.0),
    _w("this", 30.0, 30.3), _w("is", 30.3, 30.5), _w("a", 30.5, 30.6), _w("test", 30.6, 31.0),
]


def test_cut_duplicate_phrases_real_bug_1():
    cut = _cut_duplicate_phrases(DUP_TAKE_1, set())
    check("dup-phrase: 剪掉第一次(废弃)出现的 8 个词",
          cut == set(range(8)), sorted(cut))


def test_cut_duplicate_phrases_real_bug_2():
    cut = _cut_duplicate_phrases(DUP_TAKE_2, set())
    check("dup-phrase: 剪掉第一次(废弃)出现的 9 个词",
          cut == set(range(9)), sorted(cut))


def test_cut_duplicate_phrases_negative_far_apart():
    cut = _cut_duplicate_phrases(NEG_FAR_APART, set())
    check("dup-phrase: 间隔 30s(超过 15s 窗口)的重复短语不会被剪", cut == set(), sorted(cut))


def test_cut_duplicate_phrases_too_short_to_check():
    cut = _cut_duplicate_phrases(WORDS, set())  # 只有 5 个词，< _DUP_MIN_NGRAM*2
    check("dup-phrase: 词数太少时直接跳过检测（不误报）", cut == set(), sorted(cut))


def main():
    test_clean_first_pass()
    test_flagged_then_fixed_on_retry()
    test_monotonic_cut_never_resurrected()
    test_best_result_delivered_not_last()
    test_flagged_still_flagged_after_all_retries()
    test_llm_unconfigured()
    test_cut_duplicate_phrases_real_bug_1()
    test_cut_duplicate_phrases_real_bug_2()
    test_cut_duplicate_phrases_negative_far_apart()
    test_cut_duplicate_phrases_too_short_to_check()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All filler-review tests passed.")


if __name__ == "__main__":
    main()

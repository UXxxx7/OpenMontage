# plan_filler_removal 的事后复核单测——不依赖真实 LLM，monkeypatch
# content_planner 自己命名空间里的 call_llm_chat（不是 llm_client.call_llm_chat，
# 因为 content_planner.py 是 `from .llm_client import call_llm_chat`，patch 后者
# 对已绑定的名字没有效果）。
#
# Run: uv run python -m whatsapp_mvp.test_filler_review

from __future__ import annotations

import json

import whatsapp_mvp.content_planner as content_planner
from whatsapp_mvp.content_planner import FILLER_SYSTEM_PROMPT, VERIFY_FILLER_SYSTEM_PROMPT

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

    def fake(system_prompt, user_message, *, temperature=0.1):
        calls.append(system_prompt)
        if _is_filler_call(system_prompt):
            return json.dumps({"cut_word_indices": [1, 3]})
        if _is_verify_call(system_prompt):
            return json.dumps({"clean": True})
        raise AssertionError("unexpected system_prompt")

    content_planner.call_llm_chat = fake
    result = content_planner.plan_filler_removal(WORDS, duration=1.6)
    check("clean-first-pass: 恰好 2 次 LLM 调用（判断+复核）", len(calls) == 2, calls)
    check("clean-first-pass: 结果非空且不重试", result == [
        {"start_seconds": 0.0, "end_seconds": 0.3},
        {"start_seconds": 0.6, "end_seconds": 1.0},
        {"start_seconds": 1.3, "end_seconds": 1.6},
    ], result)


def test_flagged_then_fixed_on_retry():
    calls = []

    def fake(system_prompt, user_message, *, temperature=0.1):
        calls.append(system_prompt)
        if _is_filler_call(system_prompt):
            if "NOTE: a previous pass" in user_message:
                return json.dumps({"cut_word_indices": [1, 3]})  # retry: correct cut
            return json.dumps({"cut_word_indices": [1]})  # first pass: misses "uh"
        if _is_verify_call(system_prompt):
            if "uh" in user_message:
                return json.dumps({"clean": False, "issue": "leftover 'uh' filler word"})
            return json.dumps({"clean": True})
        raise AssertionError("unexpected system_prompt")

    content_planner.call_llm_chat = fake
    result = content_planner.plan_filler_removal(WORDS, duration=1.6)
    check("flagged-then-fixed: 恰好 4 次 LLM 调用（判断+复核+重判+复核）", len(calls) == 4, calls)
    check("flagged-then-fixed: 最终结果反映重试后的正确剪切", result == [
        {"start_seconds": 0.0, "end_seconds": 0.3},
        {"start_seconds": 0.6, "end_seconds": 1.0},
        {"start_seconds": 1.3, "end_seconds": 1.6},
    ], result)


def test_flagged_still_flagged_after_retry():
    calls = []

    def fake(system_prompt, user_message, *, temperature=0.1):
        calls.append(system_prompt)
        if _is_filler_call(system_prompt):
            return json.dumps({"cut_word_indices": [1]})  # never catches "uh", even on retry
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
    check("still-flagged-after-retry: 不抛异常，直接返回重试结果", not raised)
    check("still-flagged-after-retry: 恰好 4 次 LLM 调用，不会无限重试", len(calls) == 4, calls)
    check("still-flagged-after-retry: 返回的是重试后的剪切结果", result is not None and len(result) > 0, result)


def test_llm_unconfigured():
    def fake(system_prompt, user_message, *, temperature=0.1):
        return None

    content_planner.call_llm_chat = fake
    result = content_planner.plan_filler_removal(WORDS, duration=1.6)
    check("LLM 不可用: 返回空列表，不炸", result == [], result)


def main():
    test_clean_first_pass()
    test_flagged_then_fixed_on_retry()
    test_flagged_still_flagged_after_retry()
    test_llm_unconfigured()

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All filler-review tests passed.")


if __name__ == "__main__":
    main()

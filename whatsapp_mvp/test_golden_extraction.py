# WhatsApp MVP - golden extraction regression test
#
# Guards against the two production bugs found in preview(5).mp4:
#   1. A count_up value extracted as 0 instead of the real spoken figure
#      ("$1.5 million" -> "$0.0"), now caught by _zero_value_titles' retry
#      guard in content_planner.plan_content.
#   2. A retake surviving the cut ("I've put the full breakdown in this
#      video..." said twice, only the second take should remain).
#
# Uses a recorded fixture for the "good" LLM response (captured from a real
# live DeepSeek call against this exact transcript — see PROGRESS notes) so
# CI runs fast, deterministic, and free. No live API calls happen here.
#
# Run: uv run python -m whatsapp_mvp.test_golden_extraction

from __future__ import annotations

import json
from unittest import mock

from whatsapp_mvp import content_planner
from whatsapp_mvp.content_planner import plan_content, plan_filler_removal

FAILED = []


def check(name, condition, detail=""):
    print(("PASS" if condition else "FAIL") + f" [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


SEGMENTS = [
    {"start": 10.0, "text": "Your current plan covers you for one point five million dollars in coverage."},
    {"start": 15.0, "text": "And your annual premium is going to increase by twelve percent if you don't renew on time."},
]

# Recorded from a real live DeepSeek call against SEGMENTS above — the model
# got it right this time; frozen here as a regression fixture so we notice if
# the downstream frame-mapping code ever mishandles a correctly-extracted value.
GOOD_RESPONSE = json.dumps({
    "chapters": [],
    "data_points": [{
        "visual": "count_up", "title": "Your Current Coverage", "seconds": 10.0,
        "rows": [{"label": "COVERAGE", "seconds": 10.0, "value": 1.5, "tone": "accent",
                   "prefix": "$", "divideBy": 1.0, "decimals": 1, "unit": "M"}],
    }],
})

# A synthetic "degenerate" response matching the exact observed production bug
# shape: the LLM extracted 0 instead of 1.5. Used to prove the retry guard
# actually recovers, and that it drops the card if the retry still fails.
BAD_RESPONSE = json.dumps({
    "chapters": [],
    "data_points": [{
        "visual": "count_up", "title": "Your Current Coverage", "seconds": 10.0,
        "rows": [{"label": "COVERAGE", "seconds": 10.0, "value": 0, "tone": "accent",
                   "prefix": "$", "divideBy": 1.0, "decimals": 1, "unit": "M"}],
    }],
})


EMPTY_RESPONSE = json.dumps({"chapters": [], "data_points": []})


def _responses(*fixed):
    """Like side_effect=[...] but returns an empty (harmless) plan for any
    extra calls instead of raising StopIteration — e.g. dropping the coverage
    card in test 3 below leaves a 30s video with zero visual events, which
    also triggers content_planner's own richness-floor re-plan call. Falling
    back to empty (rather than repeating the last fixed response) keeps that
    unrelated call from re-injecting the same bad card through a different
    code path and confusing what this test is actually checking."""
    it = iter(fixed)

    def _fn(*_a, **_k):
        return next(it, EMPTY_RESPONSE)
    return _fn


def _coverage_value(plan):
    cards = plan.get("data_cards", [])
    if not cards or not cards[0].get("rows"):
        return "MISSING"
    return cards[0]["rows"][0].get("value")


def main():
    # 1. Good LLM response round-trips correctly through the real downstream
    #    frame-mapping code (locks in today's known-correct extraction shape).
    with mock.patch.object(content_planner, "call_llm_chat", return_value=GOOD_RESPONSE):
        plan = plan_content(SEGMENTS, duration=30.0)
    check("正确提取的 $1.5M 原样通过", _coverage_value(plan) == 1.5, plan.get("data_cards"))

    # 2. Bad response (value=0) on the FIRST call, good response on the RETRY
    #    -> the guard should recover the correct value.
    with mock.patch.object(content_planner, "call_llm_chat", side_effect=_responses(BAD_RESPONSE, GOOD_RESPONSE)):
        plan = plan_content(SEGMENTS, duration=30.0)
    check("0 值触发重试并恢复正确值", _coverage_value(plan) == 1.5, plan.get("data_cards"))

    # 3. Bad response on BOTH the initial call and the retry -> the guard
    #    should drop the card entirely rather than ship a wrong "$0.0". Also
    #    exercises the cascading richness-floor re-plan call (see _responses).
    with mock.patch.object(content_planner, "call_llm_chat", side_effect=_responses(BAD_RESPONSE, BAD_RESPONSE)):
        plan = plan_content(SEGMENTS, duration=30.0)
    check("重试后仍是 0 值 -> 整卡丢弃而非显示 $0.0", _coverage_value(plan) == "MISSING", plan.get("data_cards"))

    # 4. Retake removal: a false start immediately followed by the clean
    #    retake must leave only the second, complete utterance.
    words = []
    t = 0.0
    for phrase, wps in (
        ("I've put the full breakdown in this video", 2.5),
        ("I've put the full breakdown in this video so you can see exactly what's changing before your renewal date", 2.5),
    ):
        for word in phrase.split():
            dur = 1.0 / wps
            words.append({"word": word, "start": t, "end": t + dur})
            t += dur

    cut_response = json.dumps({"cut_word_indices": list(range(8))})  # first 8 words = the false start
    with mock.patch.object(content_planner, "call_llm_chat", return_value=cut_response):
        keep_ranges = plan_filler_removal(words, duration=t)
    from whatsapp_mvp.content_planner import _words_in_keep_ranges
    kept_text = " ".join(w["word"] for w in _words_in_keep_ranges(words, keep_ranges))
    check("重录的第一次尝试被剪掉，只留下完整版本",
          kept_text == "I've put the full breakdown in this video so you can see exactly what's changing before your renewal date",
          kept_text)

    # 5. Keep-range padding: the retained segment's start should be pulled a
    #    little earlier than the raw ASR word-start (choppy-cuts fix — cutting
    #    at the literal word boundary can clip the leading phoneme). The cut
    #    region here is 3.2s wide, so the full pad should apply unclamped.
    raw_start = words[8]["start"]
    check("保留片段起点加了 padding（不是原始词边界）",
          keep_ranges and keep_ranges[0]["start_seconds"] == raw_start - content_planner.FILLER_CUT_PAD_SECONDS,
          f"raw_start={raw_start}, got={keep_ranges[0]['start_seconds'] if keep_ranges else None}")

    # 6. Padding must clamp to half the gap when the cut region is very short,
    #    never re-including any part of a genuinely-cut word.
    from whatsapp_mvp.content_planner import _pad_keep_ranges
    tight_ranges = [{"start_seconds": 1.0, "end_seconds": 2.0}, {"start_seconds": 2.02, "end_seconds": 3.0}]
    _pad_keep_ranges(tight_ranges, duration=5.0)
    check("窄间隙下 padding 钳制，不会侵入相邻被剪片段",
          tight_ranges[0]["end_seconds"] <= tight_ranges[1]["start_seconds"],
          tight_ranges)

    # 7. Multi-retake convergence: reproduces a real production bug found in a
    #    genuine end-to-end run (job_b5f4edfc22b4) — a transcript with THREE
    #    separate leftover retakes, where the initial pass only caught one and
    #    verify_filler_removal's old single-"issue" schema meant the retry
    #    never even saw the other two (all 3 survived into the final cut).
    #    Here the initial pass catches only retake #1; verify reports all 3
    #    remaining issues at once (new plural "issues" schema); the retry
    #    fixes all 3 in one pass using that full feedback.
    def _phrase_words(phrase, start_t, wps=2.5):
        out, t = [], start_t
        for word in phrase.split():
            dur = 1.0 / wps
            out.append({"word": word, "start": t, "end": t + dur})
            t += dur
        return out, t

    words3, t3 = [], 0.0
    false_starts = [
        "Your current plan covers you for",
        "I've put the full breakdown in this video",
        "If you have any questions just WhatsApp me directly",
    ]
    completions = [
        "Your current plan covers you for one and a half million",
        "I've put the full breakdown in this video so you have everything in one place",
        "If you have any questions just WhatsApp me directly or scan the QR code below",
    ]
    false_start_word_counts = []
    for fs, complete in zip(false_starts, completions):
        w, t3 = _phrase_words(fs, t3)
        false_start_word_counts.append(len(w))
        words3.extend(w)
        w, t3 = _phrase_words(complete, t3)
        words3.extend(w)

    # Index ranges of each false start within words3.
    fs_ranges = []
    cursor = 0
    for fs, complete in zip(false_starts, completions):
        n_fs = len(fs.split())
        n_complete = len(complete.split())
        fs_ranges.append(list(range(cursor, cursor + n_fs)))
        cursor += n_fs + n_complete

    initial_cut = json.dumps({"cut_word_indices": fs_ranges[0]})  # only catches retake #1
    verify_multi_issue = json.dumps({
        "clean": False,
        "issues": [
            "Leftover false start before 'I've put the full breakdown in this video so...'",
            "Leftover false start before 'If you have any questions just WhatsApp me directly or...'",
        ],
    })
    retry_cut = json.dumps({"cut_word_indices": fs_ranges[0] + fs_ranges[1] + fs_ranges[2]})  # fixes all 3
    verify_clean = json.dumps({"clean": True})

    with mock.patch.object(content_planner, "call_llm_chat",
                            side_effect=_responses(initial_cut, verify_multi_issue, retry_cut, verify_clean)):
        keep_ranges3 = plan_filler_removal(words3, duration=t3)
    kept_text3 = " ".join(w["word"] for w in _words_in_keep_ranges(words3, keep_ranges3))
    expected3 = " ".join(completions)
    check("3 处遗留重录在一次重试内全部收敛（多 issue 反馈生效）",
          kept_text3 == expected3, kept_text3)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All golden extraction tests passed.")


if __name__ == "__main__":
    main()

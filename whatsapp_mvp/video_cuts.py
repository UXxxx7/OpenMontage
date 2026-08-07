"""Python port of remotion-composer/src/cuts.ts — the SAME cut model, kept in
sync by hand (see test_video_cuts.py's fixture-parity test against the TS
source, and that file's own header comment: "every caller MUST go through
computeOutputDuration... duration is otherwise easy to hand-duplicate and let
drift"). This module exists because the server has its own two places that
need to reason in OUTPUT-frame space despite `render_props` staying in SOURCE-
frame space forever (see cuts.ts's header comment for why): `qa_stills.py`
picks still frames to review, and `pipeline_runner.py` clamps a client's
`videoCuts` edit against the real on-disk source duration before rendering.

Model: `videoCuts` is a list of REMOVED regions, half-open [fromFrame,
toFrame), in the ORIGINAL SOURCE video's own frame numbering. Every other
frame field in render_props stays in that same source-frame space forever;
this module maps it to output-frame space only where a caller specifically
needs to reason about what's actually on screen at a given moment (a QA
still) or about the real playable duration.
"""

from __future__ import annotations

import math
from typing import TypedDict


class VideoCut(TypedDict):
    fromFrame: int
    toFrame: int


def _js_round(x: float) -> int:
    """Python's round() is round-half-to-even; JS's Math.round() rounds .5
    towards +Infinity. Only matters for malformed/adversarial float input
    (real cut frames are always whole numbers), but this keeps the parity
    test exact rather than "usually agrees."""
    return math.floor(x + 0.5)


def normalize_cuts(cuts: list[dict] | None, src_len: int) -> list[VideoCut]:
    """Sort, clamp to [0, src_len], drop empty/invalid entries, and merge
    overlapping AND touching cuts. Mirrors cuts.ts's normalizeCuts exactly,
    including merging touching (not just overlapping) cuts — a zero-length
    kept segment between two touching cuts is invalid on the Remotion side."""
    if not cuts:
        return []
    clamped: list[VideoCut] = []
    for c in cuts:
        if not isinstance(c, dict):
            continue
        raw_from, raw_to = c.get("fromFrame"), c.get("toFrame")
        if not isinstance(raw_from, (int, float)) or not isinstance(raw_to, (int, float)):
            continue
        frm = max(0, min(_js_round(raw_from), src_len))
        to = max(0, min(_js_round(raw_to), src_len))
        if to > frm:
            clamped.append({"fromFrame": frm, "toFrame": to})
    if not clamped:
        return []
    clamped.sort(key=lambda c: c["fromFrame"])
    out: list[VideoCut] = [dict(clamped[0])]
    for cur in clamped[1:]:
        prev = out[-1]
        if cur["fromFrame"] <= prev["toFrame"]:
            prev["toFrame"] = max(prev["toFrame"], cur["toFrame"])
        else:
            out.append(dict(cur))
    return out


def total_cut_frames(cuts: list[VideoCut]) -> int:
    return sum(c["toFrame"] - c["fromFrame"] for c in cuts)


def source_to_output(f: int, cuts: list[VideoCut]) -> int:
    """SOURCE frame -> OUTPUT frame. `cuts` MUST already be normalized."""
    removed = 0
    for c in cuts:
        if c["fromFrame"] >= f:
            break
        removed += min(c["toFrame"], f) - c["fromFrame"]
    return f - removed


def output_to_source(o: int, cuts: list[VideoCut]) -> int:
    """OUTPUT frame -> SOURCE frame. Exact right-inverse of source_to_output."""
    removed = 0
    for c in cuts:
        splice_out = c["fromFrame"] - removed
        if o < splice_out:
            return o + removed
        removed += c["toFrame"] - c["fromFrame"]
    return o + removed


class KeptSegment(TypedDict):
    srcFrom: int
    srcTo: int
    outFrom: int


def kept_segments(cuts: list[VideoCut], src_len: int) -> list[KeptSegment]:
    segs: list[KeptSegment] = []
    cursor = 0
    removed = 0
    for c in cuts:
        if c["fromFrame"] > cursor:
            segs.append({"srcFrom": cursor, "srcTo": c["fromFrame"], "outFrom": cursor - removed})
        removed += c["toFrame"] - c["fromFrame"]
        cursor = c["toFrame"]
    if src_len > cursor:
        segs.append({"srcFrom": cursor, "srcTo": src_len, "outFrom": cursor - removed})
    return segs


def compute_output_duration(props: dict, fps: int = 30) -> int:
    """The single source of truth for the composition's OUTPUT duration on
    the Python side — mirrors cuts.ts's computeOutputDuration exactly."""
    duration_seconds = props.get("durationSeconds")
    try:
        duration_seconds = float(duration_seconds) if duration_seconds else 1.0
    except (TypeError, ValueError):
        duration_seconds = 1.0
    src_len = max(1, math.ceil(duration_seconds * fps))
    raw_cuts = props.get("videoCuts")
    cuts = normalize_cuts(raw_cuts if isinstance(raw_cuts, list) else None, src_len)
    return max(1, src_len - total_cut_frames(cuts))

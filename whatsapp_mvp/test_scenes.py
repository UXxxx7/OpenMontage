# Beat-driven scenes generation + data-card safe-zone placement unit tests.
#
# The core promise under test: the speaker card only shrinks when a data card
# needs the canvas, returns to full when it doesn't, and nothing is ever
# placed where the chrome (nav / captions / brand bar) lives. This is the
# "adapt to the video's own content" mechanism — a video with no numbers gets
# a full-screen card the whole way through, a data-dense one gets docked
# intervals synchronized to its beats.
#
# Run: uv run python -m whatsapp_mvp.test_scenes

from __future__ import annotations

from whatsapp_mvp.pipeline_runner import (
    _CAPTION_SAFE_TOP,
    _CONTENT_BOX,
    _CONTENT_TOP,
    _FULL_BOX,
    _TRANSITION_FRAMES,
    build_xiaojin_scenes,
    place_data_cards,
)

FAILED = []


def check(name: str, condition: bool, detail=""):
    print(("PASS" if condition else "FAIL") + f" [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def _box_at(scenes, key):
    return [(s["frame"], s[key]) for s in scenes]


def main():
    # --- 1. No data cards -> single full-screen keyframe, card never moves.
    scenes = build_xiaojin_scenes({"chapters": [], "dataCards": []}, duration_seconds=30)
    check("no dataCards -> single FULL keyframe", scenes == [{"frame": 0, **_FULL_BOX}], scenes)

    # --- 2. One card mid-video -> full, dock 20f before mount, hold, return.
    plan = {"dataCards": [{"title": "T", "mountFrame": 300, "rows": [{"label": "A", "value": 1, "mountOffset": 50}]}]}
    scenes = build_xiaojin_scenes(plan, duration_seconds=60)  # 1800 frames
    frames = [s["frame"] for s in scenes]
    check("frames strictly increasing (Remotion interpolate requirement)",
          all(a < b for a, b in zip(frames, frames[1:])), frames)
    check("starts FULL at 0", scenes[0] == {"frame": 0, **_FULL_BOX})
    check("dock transition starts 20f before mountFrame",
          scenes[1]["frame"] == 300 - _TRANSITION_FRAMES and scenes[1]["h"] == _FULL_BOX["h"], scenes[1])
    check("docked to CONTENT box at mountFrame", scenes[2] == {"frame": 300, **_CONTENT_BOX}, scenes[2])
    check("returns to FULL after hold", scenes[-1]["h"] == _FULL_BOX["h"] and scenes[-1]["frame"] > 300, scenes[-1])

    # --- 3. Two cards close together -> one merged docked interval (no thrash).
    plan = {"dataCards": [
        {"title": "A", "mountFrame": 300, "rows": [{"label": "a", "value": 1, "mountOffset": 50}]},
        {"title": "B", "mountFrame": 560, "rows": [{"label": "b", "value": 2, "mountOffset": 50}]},
    ]}
    scenes = build_xiaojin_scenes(plan, duration_seconds=60)
    content_entries = [s for s in scenes if s["h"] == _CONTENT_BOX["h"]]
    check("close cards merge into one docked interval (2 CONTENT keyframes, not 4)",
          len(content_entries) == 2, scenes)

    # --- 4. Two cards far apart -> two separate docked intervals.
    plan = {"dataCards": [
        {"title": "A", "mountFrame": 300, "rows": [{"label": "a", "value": 1, "mountOffset": 50}]},
        {"title": "B", "mountFrame": 1200, "rows": [{"label": "b", "value": 2, "mountOffset": 50}]},
    ]}
    scenes = build_xiaojin_scenes(plan, duration_seconds=60)
    content_entries = [s for s in scenes if s["h"] == _CONTENT_BOX["h"]]
    check("distant cards get two separate docked intervals (4 CONTENT keyframes)",
          len(content_entries) == 4, scenes)
    frames = [s["frame"] for s in scenes]
    check("two-interval frames still strictly increasing", all(a < b for a, b in zip(frames, frames[1:])), frames)

    # --- 5. Card mounting near frame 0 doesn't produce frame<=0 or non-increasing keys.
    plan = {"dataCards": [{"title": "T", "mountFrame": 5, "rows": [{"label": "a", "value": 1}]}]}
    scenes = build_xiaojin_scenes(plan, duration_seconds=60)
    frames = [s["frame"] for s in scenes]
    check("early card: strictly increasing from 0", frames[0] == 0 and all(a < b for a, b in zip(frames, frames[1:])), frames)

    # --- 6. Card near the end -> video ends docked (no truncated return-transition).
    plan = {"dataCards": [{"title": "T", "mountFrame": 1700, "rows": [{"label": "a", "value": 1, "mountOffset": 50}]}]}
    scenes = build_xiaojin_scenes(plan, duration_seconds=60)  # ends at 1800
    check("card near end: stays docked, last keyframe is CONTENT",
          scenes[-1]["h"] == _CONTENT_BOX["h"], scenes)
    check("card near end: no keyframe beyond duration", scenes[-1]["frame"] <= 1800, scenes[-1])

    # --- 7. Every scene box stays inside chrome-safe canvas (nav 88 / brand 1848).
    for s in scenes + build_xiaojin_scenes({"dataCards": []}, 30):
        ok = s["y"] >= 88 and s["y"] + s["h"] <= 1848 and s["x"] >= 0 and s["x"] + s["w"] <= 1080
        if not ok:
            check("scene box inside chrome-safe canvas", False, s)
            break
    else:
        check("scene box inside chrome-safe canvas", True)

    # --- 8. place_data_cards: default placement lands in the content zone,
    # below the docked card, above the caption band.
    placed = place_data_cards([{"title": "T", "mountFrame": 100, "rows": [{"label": "a", "value": 1}] * 3}])
    c = placed[0]
    est_h = 96 + 3 * 64
    check("dataCard default y in content zone", c["y"] >= _CONTENT_TOP, c)
    check("dataCard bottom clear of caption band", c["y"] + est_h <= _CAPTION_SAFE_TOP, c)
    check("dataCard default x/width set", c["x"] == 80 and c["width"] == 920, c)

    # --- 9. place_data_cards: an unsafe explicit y (schema default 900 —
    # overlaps the docked card) gets clamped into the safe zone.
    placed = place_data_cards([{"title": "T", "y": 900, "mountFrame": 100, "rows": [{"label": "a", "value": 1}]}])
    check("y=900 (under docked card) clamped down to content zone", placed[0]["y"] >= _CONTENT_TOP, placed[0])

    placed = place_data_cards([{"title": "T", "y": 1800, "mountFrame": 100, "rows": [{"label": "a", "value": 1}] * 6}])
    est_h = 96 + 6 * 64
    check("y=1800 (in caption band) clamped up", placed[0]["y"] + est_h <= _CAPTION_SAFE_TOP, placed[0])

    # --- 10. place_data_cards is pure — input not mutated.
    original = [{"title": "T", "mountFrame": 100, "rows": [{"label": "a", "value": 1}]}]
    place_data_cards(original)
    check("input list not mutated", "y" not in original[0], original[0])

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All scenes/safe-zone unit tests passed.")


if __name__ == "__main__":
    main()

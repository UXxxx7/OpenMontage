# P2 Task 5 — unit tests for content_planner's field-mapping logic
# (_to_frame_plan), independent of the live LLM call. Covers the contract-②
# field renames (at_seconds->atFrame, info_cards->dataCards), the "no
# notable numbers" fallback (empty data_points is a normal outcome, not an
# error), malformed-input tolerance, and chapter ordering.
#
# Run: uv run python -m whatsapp_mvp.test_content_planner

from __future__ import annotations

from whatsapp_mvp.content_planner import FPS, MOUNT_LEAD_FRAMES, _to_frame_plan

FAILED = []


def check(name: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"{status} [{name}]" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def main():
    # --- 1. Basic chapter field rename: at_seconds -> atFrame ---
    plan = _to_frame_plan({"chapters": [{"at_seconds": 2.0, "label": "Intro"}]}, duration=10.0)
    check("chapter at_seconds -> atFrame", plan["chapters"] == [{"atFrame": 60, "label": "Intro"}], plan["chapters"])
    check("no mode_schedule key in output (Xiaojin doesn't use it)", "mode_schedule" not in plan)
    check("dataCards key present (not info_cards)", "dataCards" in plan and "info_cards" not in plan)

    # --- 2. Chapters get sorted by atFrame even if the LLM returns them out of order ---
    plan = _to_frame_plan({
        "chapters": [{"at_seconds": 5.0, "label": "B"}, {"at_seconds": 1.0, "label": "A"}],
    }, duration=10.0)
    check("chapters sorted ascending", [c["label"] for c in plan["chapters"]] == ["A", "B"], plan["chapters"])

    # --- 3. "No notable numbers" is a normal, common outcome — not an error ---
    plan = _to_frame_plan({"chapters": [{"at_seconds": 0, "label": "Talk"}], "data_points": []}, duration=30.0)
    check("empty data_points -> empty dataCards (not an error)", plan["dataCards"] == [])

    plan_missing_key = _to_frame_plan({"chapters": []}, duration=30.0)  # no data_points key at all
    check("missing data_points key -> empty dataCards", plan_missing_key["dataCards"] == [])

    # --- 4. Data point field mapping: rows keep label/value/tone/prefix/etc,
    # mountFrame = min(row seconds)*FPS - MOUNT_LEAD_FRAMES, mountOffset is
    # relative to the card's own mountFrame (not absolute). ---
    plan = _to_frame_plan({
        "chapters": [],
        "data_points": [{
            "title": "Before/After",
            "rows": [
                {"label": "BEFORE", "seconds": 10.0, "value": 100, "tone": "bad"},
                {"label": "AFTER", "seconds": 12.0, "value": 200, "prefix": "$", "divideBy": 100, "decimals": 1, "unit": "x", "tone": "good"},
            ],
        }],
    }, duration=30.0)
    card = plan["dataCards"][0]
    expected_mount = round(10.0 * FPS) - MOUNT_LEAD_FRAMES
    check("dataCard mountFrame = first row's frame - lead", card["mountFrame"] == expected_mount, card)
    row0, row1 = card["rows"]
    # mountOffset is relative to the CARD's mountFrame (which is already
    # MOUNT_LEAD_FRAMES before the earliest row) — so even the earliest row's
    # offset equals MOUNT_LEAD_FRAMES, not 0. That's by design: the card
    # appears MOUNT_LEAD_FRAMES early, and the count-up itself starts once
    # mountOffset frames have passed, landing exactly on the spoken beat.
    check("row0 mountOffset == MOUNT_LEAD_FRAMES (card already appeared early)", row0["mountOffset"] == MOUNT_LEAD_FRAMES, row0)
    expected_offset = round(12.0 * FPS) - expected_mount
    check("row1 mountOffset relative to card's mountFrame, not row0", row1["mountOffset"] == expected_offset, row1)
    check("row1 carries prefix/divideBy/decimals/unit through", (
        row1.get("prefix") == "$" and row1.get("divideBy") == 100 and row1.get("decimals") == 1 and row1.get("unit") == "x"
    ), row1)
    check("row without prefix doesn't get a spurious prefix key", "prefix" not in row0, row0)

    # --- 5. Rows with no real numeric value are dropped (dataCard rows are
    # count-up only) rather than rendering a broken/undefined count-up. ---
    plan = _to_frame_plan({
        "chapters": [],
        "data_points": [{
            "title": "Bad row",
            "rows": [{"label": "NO VALUE", "seconds": 5.0, "value": "not-a-number"}],
        }],
    }, duration=30.0)
    check("data point with no valid numeric rows is dropped entirely", plan["dataCards"] == [])

    # --- 6. Malformed chapter/data_point entries are skipped, not fatal. ---
    plan = _to_frame_plan({
        "chapters": [{"label": "missing at_seconds"}, {"at_seconds": 3.0, "label": "OK"}],
        "data_points": ["not even a dict", {"title": "no rows key"}],
    }, duration=30.0)
    check("malformed chapter entry skipped, valid one kept", plan["chapters"] == [{"atFrame": 90, "label": "OK"}], plan["chapters"])
    check("malformed data_points entries don't crash, produce no cards", plan["dataCards"] == [])

    # --- 7. Tone falls back to "normal" for an invalid/missing tone value. ---
    plan = _to_frame_plan({
        "chapters": [], "data_points": [{"title": "T", "rows": [{"label": "L", "seconds": 1.0, "value": 1, "tone": "not-a-real-tone"}]}],
    }, duration=10.0)
    check("invalid tone falls back to normal", plan["dataCards"][0]["rows"][0]["tone"] == "normal")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        raise SystemExit(1)
    print("All content_planner field-mapping unit tests passed.")


if __name__ == "__main__":
    main()

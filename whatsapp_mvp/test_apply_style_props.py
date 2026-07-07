# P2 Task 2 acceptance test — build_xiaojin_render_props() output must validate
# against contracts/render_props.schema.json for a range of inputs, independent
# of whether P3's XiaojinEditorial can actually render it yet.
#
# Run: uv run python -m whatsapp_mvp.test_apply_style_props

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from whatsapp_mvp.pipeline_runner import (
    apply_style_params_to_op,
    build_xiaojin_render_props,
    resolve_reframe_op,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "render_props.schema.json"


def _validator():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _assert_valid(name: str, props: dict, validator: jsonschema.Draft202012Validator):
    errors = sorted(validator.iter_errors(props), key=lambda e: e.path)
    if errors:
        print(f"FAIL [{name}]")
        for e in errors:
            print(f"  - {list(e.path)}: {e.message}")
        raise SystemExit(1)
    print(f"PASS [{name}]")


def main():
    validator = _validator()

    # Case 1: minimal — no dataCards/compliance/brand/intro/outro at all.
    # Contract's README requires the renderer to still produce a
    # plain-but-correct result from required fields alone.
    minimal = build_xiaojin_render_props(
        video_src_rel="jobs/case-minimal/source.mp4",
        duration_seconds=12.5,
        captions=[{"text": "Hello world.", "startMs": 0, "endMs": 1200}],
        content_plan={"chapters": [], "dataCards": []},
        op={},
    )
    _assert_valid("minimal (no optional fields)", minimal, validator)

    # Case 2: full — matches the shape of a real apply_style call with
    # chapters + dataCards from content_planner, plus compliance/brand/intro.
    full = build_xiaojin_render_props(
        video_src_rel="jobs/case-full/source.mp4",
        duration_seconds=64.55,
        captions=[
            {"text": "Hi there, it's David from Pacific Life.", "startMs": 100, "endMs": 3000},
            {"text": "Your policy covers you for 1.5 million.", "startMs": 3000, "endMs": 7000},
        ],
        content_plan={
            "chapters": [{"atFrame": 0, "label": "INTRO"}, {"atFrame": 300, "label": "DETAILS"}],
            "dataCards": [
                {
                    "title": "Policy details",
                    "x": 80, "y": 900, "width": 920,
                    "mountFrame": 200,
                    "rows": [
                        {"label": "COVERAGE", "value": 1500000, "prefix": "$", "divideBy": 1000000, "decimals": 1, "unit": "M", "tone": "good", "mountOffset": 0},
                    ],
                }
            ],
        },
        op={
            "colorMode": "warm",
            "compliance": {"agentNameZh": "大卫", "agentNameEn": "David", "titleZh": "保险顾问", "licenseNo": "LIC-000000", "insurer": "Pacific Life"},
            "intro": {"eyebrow": "POLICY RENEWAL REMINDER", "title": "保單續期提醒", "subtitle": "Pacific Life 太平洋人壽"},
        },
    )
    _assert_valid("full (chapters + dataCards + compliance + intro)", full, validator)

    # Case 3: brand instead of compliance (mutually exclusive per schema comment
    # — schema itself doesn't enforce this, but check we don't emit both).
    branded = build_xiaojin_render_props(
        video_src_rel="jobs/case-brand/source.mp4",
        duration_seconds=30.0,
        captions=[{"text": "Some content.", "startMs": 0, "endMs": 2000}],
        content_plan={"chapters": [], "dataCards": []},
        op={"brand": {"company": "MRBEAST", "label": "Style Study"}},
    )
    _assert_valid("brand (non-regulated, no compliance)", branded, validator)
    assert "compliance" not in branded, "brand case should not also carry compliance"
    assert "brand" in branded

    print("\nAll P2 Task 2 acceptance cases passed schema validation.")

    # --- Task 4b acceptance: reference_analyzer's style_params actually
    # drives build_xiaojin_render_props (a warm example -> warm output;
    # explicit op.colorMode still wins over the example if both given). ---
    warm_style = {"colorMode": "warm", "aspect": "portrait"}
    dark_style = {"colorMode": "dark", "aspect": "portrait"}

    op_from_warm_example = apply_style_params_to_op({}, warm_style)
    props_warm = build_xiaojin_render_props(
        "jobs/case-warm/source.mp4", 20.0,
        [{"text": "x", "startMs": 0, "endMs": 1000}],
        {"chapters": [], "dataCards": []}, op_from_warm_example,
    )
    assert props_warm["colorMode"] == "warm", props_warm["colorMode"]
    print("PASS [warm example -> colorMode=warm in output props]")

    op_from_dark_example = apply_style_params_to_op({}, dark_style)
    props_dark = build_xiaojin_render_props(
        "jobs/case-dark/source.mp4", 20.0,
        [{"text": "x", "startMs": 0, "endMs": 1000}],
        {"chapters": [], "dataCards": []}, op_from_dark_example,
    )
    assert props_dark["colorMode"] == "dark", props_dark["colorMode"]
    print("PASS [dark example -> colorMode=dark in output props]")

    # explicit op.colorMode overrides the example's judgment
    op_explicit_wins = apply_style_params_to_op({"colorMode": "dark"}, warm_style)
    assert op_explicit_wins["colorMode"] == "dark"
    print("PASS [explicit op.colorMode overrides example's colorMode]")

    # --- reframe trigger: portrait example + landscape source -> reframe op;
    # matching aspect -> no reframe. ---
    reframe = resolve_reframe_op(warm_style, source_aspect="landscape")
    assert reframe == {"type": "reframe", "aspect": "portrait", "description": "按示例视频画幅转成 portrait"}, reframe
    print(f"PASS [portrait example + landscape source -> {reframe}]")

    no_reframe = resolve_reframe_op(warm_style, source_aspect="portrait")
    assert no_reframe is None, no_reframe
    print("PASS [portrait example + portrait source -> no reframe needed]")

    # --- Beat-driven scenes: props with dataCards get a multi-keyframe scene
    # schedule (card docks for the card, returns after) and still pass the
    # contract schema; a plan with no cards keeps a single static keyframe.
    with_cards = build_xiaojin_render_props(
        "jobs/case-scenes/source.mp4", 60.0,
        [{"text": "x", "startMs": 0, "endMs": 1000}],
        {"chapters": [], "dataCards": [
            {"title": "T", "mountFrame": 300, "rows": [{"label": "A", "value": 9, "mountOffset": 50}]},
        ]}, {},
    )
    _assert_valid("beat-driven scenes (with dataCards)", with_cards, validator)
    assert len(with_cards["scenes"]) >= 4, with_cards["scenes"]
    assert with_cards["dataCards"][0]["y"] >= 1044, with_cards["dataCards"][0]
    print(f"PASS [dataCards -> {len(with_cards['scenes'])} scene keyframes, card placed in content zone]")

    without_cards = build_xiaojin_render_props(
        "jobs/case-noscenes/source.mp4", 60.0,
        [{"text": "x", "startMs": 0, "endMs": 1000}],
        {"chapters": [], "dataCards": []}, {},
    )
    _assert_valid("static full-screen scenes (no dataCards)", without_cards, validator)
    assert len(without_cards["scenes"]) == 1, without_cards["scenes"]
    print("PASS [no dataCards -> single full-screen scene keyframe]")


if __name__ == "__main__":
    main()

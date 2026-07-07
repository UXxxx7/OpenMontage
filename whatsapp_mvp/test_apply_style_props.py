# P2 Task 2 acceptance test — build_xiaojin_render_props() output must validate
# against contracts/render_props.schema.json for a range of inputs, independent
# of whether P3's XiaojinEditorial can actually render it yet.
#
# Run: uv run python -m whatsapp_mvp.test_apply_style_props

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from whatsapp_mvp.pipeline_runner import build_xiaojin_render_props

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


if __name__ == "__main__":
    main()

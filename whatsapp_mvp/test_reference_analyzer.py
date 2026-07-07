# P2 Task 3 acceptance test — reference_analyzer output must validate against
# contracts/style_params.schema.json, and correctly classify a known-warm
# sample (cream xiaojin-editorial background) as colorMode="warm" with a
# warm-toned palette.
#
# Run: uv run python -m whatsapp_mvp.test_reference_analyzer <path-to-warm-sample.mp4>

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

from whatsapp_mvp.reference_analyzer import analyze_reference

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "style_params.schema.json"


def _is_warm_hex(hex_color: str) -> bool:
    """Loose check: warm hex means R channel >= B channel (leans red/orange/yellow)."""
    h = hex_color.lstrip("#")
    r, _, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r >= b


def main():
    if len(sys.argv) < 2:
        print("usage: test_reference_analyzer.py <warm-sample-video-or-image>")
        raise SystemExit(1)

    sample_path = sys.argv[1]
    workdir = Path("/tmp/test_reference_analyzer")
    workdir.mkdir(parents=True, exist_ok=True)

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    params = analyze_reference(sample_path, workdir)
    print(json.dumps(params, ensure_ascii=False, indent=2))

    errors = sorted(validator.iter_errors(params), key=lambda e: e.path)
    if errors:
        print("\nFAIL — schema validation errors:")
        for e in errors:
            print(f"  - {list(e.path)}: {e.message}")
        raise SystemExit(1)
    print("\nPASS — validates against style_params.schema.json")

    assert params.get("colorMode") == "warm", f"expected warm, got {params.get('colorMode')}"
    bg = params.get("palette", {}).get("background")
    assert bg and _is_warm_hex(bg), f"expected a warm-toned background hex, got {bg}"
    print(f"PASS — colorMode=warm, background={bg} (warm-toned)")


if __name__ == "__main__":
    main()

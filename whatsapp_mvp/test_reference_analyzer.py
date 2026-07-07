# P2 Task 3 acceptance test — reference_analyzer output must validate against
# contracts/style_params.schema.json, correctly classify a known-warm sample
# (cream xiaojin-editorial background) as colorMode="warm", AND correctly
# classify a known-dark sample as colorMode="dark" (this branch was flagged
# as untested in the first P2 round — no dark xiaojin sample was at hand, so
# this test synthesizes one from the actual dark theme token instead of
# leaving the branch unverified).
#
# Run: uv run python -m whatsapp_mvp.test_reference_analyzer [optional-real-warm-sample]

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
from PIL import Image

from whatsapp_mvp.reference_analyzer import analyze_reference

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "style_params.schema.json"
WORKDIR = Path("/tmp/test_reference_analyzer")

# Actual theme tokens from remotion-composer's xiaojin/postxhs theme.ts —
# using the real values (not arbitrary "light gray" / "dark gray") makes this
# test double as a check that the analyzer's warm/dark threshold agrees with
# the template's own two documented modes, not just "light vs dark" in the
# abstract.
_WARM_CREAM = (0xF2, 0xEB, 0xE0)   # #F2EBE0
_DARK_BG = (0x0D, 0x11, 0x17)      # #0D1117


def _is_warm_hex(hex_color: str) -> bool:
    """Loose check: warm hex means R channel >= B channel (leans red/orange/yellow)."""
    h = hex_color.lstrip("#")
    r, _, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r >= b


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _check(name: str, sample_path: Path, expect_color_mode: str, validator: jsonschema.Draft202012Validator):
    params = analyze_reference(str(sample_path), WORKDIR)
    errors = sorted(validator.iter_errors(params), key=lambda e: e.path)
    if errors:
        print(f"FAIL [{name}] schema validation errors:")
        for e in errors:
            print(f"  - {list(e.path)}: {e.message}")
        raise SystemExit(1)

    got = params.get("colorMode")
    if got != expect_color_mode:
        print(f"FAIL [{name}] expected colorMode={expect_color_mode}, got {got}\n{json.dumps(params, indent=2)}")
        raise SystemExit(1)

    print(f"PASS [{name}] colorMode={got}, background={params.get('palette', {}).get('background')}")
    return params


def main():
    WORKDIR.mkdir(parents=True, exist_ok=True)
    validator = _validator()

    # Self-contained: synthesize both a warm-cream and a dark sample from the
    # template's actual theme tokens, so this test doesn't depend on any
    # external file existing (and specifically covers the previously-untested
    # dark branch).
    warm_synthetic = WORKDIR / "synthetic_warm.png"
    Image.new("RGB", (1080, 1920), _WARM_CREAM).save(warm_synthetic)
    _check("synthetic warm (xiaojin cream token)", warm_synthetic, "warm", validator)

    dark_synthetic = WORKDIR / "synthetic_dark.png"
    Image.new("RGB", (1080, 1920), _DARK_BG).save(dark_synthetic)
    dark_params = _check("synthetic dark (xiaojin dark token)", dark_synthetic, "dark", validator)
    assert dark_params["palette"]["background"] == "#0D1117", dark_params["palette"]["background"]

    # Optional: a real sample passed on the command line gets the fuller check
    # (warm-hex sanity check on top of the schema/colorMode check above).
    if len(sys.argv) > 1:
        real_params = _check("real sample (CLI arg)", Path(sys.argv[1]), "warm", validator)
        bg = real_params.get("palette", {}).get("background")
        assert bg and _is_warm_hex(bg), f"expected a warm-toned background hex, got {bg}"
        print(f"PASS [real sample] background {bg} is warm-toned")

    print("\nAll P2 Task 3 acceptance cases passed (including the previously-untested dark branch).")


if __name__ == "__main__":
    main()

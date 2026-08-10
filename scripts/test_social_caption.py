"""Manual verification script for whatsapp_mvp/social_caption.py.

Not a pytest suite — this repo has no pytest precedent for LLM-prose output
(content_planner.py/croll_script.py aren't unit-tested either), so this is a
human-in-the-loop review tool: run it, read the output, judge it against the
rubric in the plan before wiring the module into the live bot.

Usage:
    .venv/Scripts/python.exe scripts/test_social_caption.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp_mvp.social_caption import generate_caption  # noqa: E402
from whatsapp_mvp.config import get_config  # noqa: E402

# Real, already-PREVIEW_READY job fixtures in storage/jobs/, picked to exercise
# both language branches on genuinely different source content.
FIXTURES = [
    ("job_2dd37e39dfa6", None, "zh — Cantonese-adjacent (Traditional-tagged)"),
    ("job_4e8504467ab6", None, "zh — Cantonese-adjacent (Traditional-tagged)"),
    ("job_51f154a80f9b", None, "zh — Cantonese-adjacent (Traditional-tagged)"),
    ("job_0c77b77ffddc", "please add subtitles", "en — insurance agent renewal reminder"),
    ("job_05d8b811e662", "please add subtitles", "en — insurance agent renewal reminder"),
]

_BLOCKLIST_ZH = [
    "在這個瞬息萬變的時代", "在这个瞬息万变的时代", "你知唔知道", "你有無諗過",
    "你有沒有想過", "立即聯繫我了解更多", "立即联系我了解更多", "人生無常，保障先行",
]
_BLOCKLIST_EN = [
    "in today's fast-paced world", "let's dive in", "have you ever wondered",
    "contact me today to learn more", "3 things you need to know",
]

# Cheap, non-exhaustive Simplified-character spot-check — a handful of chars whose
# Traditional counterpart is visually distinct and common enough in casual writing
# that seeing the Simplified form is a real signal, not a false-positive-prone one.
_SIMPLIFIED_SPOT_CHECK = ["个", "们", "说", "这", "来", "时", "没", "还", "从"]


def _rubric_check(caption: str, lang: str) -> list[str]:
    flags = []
    blocklist = _BLOCKLIST_ZH if lang == "zh" else _BLOCKLIST_EN
    hits = [p for p in blocklist if p.lower() in caption.lower()]
    if hits:
        flags.append(f"BLOCKLIST HIT: {hits}")
    if lang == "zh":
        simp_hits = [c for c in _SIMPLIFIED_SPOT_CHECK if c in caption]
        if simp_hits:
            flags.append(f"POSSIBLE SIMPLIFIED CHARS: {simp_hits}")
    if caption.strip().startswith(("This video", "这段视频", "這段視頻", "本片")):
        flags.append("READS LIKE A SUMMARY, NOT A POST")
    return flags


def main() -> None:
    config = get_config()
    jobs_dir = config.jobs_dir
    print(f"jobs_dir = {jobs_dir}\n")

    for job_id, edit_request, note in FIXTURES:
        job_dir = jobs_dir / job_id
        print("=" * 70)
        print(f"{job_id}  ({note})")
        print(f"edit_request = {edit_request!r}")
        if not job_dir.exists():
            print("  !! job_dir does not exist, skipping")
            continue

        result = generate_caption(job_dir, edit_request)
        if result is None:
            print("  generate_caption() returned None (see warnings above)")
            continue

        print(f"  lang: {result['lang']}")
        print("  caption:")
        for line in result["caption"].split("\n"):
            print(f"    {line}")
        print(f"  hashtags: {' '.join(result['hashtags'])}")

        flags = _rubric_check(result["caption"], result["lang"])
        if flags:
            print(f"  RUBRIC FLAGS: {flags}")
        else:
            print("  rubric: no automated flags (still needs human judgment on tone/register)")
        print()


if __name__ == "__main__":
    main()

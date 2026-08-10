"""Manual verification for the caption content edits in whatsapp_mvp/social_batch.py.

Not a pytest suite (no precedent for LLM-prose output in this repo, same as
scripts/test_social_caption.py). Run, read, judge against the rubric.

Usage:
    .venv/Scripts/python.exe scripts/test_social_batch_caption.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp_mvp.social_batch import generate_social_caption, PLATFORM_SPECS  # noqa: E402

PHOTO = r"C:\Users\Marvelle\AppData\Local\Temp\social_batch_test\test_photo.jpg"
HINTS = {
    "zh": "呢個月保單續保提醒",
    "en": "reminder that policies are due for renewal this month",
}

_BLOCKLIST_ZH = ["在這個瞬息萬變的時代", "在这个瞬息万变的时代", "你知唔知道", "你有無諗過",
                 "你有沒有想過", "立即聯繫我了解更多", "立即联系我了解更多"]
_BLOCKLIST_EN = ["in today's fast-paced world", "let's dive in", "have you ever wondered",
                 "contact me today to learn more", "3 things you need to know"]
_SIMPLIFIED_SPOT_CHECK = ["个", "们", "说", "这", "来", "时", "没", "还", "从"]


def rubric_check(caption: str, lang: str) -> list[str]:
    flags = []
    blocklist = _BLOCKLIST_ZH if lang == "zh" else _BLOCKLIST_EN
    hits = [p for p in blocklist if p.lower() in caption.lower()]
    if hits:
        flags.append(f"BLOCKLIST HIT: {hits}")
    if "\u2014" in caption:
        flags.append("EM DASH PRESENT")
    if lang == "zh":
        simp_hits = [c for c in _SIMPLIFIED_SPOT_CHECK if c in caption]
        if simp_hits:
            flags.append(f"POSSIBLE SIMPLIFIED CHARS: {simp_hits}")
    if "艺人" in caption or "artist" in caption.lower() or "tour" in caption.lower() or "backstage" in caption.lower():
        flags.append("STALE 'ARTIST/TOUR' ASSUMPTION LEAKED INTO OUTPUT")
    return flags


def main() -> None:
    for spec in PLATFORM_SPECS:
        platform = spec["platform"]
        for lang, hint in HINTS.items():
            print("=" * 70)
            print(f"{platform}  lang={lang}  hint={hint!r}")
            result = generate_social_caption(PHOTO, platform, lang=lang, hint=hint)
            if result is None:
                print("  generate_social_caption() returned None")
                continue
            print("  caption:")
            for line in result["caption"].split("\n"):
                print(f"    {line}")
            print("  hashtags:", " ".join(result["hashtags"]))
            flags = rubric_check(result["caption"], lang)
            print("  RUBRIC FLAGS:", flags or "none")
            print()


if __name__ == "__main__":
    main()

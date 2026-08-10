# WhatsApp MVP - Standalone verification for clip_factory.py's selection logic.
#
# Not wired into the bot, no DB/Job touched. Point it at a real video, read
# the report, judge whether the selection/ranking is actually good before
# writing a single line of the DB schema / worker / webhook / Node wiring
# that depends on it — same discipline as scripts/test_social_caption.py
# earlier today.
#
# Usage:
#   .venv/Scripts/python.exe -m whatsapp_mvp.verify_clip_selection <video-path>

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp_mvp import clip_factory  # noqa: E402
from whatsapp_mvp.pipeline_runner import transcribe_segments  # noqa: E402


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float(r.stdout.strip())


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: verify_clip_selection.py <video-path>")
        sys.exit(1)

    video_path = Path(sys.argv[1]).resolve()
    if not video_path.exists():
        print(f"not found: {video_path}")
        sys.exit(1)

    workdir = video_path.parent / f"_verify_{video_path.stem}"
    workdir.mkdir(parents=True, exist_ok=True)

    duration = _probe_duration(video_path)
    print(f"source: {video_path.name}  duration={duration:.0f}s (~{duration/60:.1f}min)")

    print("transcribing (or reusing cache if this has run before)...")
    t0 = time.time()
    segments = transcribe_segments(str(video_path), workdir)
    print(f"  {len(segments)} segments in {time.time()-t0:.1f}s")
    if not segments:
        print("no segments — transcription failed or produced nothing. Stopping.")
        sys.exit(1)

    min_clips, max_clips = clip_factory.yield_target(duration)
    print(f"\nyield target for {duration/60:.1f} min source: {min_clips}-{max_clips} clips")

    print("running selection LLM call...")
    t0 = time.time()
    selection = clip_factory.select_clips(segments, duration, workdir=workdir)
    print(f"  done in {time.time()-t0:.1f}s")

    print(f"\nsource_type: {selection.get('source_type')}")
    if selection.get("below_target_reason"):
        print(f"below_target_reason: {selection['below_target_reason']}")

    all_candidates = selection.get("candidates") or []
    print(f"\n{len(all_candidates)} raw candidates returned:\n")
    for c in all_candidates:
        scores = c.get("scores") or {}
        total = sum(float(scores.get(k) or 0) for k in
                   ("hook", "coherence", "value", "energy", "platform_fit"))
        dur = c.get("end_seconds", 0) - c.get("start_seconds", 0)
        ok = "OK" if c.get("standalone_ok") else "REJECTED (standalone test failed)"
        print(f"  [{c.get('clip_family','?'):8s}] {c.get('start_seconds',0):6.1f}s-{c.get('end_seconds',0):6.1f}s "
              f"({dur:5.1f}s)  total={total:5.1f}  {ok}")
        print(f"    hook: {c.get('hook_text','')!r}")
        print(f"    scores: {scores}")
        if not c.get("standalone_ok"):
            print(f"    standalone_issue: {c.get('standalone_issue')}")
        print()

    ranked = clip_factory.rank_and_trim(selection)
    print("=" * 70)
    print(f"FINAL RANKED OUTPUT: {len(ranked)} clips (target was {min_clips}-{max_clips})\n")
    for c in ranked:
        print(f"  #{c['rank']}  [{c['clip_family']}]  {c['duration_seconds']:.1f}s  "
              f"score={c['score_total']:.1f}")
        print(f"      {c['start_seconds']:.1f}s - {c['end_seconds']:.1f}s")
        print(f"      hook: {c['hook_text']!r}")
        print()

    # Timeline coverage sanity check — are the kept clips spread across the source,
    # or all clustered in one section?
    if ranked:
        positions = sorted(c["start_seconds"] / duration for c in ranked)
        print(f"timeline coverage (0=start, 1=end of source): {[round(p, 2) for p in positions]}")


if __name__ == "__main__":
    main()

"""Regression test for two compounding bugs in source_media_review's frame
sampling step, both found live-debugging a "why is representative_frames
always empty" report (2026-08-10):

1. `lib/source_media_review.py`'s call into `frame_sampler.execute()` never
   passed the tool's required `strategy` field — `frame_sampler.py` does a
   bare `inputs["strategy"]`, so this raised `KeyError('strategy')` on every
   single call, silently swallowed by a broad `except Exception` + a log
   warning (`frame_sampler failed for ...: 'strategy'`, present in
   `logs/worker.log` on every job since at least 2026-07-13 — 100% failure
   rate for 18+ days, never surfaced as a user-visible symptom because
   `representative_frames` isn't required by the schema and nothing
   downstream currently reads the image content).
2. Fixing #1 alone still produced an empty list: `frame_sampler.py` returns
   its results under the key `"frames"` (a list of `{path, timestamp_seconds,
   index}` dicts) — the caller was reading `sample_result.data.get(
   "frame_paths", [])`, a key that has never existed, silently defaulting to
   an empty list via `.get(..., [])`.

Both bugs had to be fixed together — fixing only one still leaves
`representative_frames` empty, just via a different silent path. This test
exercises the real integration (real `tool_registry`, real ffmpeg frame
extraction) rather than mocking `frame_sampler`, specifically because a
mock would have kept passing throughout both bugs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lib.source_media_review import review_source_media
from tools.tool_registry import registry

# Any real video already sitting in storage/jobs/ from prior manual testing;
# skip if the fixture repo doesn't have one rather than fail the whole suite.
_SAMPLE_VIDEO = Path("storage/jobs/job_2dfebb0fc200/input.mp4")


@pytest.mark.skipif(not _SAMPLE_VIDEO.exists(), reason="no sample video fixture on disk")
def test_review_source_media_produces_representative_frames(tmp_path):
    registry.discover("tools")  # matches how the real worker.py path warms up the registry

    result = review_source_media([_SAMPLE_VIDEO], {}, registry)
    entry = result["files"][0]

    frames = entry.get("representative_frames")
    assert frames, "representative_frames should not be empty for a real video probe"
    assert all(Path(p).exists() for p in frames), "every listed frame path must actually exist on disk"

    # Cleanup: this test writes real .jpg files into the fixture job's dir
    # (source_media_review's own convention: <video_dir>/.source_review_frames/).
    for p in frames:
        Path(p).unlink(missing_ok=True)
    review_dir = _SAMPLE_VIDEO.parent / ".source_review_frames"
    if review_dir.exists() and not any(review_dir.iterdir()):
        review_dir.rmdir()

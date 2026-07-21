"""Replica harness for exercising the real WhatsApp bot pipeline without a
live WhatsApp round-trip.

Two modes:

  retry <job_id> [<job_id> ...]
      POSTs to the REAL `/jobs/{id}/retry` HTTP endpoint (localhost:8000) --
      the exact same endpoint `server/worker.js` calls for a real "retry"
      WhatsApp reply. Most faithful to production (full remove_filler +
      apply_style re-run); requires the Python app to already be running.
      Polls the job until it reaches PREVIEW_READY/ERROR.

  apply-style <job_id> [<job_id> ...]
      Calls `pipeline_runner._op_apply_style` directly, in-process, reusing
      the job's already-enhanced audio file when present (falls back to the
      post-remove_filler cut, then the raw input). Skips re-running
      remove_filler/transcription/enhancement — much faster for iterating on
      content_planner/render-layer bugs specifically. Not a substitute for
      `retry` mode when the bug could be in remove_filler or the enhancement
      chain itself.

Both modes report, for the resulting props:
  - whether apply_style degraded (bare cut delivered instead of the styled one)
  - whether any content-zone visual (dataCards/gauges/countdowns/calendarEvents/
    beforeAfter/stepLists/topicCards) falls inside a Dominant (oversized)
    SpeakerCard window in the final `scenes` array -- the facecam-overlap bug
    class (Fix C31/C33)
  - any remaining props_lint findings

Usage (from repo root, PYTHONIOENCODING=utf-8 on Windows consoles):
  .venv/Scripts/python.exe -m whatsapp_mvp.simulate_job retry job_51f154a80f9b job_452ef6c48100
  .venv/Scripts/python.exe -m whatsapp_mvp.simulate_job apply-style job_73e873e4f7e1
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional


def _facecam_overlap_findings(props: dict) -> list[tuple[str, str, int, int, int]]:
    """Fix C31/C33's own check, formalized: does any content-zone visual's
    mount/end window fall inside a frame where the SpeakerCard is Dominant
    (oversized, h>=1000) per `scenes`? Returns (kind, label, mountFrame,
    endFrame, first_bad_frame) tuples; empty means clean."""
    scenes = props.get("scenes") or []
    if not scenes:
        return []

    def mode_at(frame: int) -> str:
        state = "dominant"
        for s in scenes:
            if s["frame"] <= frame:
                state = "workflow" if s["h"] < 1000 else "dominant"
            else:
                break
        return state

    groups = {
        "dataCards": props.get("dataCards") or [],
        "gauges": props.get("gauges") or [],
        "countdowns": props.get("countdowns") or [],
        "calendarEvents": props.get("calendarEvents") or [],
        "beforeAfter": props.get("beforeAfter") or [],
        "stepLists": props.get("stepLists") or [],
        "topicCards": props.get("topicCards") or [],
    }
    problems = []
    for kind, items in groups.items():
        for it in items:
            mf, ef = it.get("mountFrame"), it.get("endFrame")
            if mf is None or ef is None:
                continue
            for f in range(mf, ef, 5):
                if mode_at(f) == "dominant":
                    label = it.get("headline") or it.get("title") or ""
                    problems.append((kind, label, mf, ef, f))
                    break
    return problems


def _report(job_id: str, props_path: Path, degraded: Optional[list] = None) -> None:
    print(f"\n=== {job_id} ===")
    if not props_path.exists():
        print("  no props file produced (apply_style never reached the render step)")
        return
    props = json.loads(props_path.read_text(encoding="utf-8"))
    if degraded:
        print(f"  DEGRADED operations: {degraded}")
    else:
        print("  not degraded (styled render delivered)")

    overlap = _facecam_overlap_findings(props)
    if overlap:
        print(f"  FACECAM-OVERLAP BUG (C31/C33 class) -- {len(overlap)} finding(s):")
        for kind, label, mf, ef, f in overlap:
            print(f"    {kind} {label!r} window {mf}-{ef} hits Dominant at frame {f}")
    else:
        print("  facecam-overlap check: clean (no content-zone visual falls inside a Dominant window)")

    try:
        from .props_lint import lint_props
        findings = lint_props(props)
        if findings:
            print(f"  props_lint: {len(findings)} finding(s): {[f['check'] for f in findings]}")
        else:
            print("  props_lint: clean")
    except Exception as e:
        print(f"  props_lint: could not run ({e})")


def _mode_retry(job_ids: list[str]) -> None:
    import requests

    base = "http://localhost:8000"
    try:
        r = requests.get(f"{base}/health", timeout=5)
        r.raise_for_status()
    except Exception as e:
        print(f"Python app not reachable at {base} ({e}) -- start it first: "
              f".venv/Scripts/python.exe -m whatsapp_mvp.main server")
        sys.exit(1)

    for job_id in job_ids:
        print(f"\n--- retrying {job_id} via real /jobs/{{id}}/retry endpoint ---")
        resp = requests.post(f"{base}/jobs/{job_id}/retry", timeout=10)
        if resp.status_code != 200:
            print(f"  retry request failed: HTTP {resp.status_code} {resp.text[:300]}")
            continue

        deadline = time.time() + 1500  # 25 min ceiling per job -- a full retry can chain
        # props_lint's 3-round replan loop with a vision-QA-triggered extra replan on top;
        # 10 min (the first value tried here) was confirmed too short on a real run that
        # was still healthily progressing well past it.
        last_status = None
        while time.time() < deadline:
            r = requests.get(f"{base}/jobs/{job_id}", timeout=10)
            data = r.json()
            status = data.get("status")
            if status != last_status:
                print(f"  status: {status}")
                last_status = status
            if status in ("PREVIEW_READY", "ERROR"):
                props_path = Path("storage/jobs") / job_id / "_op_apply_style_props.json"
                _report(job_id, props_path, degraded=data.get("degraded_operations"))
                break
            time.sleep(5)
        else:
            print(f"  TIMEOUT waiting for {job_id} to finish (still {last_status} after 10 min)")


def _mode_apply_style(job_ids: list[str]) -> None:
    from .pipeline_runner import _op_apply_style

    for job_id in job_ids:
        workdir = Path("storage/jobs") / job_id
        src = None
        for candidate in ("_op_audio_enhance.mp4", "_op_nofiller.mp4", "input.mp4"):
            p = workdir / candidate
            if p.exists():
                src = str(p)
                break
        if src is None:
            print(f"\n--- {job_id}: no usable source video found in {workdir}, skipping ---")
            continue

        print(f"\n--- apply_style direct on {job_id} (src={Path(src).name}) ---")
        props_path = workdir / "_op_apply_style_props.json"
        degraded = None
        try:
            _op_apply_style(src, {}, workdir)
        except RuntimeError as e:
            degraded = ["apply_style"]
            print(f"  apply_style raised (degraded): {e}")
        _report(job_id, props_path, degraded=degraded)


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in ("retry", "apply-style"):
        print(__doc__)
        sys.exit(1)
    mode, job_ids = sys.argv[1], sys.argv[2:]
    if mode == "retry":
        _mode_retry(job_ids)
    else:
        _mode_apply_style(job_ids)


if __name__ == "__main__":
    main()

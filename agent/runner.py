"""
runner.py — Deterministic video production runner.

Spawned by server/worker.js per job:
  python3 -m runner --job-id <id> --prompt "..." --wa-number "+852..."
"""

import argparse
import sys
import traceback

from state import JobState
from service import VideoService
from whatsapp import notify_error


def run(job_id: str, prompt: str, wa_number: str) -> None:
    state   = JobState(job_id, wa_number)
    service = VideoService(state)
    service.run(prompt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id",    required=True)
    parser.add_argument("--prompt",    required=True)
    parser.add_argument("--wa-number", required=True)
    args = parser.parse_args()

    try:
        run(args.job_id, args.prompt, args.wa_number)
        sys.exit(0)
    except Exception:
        traceback.print_exc()
        try:
            state = JobState(args.job_id, args.wa_number)
            notify_error(state)
        except Exception:
            pass
        sys.exit(1)

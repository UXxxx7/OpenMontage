"""
state.py — Redis-backed job state and approval gate.

Each job owns:
  - A project directory for OpenMontage artifacts
  - A Redis key tracking the final video path
  - A pub/sub approval channel for human-in-the-loop pauses
"""

import os
import time
from pathlib import Path
from typing import Optional

import redis as redis_lib

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/tmp/wa-montage-projects"))


class JobState:
    def __init__(self, job_id: str, wa_number: str) -> None:
        self.job_id = job_id
        self.wa_number = wa_number

        # Sanitise wa_number for use as a directory name
        safe = wa_number.lstrip("+").replace(" ", "_")
        self.project_dir = str(OUTPUT_ROOT / f"{safe}_{job_id}")
        Path(self.project_dir).mkdir(parents=True, exist_ok=True)

        self.redis = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)

        # Keys
        self._video_key = f"job:{job_id}:video_path"
        self._approval_channel = f"approval:{wa_number}"

    # ── Final video ──────────────────────────────────────────────────
    def set_final_video(self, path: str) -> None:
        self.redis.set(self._video_key, path, ex=3600)

    def find_final_video(self) -> Optional[str]:
        # 1. Check Redis first (set by tool execution)
        cached = self.redis.get(self._video_key)
        if cached:
            return cached

        # 2. Scan project dir for renders/final.mp4
        for candidate in [
            Path(self.project_dir) / "renders" / "final.mp4",
            Path(self.project_dir) / "final.mp4",
        ]:
            if candidate.exists():
                return str(candidate)

        # 3. Walk and find any .mp4 produced
        for f in Path(self.project_dir).rglob("*.mp4"):
            return str(f)

        return None

    # ── Approval gate ────────────────────────────────────────────────
    def wait_for_approval(self, timeout: int = 300) -> Optional[str]:
        """
        Block until the user replies via WhatsApp (Node webhook publishes
        to `approval:<waNumber>`), or until timeout seconds elapse.
        Returns the user's reply string, or None on timeout.
        """
        sub = self.redis.pubsub()
        sub.subscribe(self._approval_channel)

        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                msg = sub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg["type"] == "message":
                    return msg["data"]
            return None
        finally:
            sub.unsubscribe(self._approval_channel)
            sub.close()

"""
pipeline/avatar.py — Generate avatar video via HeyGen API.

Uses the HeyGen v2 video generation endpoint directly.
Polls until the video is ready, then downloads it to project_dir/avatar.mp4.

HeyGen avatar ID and voice ID are configured via env vars so they can be
swapped without code changes.
"""

import os
import time
from pathlib import Path

import requests

HEYGEN_API = "https://api.heygen.com"
HEYGEN_KEY = os.environ.get("HEYGEN_API_KEY", "")

# Default avatar + voice — override via .env
AVATAR_ID   = os.environ.get("HEYGEN_AVATAR_ID", "Daisy-inskirt-20220818")
VOICE_ID    = os.environ.get("HEYGEN_VOICE_ID",  "2d5b0e6cf36f460aa7fc47e3eee4ba54")  # Cantonese female
RESOLUTION  = os.environ.get("HEYGEN_RESOLUTION", "720p")

POLL_INTERVAL = 10   # seconds between status checks
MAX_WAIT      = 600  # 10 minutes


def generate_avatar(script: dict, project_dir: Path) -> str:
    """
    Submit a HeyGen video job, poll until complete, download to avatar.mp4.
    Returns the local file path.
    """
    out_path = project_dir / "avatar.mp4"
    if out_path.exists():
        return str(out_path)

    full_text = script.get("full_text", "")
    if not full_text:
        full_text = " ".join(line["text"] for line in script.get("lines", []))

    video_id = _submit_job(full_text)
    print(f"[avatar] HeyGen job submitted: {video_id}", flush=True)

    video_url = _poll_until_ready(video_id)
    print(f"[avatar] video ready: {video_url}", flush=True)

    _download(video_url, out_path)
    return str(out_path)


def _submit_job(text: str) -> str:
    resp = requests.post(
        f"{HEYGEN_API}/v2/video/generate",
        headers={"X-Api-Key": HEYGEN_KEY, "Content-Type": "application/json"},
        json={
            "video_inputs": [
                {
                    "character": {
                        "type": "avatar",
                        "avatar_id": AVATAR_ID,
                        "avatar_style": "normal",
                    },
                    "voice": {
                        "type": "text",
                        "input_text": text,
                        "voice_id": VOICE_ID,
                        "speed": 1.0,
                    },
                    "background": {
                        "type": "color",
                        "value": "#FFFFFF",
                    },
                }
            ],
            "dimension": {"width": 1280, "height": 720} if RESOLUTION == "720p"
                         else {"width": 1920, "height": 1080},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    video_id = data.get("data", {}).get("video_id")
    if not video_id:
        raise RuntimeError(f"HeyGen did not return video_id: {data}")
    return video_id


def _poll_until_ready(video_id: str) -> str:
    deadline = time.time() + MAX_WAIT
    while time.time() < deadline:
        resp = requests.get(
            f"{HEYGEN_API}/v1/video_status.get",
            headers={"X-Api-Key": HEYGEN_KEY},
            params={"video_id": video_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        status = data.get("status")

        if status == "completed":
            url = data.get("video_url")
            if not url:
                raise RuntimeError("HeyGen status=completed but no video_url")
            return url
        elif status == "failed":
            raise RuntimeError(f"HeyGen video failed: {data.get('error')}")

        print(f"[avatar] status={status}, waiting {POLL_INTERVAL}s…", flush=True)
        time.sleep(POLL_INTERVAL)

    raise TimeoutError(f"HeyGen video not ready after {MAX_WAIT}s")


def _download(url: str, out_path: Path) -> None:
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

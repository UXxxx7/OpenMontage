"""
whatsapp.py — Publish progress events to Redis.
Node.js worker.js subscribes and relays to WhatsApp Cloud API.

We never call WhatsApp directly from Python — all outbound traffic
goes through the Node.js layer that already holds the WA credentials.
"""

import json
from state import JobState


def notify_text(state: JobState, body: str) -> None:
    """Send a plain text message to the user."""
    _publish(state, {"type": "text", "body": body})


def notify_approval(state: JobState, body: str) -> None:
    """
    Send an approval prompt to the user.
    The agent will then block on state.wait_for_approval().
    """
    _publish(state, {"type": "approval", "body": body})


def notify_video(state: JobState, path: str, caption: str = "") -> None:
    """Notify Node worker that the final video is ready."""
    _publish(state, {"type": "video", "path": path, "caption": caption})


def _publish(state: JobState, payload: dict) -> None:
    channel = f"progress:{state.job_id}"
    state.redis.publish(channel, json.dumps(payload, ensure_ascii=False))

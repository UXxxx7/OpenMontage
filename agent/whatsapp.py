"""
whatsapp.py — Direct WhatsApp message sending from Python pipeline.

Replaces the old Redis pub/sub relay. The pipeline now calls
WhatsAppClient and MediaManager directly instead of publishing events
for Node.js to forward.
"""

from __future__ import annotations

import asyncio
import logging

from wa.client import WhatsAppClient
from wa.media import MediaManager

logger = logging.getLogger(__name__)


# ── Sync wrappers (pipeline is synchronous) ───────────────────────────────────

def notify_text(state, body: str) -> None:
    try:
        asyncio.run(_send_text(state.wa_number, body))
    except Exception as e:
        logger.error("notify_text failed: %s", e)


def notify_approval(state, body: str) -> None:
    notify_text(state, body)


def notify_video(state, video_path: str, caption: str = "") -> None:
    try:
        asyncio.run(_send_video(state.wa_number, video_path, caption))
    except Exception as e:
        logger.error("notify_video failed: %s", e)


def notify_error(state, message: str = "抱歉，視頻製作失敗 😢 請稍後再試。") -> None:
    notify_text(state, message)


# ── Async implementations ─────────────────────────────────────────────────────

async def _send_text(to: str, body: str) -> None:
    client = WhatsAppClient()
    await client.send_text(to, body)


async def _send_video(to: str, video_path: str, caption: str) -> None:
    media    = MediaManager()
    client   = WhatsAppClient()
    media_id = await media.upload(video_path, "video/mp4")
    await client.send_video(to, video_id=media_id, caption=caption)

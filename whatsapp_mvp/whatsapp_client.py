# WhatsApp MVP - WhatsApp Cloud API Client

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Optional

import requests

from .config import Config

logger = logging.getLogger(__name__)


class WhatsAppClient:
    """Minimal WhatsApp Cloud API client."""

    API_BASE = "https://graph.facebook.com/v21.0"

    def __init__(self, config: Config):
        self.config = config
        self.phone_number_id = config.whatsapp_phone_number_id
        self.access_token = config.whatsapp_access_token

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    def verify_signature(self, signature: str, body: bytes) -> bool:
        """Verify the X-Hub-Signature-256 header."""
        if not self.config.whatsapp_app_secret:
            logger.warning("WHATSAPP_APP_SECRET not set; skipping signature verification")
            return True
        expected = hmac.new(
            self.config.whatsapp_app_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        expected_header = f"sha256={expected}"
        return hmac.compare_digest(expected_header, signature)

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    def _send_message(self, payload: dict) -> dict:
        url = f"{self.API_BASE}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def send_text_message(self, to: str, text: str) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": True, "body": text},
        }
        return self._send_message(payload)

    def send_video_message(self, to: str, video_id: str, caption: Optional[str] = None) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "video",
            "video": {"id": video_id},
        }
        if caption:
            payload["video"]["caption"] = caption
        return self._send_message(payload)

    # ------------------------------------------------------------------
    # Media download
    # ------------------------------------------------------------------

    def get_media_url(self, media_id: str) -> str:
        """Retrieve the download URL for a media object."""
        url = f"{self.API_BASE}/{media_id}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["url"]

    def download_media(self, media_id: str, dest_path: str) -> None:
        """Download media from WhatsApp to local disk."""
        download_url = self.get_media_url(media_id)
        headers = {"Authorization": f"Bearer {self.access_token}"}
        resp = requests.get(download_url, headers=headers, timeout=120, stream=True)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"Downloaded media {media_id} to {dest_path}")


def get_whatsapp_client() -> WhatsAppClient:
    from .config import get_config
    return WhatsAppClient(get_config())

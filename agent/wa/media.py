from __future__ import annotations
"""
WhatsApp Cloud API — Media handling.

Responsibilities:
- Upload media (image, video, audio, document, sticker)
- Get media URL from media ID
- Download media binary
- Delete media
"""

import logging
from pathlib import Path

import httpx

from wa.config import settings

logger = logging.getLogger(__name__)

# Supported MIME types per media category
SUPPORTED_TYPES = {
    "image": {"image/jpeg", "image/png"},
    "video": {"video/mp4", "video/3gpp"},
    "audio": {"audio/aac", "audio/mp4", "audio/mpeg", "audio/amr", "audio/ogg"},
    "document": {
        "application/pdf", "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
    },
    "sticker": {"image/webp"},
}


class MediaManager:
    """Async manager for WhatsApp media operations."""

    def __init__(
        self,
        phone_number_id: str | None = None,
        access_token: str | None = None,
        api_version: str | None = None,
    ):
        self.phone_number_id = phone_number_id or settings.whatsapp_phone_number_id
        self.access_token = access_token or settings.whatsapp_access_token
        self.api_version = api_version or settings.whatsapp_api_version
        self.base_url = f"https://graph.facebook.com/{self.api_version}"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    # -----------------------------------------------------------------------
    # Upload
    # -----------------------------------------------------------------------

    async def upload(
        self,
        file_path: str | Path,
        mime_type: str,
    ) -> str:
        """
        Upload a media file and return the media ID.

        Args:
            file_path: Path to the file to upload.
            mime_type: MIME type (e.g. "image/jpeg", "application/pdf").

        Returns:
            The media ID string.
        """
        file_path = Path(file_path)
        url = f"{self.base_url}/{self.phone_number_id}/media"

        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(file_path, "rb") as f:
                response = await client.post(
                    url,
                    headers=self._auth_headers(),
                    data={"messaging_product": "whatsapp", "type": mime_type},
                    files={"file": (file_path.name, f, mime_type)},
                )

        data = response.json()
        if response.status_code != 200:
            logger.error("Media upload error: %s %s", response.status_code, data)
            response.raise_for_status()

        media_id = data.get("id", "")
        logger.info("Media uploaded: %s → %s", file_path.name, media_id)
        return media_id

    # -----------------------------------------------------------------------
    # Get URL
    # -----------------------------------------------------------------------

    async def get_url(self, media_id: str) -> str:
        """
        Get the download URL for a media ID.
        The URL is temporary and requires the access token to download.
        """
        url = f"{self.base_url}/{media_id}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._auth_headers())

        data = response.json()
        if response.status_code != 200:
            logger.error("Get media URL error: %s %s", response.status_code, data)
            response.raise_for_status()

        media_url = data.get("url", "")
        logger.info("Media URL retrieved for %s", media_id)
        return media_url

    # -----------------------------------------------------------------------
    # Download
    # -----------------------------------------------------------------------

    async def download(self, media_id: str) -> tuple[bytes, str]:
        """
        Download media by ID. Returns (file_bytes, mime_type).

        Two-step process:
        1. Get the temporary URL from the media ID
        2. Download the binary from that URL
        """
        # Step 1: get URL and mime type
        url = f"{self.base_url}/{media_id}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._auth_headers())

        data = response.json()
        if response.status_code != 200:
            logger.error("Get media info error: %s %s", response.status_code, data)
            response.raise_for_status()

        media_url = data["url"]
        mime_type = data.get("mime_type", "application/octet-stream")

        # Step 2: download the actual file
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(media_url, headers=self._auth_headers())

        if response.status_code != 200:
            logger.error("Media download error: %s", response.status_code)
            response.raise_for_status()

        logger.info("Media downloaded: %s (%s, %d bytes)", media_id, mime_type, len(response.content))
        return response.content, mime_type

    async def download_to_file(self, media_id: str, output_dir: str | Path) -> Path:
        """
        Download media and save to a file. Returns the file path.
        Filename is based on the media ID.
        """
        content, mime_type = await self.download(media_id)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Determine extension from mime type
        ext = _mime_to_ext(mime_type)
        file_path = output_dir / f"{media_id}{ext}"
        file_path.write_bytes(content)

        logger.info("Media saved to %s", file_path)
        return file_path

    # -----------------------------------------------------------------------
    # Delete
    # -----------------------------------------------------------------------

    async def delete(self, media_id: str) -> bool:
        """Delete a media file. Returns True on success."""
        url = f"{self.base_url}/{media_id}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(url, headers=self._auth_headers())

        data = response.json()
        if response.status_code != 200:
            logger.error("Media delete error: %s %s", response.status_code, data)
            return False

        success = data.get("success", False)
        logger.info("Media deleted: %s → %s", media_id, success)
        return success


def _mime_to_ext(mime_type: str) -> str:
    """Map common MIME types to file extensions."""
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/3gpp": ".3gp",
        "audio/aac": ".aac",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "audio/amr": ".amr",
        "audio/ogg": ".ogg",
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "text/plain": ".txt",
    }
    return mapping.get(mime_type, ".bin")

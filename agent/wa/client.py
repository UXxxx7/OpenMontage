from __future__ import annotations
"""
WhatsApp Cloud API — Message sending client.

Covers ALL outbound message types:
- Text (with/without preview)
- Media: image, video, audio, document, sticker
- Location
- Contacts
- Reactions
- Interactive: reply buttons, CTA URL buttons, list messages,
              location request, product, product list, flows
- Templates (with parameters, header media, buttons)
- Read receipts (mark as read)
- Business profile management
- Phone number info
"""

import logging
from typing import Any

import httpx

from wa.config import settings

logger = logging.getLogger(__name__)


class WhatsAppClient:
    """Async client for sending WhatsApp messages via the Cloud API."""

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
        self.messages_url = f"{self.base_url}/{self.phone_number_id}/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def _send(self, payload: dict) -> dict:
        """Send a payload to the messages endpoint and return the response."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                self.messages_url,
                headers=self._headers(),
                json=payload,
            )
        data = response.json()
        if response.status_code != 200:
            logger.error("WhatsApp API error: %s %s", response.status_code, data)
            response.raise_for_status()
        logger.info("Message sent: %s", data.get("messages", [{}])[0].get("id", "unknown"))
        return data

    def _base_payload(self, to: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
        }

    # -----------------------------------------------------------------------
    # Text
    # -----------------------------------------------------------------------

    MAX_TEXT_LENGTH = 4096

    async def send_text(
        self,
        to: str,
        body: str,
        preview_url: bool = False,
        reply_to: str | None = None,
        auto_split: bool = True,
    ) -> dict | list[dict]:
        """
        Send a text message. If auto_split is True and the body exceeds
        4,096 characters, it splits into multiple messages.

        Returns a single result dict, or a list of result dicts if split.
        """
        if auto_split and len(body) > self.MAX_TEXT_LENGTH:
            return await self._send_text_split(to, body, preview_url, reply_to)

        payload = {
            **self._base_payload(to),
            "type": "text",
            "text": {"preview_url": preview_url, "body": body},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    async def _send_text_split(
        self,
        to: str,
        body: str,
        preview_url: bool = False,
        reply_to: str | None = None,
    ) -> list[dict]:
        """Split a long message and send as multiple parts."""
        chunks = self._split_text(body, self.MAX_TEXT_LENGTH)
        results = []
        for i, chunk in enumerate(chunks):
            part_label = f"[{i+1}/{len(chunks)}] " if len(chunks) > 1 else ""
            payload = {
                **self._base_payload(to),
                "type": "text",
                "text": {"preview_url": preview_url, "body": f"{part_label}{chunk}"},
            }
            # Only reply_to on the first chunk
            if i == 0 and reply_to:
                payload["context"] = {"message_id": reply_to}
            results.append(await self._send(payload))
        return results

    @staticmethod
    def _split_text(text: str, max_len: int) -> list[str]:
        """
        Split text into chunks of max_len characters.
        Tries to split on paragraph breaks > newlines > sentence endings > spaces.
        Works for both English and Chinese text.
        """
        if len(text) <= max_len:
            return [text]

        # Reserve space for part label like "[1/3] "
        effective_max = max_len - 8
        chunks = []
        remaining = text

        while remaining:
            if len(remaining) <= effective_max:
                chunks.append(remaining)
                break

            # Find the best split point
            segment = remaining[:effective_max]
            split_at = -1

            # Try paragraph break first
            split_at = segment.rfind("\n\n")
            if split_at == -1 or split_at < effective_max // 2:
                # Try single newline
                split_at = segment.rfind("\n")
            if split_at == -1 or split_at < effective_max // 2:
                # Try sentence endings (works for both EN and ZH)
                for sep in ("。", ".", "！", "!", "？", "?", "；", ";"):
                    pos = segment.rfind(sep)
                    if pos > effective_max // 2:
                        split_at = pos + 1  # include the punctuation
                        break
            if split_at == -1 or split_at < effective_max // 2:
                # Try space (for English)
                split_at = segment.rfind(" ")
            if split_at == -1 or split_at < effective_max // 2:
                # Hard cut as last resort
                split_at = effective_max

            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()

        return chunks

    # -----------------------------------------------------------------------
    # Media messages: image, video, audio, document, sticker
    # -----------------------------------------------------------------------

    async def send_image(
        self,
        to: str,
        image_id: str | None = None,
        image_url: str | None = None,
        caption: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """Send an image by media ID or URL."""
        image: dict[str, Any] = {}
        if image_id:
            image["id"] = image_id
        elif image_url:
            image["link"] = image_url
        if caption:
            image["caption"] = caption
        payload = {**self._base_payload(to), "type": "image", "image": image}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    async def send_video(
        self,
        to: str,
        video_id: str | None = None,
        video_url: str | None = None,
        caption: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """Send a video by media ID or URL."""
        video: dict[str, Any] = {}
        if video_id:
            video["id"] = video_id
        elif video_url:
            video["link"] = video_url
        if caption:
            video["caption"] = caption
        payload = {**self._base_payload(to), "type": "video", "video": video}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    async def send_audio(
        self,
        to: str,
        audio_id: str | None = None,
        audio_url: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """Send an audio message by media ID or URL."""
        audio: dict[str, Any] = {}
        if audio_id:
            audio["id"] = audio_id
        elif audio_url:
            audio["link"] = audio_url
        payload = {**self._base_payload(to), "type": "audio", "audio": audio}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    async def send_document(
        self,
        to: str,
        document_id: str | None = None,
        document_url: str | None = None,
        caption: str | None = None,
        filename: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """Send a document by media ID or URL."""
        document: dict[str, Any] = {}
        if document_id:
            document["id"] = document_id
        elif document_url:
            document["link"] = document_url
        if caption:
            document["caption"] = caption
        if filename:
            document["filename"] = filename
        payload = {**self._base_payload(to), "type": "document", "document": document}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    async def send_sticker(
        self,
        to: str,
        sticker_id: str | None = None,
        sticker_url: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """Send a sticker by media ID or URL."""
        sticker: dict[str, Any] = {}
        if sticker_id:
            sticker["id"] = sticker_id
        elif sticker_url:
            sticker["link"] = sticker_url
        payload = {**self._base_payload(to), "type": "sticker", "sticker": sticker}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    # -----------------------------------------------------------------------
    # Location
    # -----------------------------------------------------------------------

    async def send_location(
        self,
        to: str,
        latitude: float,
        longitude: float,
        name: str | None = None,
        address: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """Send a location pin."""
        location: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
        }
        if name:
            location["name"] = name
        if address:
            location["address"] = address
        payload = {**self._base_payload(to), "type": "location", "location": location}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    # -----------------------------------------------------------------------
    # Contacts
    # -----------------------------------------------------------------------

    async def send_contacts(
        self,
        to: str,
        contacts: list[dict],
        reply_to: str | None = None,
    ) -> dict:
        """
        Send one or more contact cards.

        Each contact dict should follow the WhatsApp contacts format:
        {
            "name": {"formatted_name": "John Doe", "first_name": "John", "last_name": "Doe"},
            "phones": [{"phone": "+85212345678", "type": "WORK"}],
            "emails": [{"email": "john@example.com", "type": "WORK"}],
        }
        """
        payload = {**self._base_payload(to), "type": "contacts", "contacts": contacts}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    # -----------------------------------------------------------------------
    # Reactions
    # -----------------------------------------------------------------------

    async def send_reaction(
        self,
        to: str,
        message_id: str,
        emoji: str,
    ) -> dict:
        """React to a message with an emoji. Send empty emoji to remove reaction."""
        payload = {
            **self._base_payload(to),
            "type": "reaction",
            "reaction": {
                "message_id": message_id,
                "emoji": emoji,
            },
        }
        return await self._send(payload)

    async def remove_reaction(self, to: str, message_id: str) -> dict:
        """Remove a reaction from a message."""
        return await self.send_reaction(to, message_id, emoji="")

    # -----------------------------------------------------------------------
    # Interactive messages: buttons and lists
    # -----------------------------------------------------------------------

    async def send_reply_buttons(
        self,
        to: str,
        body: str,
        buttons: list[dict[str, str]],
        header: str | None = None,
        footer: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """
        Send a message with up to 3 reply buttons.

        buttons: [{"id": "btn_1", "title": "Option 1"}, ...]
        """
        action = {
            "buttons": [
                {"type": "reply", "reply": btn}
                for btn in buttons[:3]
            ],
        }
        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": body},
            "action": action,
        }
        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}
        payload = {
            **self._base_payload(to),
            "type": "interactive",
            "interactive": interactive,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    async def send_cta_url_button(
        self,
        to: str,
        body: str,
        button_title: str,
        url: str,
        header: str | None = None,
        footer: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """Send a message with a CTA URL button."""
        interactive: dict[str, Any] = {
            "type": "cta_url",
            "body": {"text": body},
            "action": {
                "name": "cta_url",
                "parameters": {
                    "display_text": button_title,
                    "url": url,
                },
            },
        }
        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}
        payload = {
            **self._base_payload(to),
            "type": "interactive",
            "interactive": interactive,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    async def send_list(
        self,
        to: str,
        body: str,
        button_text: str,
        sections: list[dict],
        header: str | None = None,
        footer: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """
        Send a list message (menu).

        sections: [
            {
                "title": "Section 1",
                "rows": [
                    {"id": "row_1", "title": "Row 1", "description": "Optional desc"},
                ]
            }
        ]
        """
        interactive: dict[str, Any] = {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_text,
                "sections": sections,
            },
        }
        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}
        payload = {
            **self._base_payload(to),
            "type": "interactive",
            "interactive": interactive,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    # -----------------------------------------------------------------------
    # Location request (ask user to share their location)
    # -----------------------------------------------------------------------

    async def send_location_request(
        self,
        to: str,
        body: str,
        reply_to: str | None = None,
    ) -> dict:
        """Request the user to share their location."""
        interactive: dict[str, Any] = {
            "type": "location_request_message",
            "body": {"text": body},
            "action": {"name": "send_location"},
        }
        payload = {
            **self._base_payload(to),
            "type": "interactive",
            "interactive": interactive,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    # -----------------------------------------------------------------------
    # Product messages (catalog)
    # -----------------------------------------------------------------------

    async def send_product(
        self,
        to: str,
        catalog_id: str,
        product_retailer_id: str,
        body: str | None = None,
        footer: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """Send a single product from a catalog."""
        interactive: dict[str, Any] = {
            "type": "product",
            "action": {
                "catalog_id": catalog_id,
                "product_retailer_id": product_retailer_id,
            },
        }
        if body:
            interactive["body"] = {"text": body}
        if footer:
            interactive["footer"] = {"text": footer}
        payload = {
            **self._base_payload(to),
            "type": "interactive",
            "interactive": interactive,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    async def send_product_list(
        self,
        to: str,
        catalog_id: str,
        sections: list[dict],
        header: str,
        body: str,
        footer: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """
        Send a multi-product message (up to 30 products, max 10 sections).

        sections: [
            {
                "title": "Popular items",
                "product_items": [
                    {"product_retailer_id": "product_1"},
                    {"product_retailer_id": "product_2"},
                ]
            }
        ]
        """
        interactive: dict[str, Any] = {
            "type": "product_list",
            "header": {"type": "text", "text": header},
            "body": {"text": body},
            "action": {
                "catalog_id": catalog_id,
                "sections": sections,
            },
        }
        if footer:
            interactive["footer"] = {"text": footer}
        payload = {
            **self._base_payload(to),
            "type": "interactive",
            "interactive": interactive,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    # -----------------------------------------------------------------------
    # Flow messages
    # -----------------------------------------------------------------------

    async def send_flow(
        self,
        to: str,
        flow_id: str,
        flow_cta: str,
        body: str,
        flow_token: str = "unused",
        flow_action: str = "navigate",
        flow_action_payload: dict | None = None,
        mode: str = "published",
        header: str | None = None,
        footer: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """
        Send a WhatsApp Flow message.

        flow_cta: button text (max 20 chars, no emoji)
        flow_action: "navigate" or "data_exchange"
        mode: "published" or "draft"
        """
        parameters: dict[str, Any] = {
            "flow_message_version": "3",
            "flow_token": flow_token,
            "flow_id": flow_id,
            "flow_cta": flow_cta,
            "flow_action": flow_action,
            "mode": mode,
        }
        if flow_action_payload:
            parameters["flow_action_payload"] = flow_action_payload
        interactive: dict[str, Any] = {
            "type": "flow",
            "body": {"text": body},
            "action": {
                "name": "flow",
                "parameters": parameters,
            },
        }
        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}
        payload = {
            **self._base_payload(to),
            "type": "interactive",
            "interactive": interactive,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    # -----------------------------------------------------------------------
    # Templates
    # -----------------------------------------------------------------------

    async def send_template(
        self,
        to: str,
        template_name: str,
        language_code: str = "en_US",
        components: list[dict] | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """
        Send a pre-approved message template.

        components: optional list of header/body/button parameter components.
        Example:
        [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": "John"},
                    {"type": "text", "text": "your order"},
                ]
            }
        ]
        """
        template: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language_code},
        }
        if components:
            template["components"] = components
        payload = {
            **self._base_payload(to),
            "type": "template",
            "template": template,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._send(payload)

    # -----------------------------------------------------------------------
    # Read receipts & typing indicators
    # -----------------------------------------------------------------------

    async def mark_as_read(self, message_id: str) -> dict:
        """Mark a message as read (sends blue ticks)."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        return await self._send(payload)

    # -----------------------------------------------------------------------
    # Business profile
    # -----------------------------------------------------------------------

    async def get_business_profile(self) -> dict:
        """Get the WhatsApp Business profile info."""
        url = f"{self.base_url}/{self.phone_number_id}/whatsapp_business_profile"
        params = {"fields": "about,address,description,email,profile_picture_url,websites,vertical"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers(), params=params)
        data = response.json()
        if response.status_code != 200:
            logger.error("Get profile error: %s %s", response.status_code, data)
            response.raise_for_status()
        return data

    async def update_business_profile(self, **fields) -> dict:
        """
        Update WhatsApp Business profile fields.

        Supported fields: about, address, description, email,
        profile_picture_url, websites, vertical
        """
        url = f"{self.base_url}/{self.phone_number_id}/whatsapp_business_profile"
        payload = {"messaging_product": "whatsapp", **fields}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=self._headers(), json=payload)
        data = response.json()
        if response.status_code != 200:
            logger.error("Update profile error: %s %s", response.status_code, data)
            response.raise_for_status()
        return data

    # -----------------------------------------------------------------------
    # Phone number info
    # -----------------------------------------------------------------------

    async def get_phone_number_info(self) -> dict:
        """Get info about the registered phone number (quality rating, status, etc.)."""
        url = f"{self.base_url}/{self.phone_number_id}"
        params = {"fields": "verified_name,code_verification_status,display_phone_number,quality_rating,messaging_limit_tier"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers(), params=params)
        data = response.json()
        if response.status_code != 200:
            logger.error("Get phone info error: %s %s", response.status_code, data)
            response.raise_for_status()
        return data

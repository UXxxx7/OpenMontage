from __future__ import annotations
"""
WhatsApp Cloud API — Webhook handling.

Responsibilities:
- Verify webhook subscription (GET challenge-response)
- Validate X-Hub-Signature-256 on incoming payloads
- Parse all inbound message types into clean dataclasses
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass, field

from wa.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsed message types
# ---------------------------------------------------------------------------

@dataclass
class MediaInfo:
    media_id: str
    mime_type: str
    sha256: str | None = None
    filename: str | None = None  # documents only
    caption: str | None = None
    voice: bool = False  # True = recorded directly in WhatsApp (instruction), False = uploaded file (content)


@dataclass
class LocationInfo:
    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None


@dataclass
class ContactCard:
    """A single contact card from a contacts message."""
    name: dict  # raw name object
    phones: list[dict] = field(default_factory=list)
    emails: list[dict] = field(default_factory=list)
    addresses: list[dict] = field(default_factory=list)
    org: dict | None = None
    urls: list[dict] = field(default_factory=list)


@dataclass
class ReactionInfo:
    message_id: str  # the message being reacted to
    emoji: str  # empty string means reaction removed


@dataclass
class InteractiveReply:
    """Reply from a button or list message."""
    reply_type: str  # "button_reply" or "list_reply"
    reply_id: str
    reply_title: str
    reply_description: str | None = None  # list_reply only


@dataclass
class ReferralInfo:
    """Click-to-WhatsApp ad referral data."""
    source_url: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    headline: str | None = None
    body: str | None = None
    media_type: str | None = None
    media_url: str | None = None


@dataclass
class ContextInfo:
    """Quoted message context (when user swipe-replies)."""
    message_id: str  # wamid of quoted message
    from_phone: str | None = None  # who sent the quoted message


@dataclass
class OrderInfo:
    """Order from a product catalog."""
    catalog_id: str
    products: list[dict] = field(default_factory=list)
    text: str | None = None


@dataclass
class InboundMessage:
    """Unified representation of any inbound WhatsApp message."""
    # Core fields — always present
    message_id: str  # wamid.xxx
    from_phone: str  # sender's phone number (e.g. "85268280680")
    timestamp: str  # unix timestamp string
    message_type: str  # text, image, video, audio, document, sticker,
    # location, contacts, reaction, interactive, order,
    # button, referral, system, unknown

    # Content — depends on message_type
    text: str | None = None  # text, button
    media: MediaInfo | None = None  # image, video, audio, document, sticker
    location: LocationInfo | None = None
    contacts: list[ContactCard] | None = None
    reaction: ReactionInfo | None = None
    interactive: InteractiveReply | None = None
    referral: ReferralInfo | None = None
    order: OrderInfo | None = None

    # Context — present when user swipe-replies to a message
    context: ContextInfo | None = None

    # Forwarded message flag
    is_forwarded: bool = False
    # Frequently forwarded (forwarded 5+ times, Meta flags these)
    is_frequently_forwarded: bool = False

    # The full raw message dict for anything we don't parse
    raw: dict = field(default_factory=dict)


@dataclass
class StatusUpdate:
    """Message status change (sent, delivered, read, failed)."""
    message_id: str
    status: str  # sent, delivered, read, failed
    timestamp: str
    recipient_phone: str
    errors: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------

class WebhookHandler:
    """Parses and validates WhatsApp Cloud API webhooks."""

    def __init__(
        self,
        verify_token: str | None = None,
        app_secret: str | None = None,
    ):
        self.verify_token = verify_token or settings.whatsapp_verify_token
        self.app_secret = app_secret or settings.whatsapp_app_secret

    # ----- Webhook verification (GET) -----

    def verify_subscription(self, mode: str, token: str, challenge: str) -> str | None:
        """
        Handle Meta's webhook verification GET request.
        Returns the challenge string if valid, None if invalid.
        """
        if mode == "subscribe" and token == self.verify_token:
            logger.info("Webhook verified successfully")
            return challenge
        logger.warning("Webhook verification failed: mode=%s token_match=%s", mode, token == self.verify_token)
        return None

    # ----- Signature validation -----

    def validate_signature(self, payload: bytes, signature_header: str) -> bool:
        """
        Validate X-Hub-Signature-256 header against the payload.
        signature_header format: "sha256=<hex_digest>"
        """
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected_sig = signature_header[7:]  # strip "sha256="
        computed_sig = hmac.new(
            self.app_secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed_sig, expected_sig)

    # ----- Payload parsing -----

    def parse_payload(self, payload: dict) -> tuple[list[InboundMessage], list[StatusUpdate]]:
        """
        Parse a webhook payload into lists of messages and status updates.
        A single webhook can contain multiple entries and changes.
        """
        messages: list[InboundMessage] = []
        statuses: list[StatusUpdate] = []

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if change.get("field") != "messages":
                    continue

                # Parse messages
                for msg in value.get("messages", []):
                    parsed = self._parse_message(msg, value.get("metadata", {}))
                    if parsed:
                        messages.append(parsed)

                # Parse status updates
                for status in value.get("statuses", []):
                    parsed_status = self._parse_status(status)
                    if parsed_status:
                        statuses.append(parsed_status)

        return messages, statuses

    def _parse_message(self, msg: dict, metadata: dict) -> InboundMessage | None:
        """Parse a single message object from the webhook."""
        msg_type = msg.get("type", "unknown")

        base = InboundMessage(
            message_id=msg.get("id", ""),
            from_phone=msg.get("from", ""),
            timestamp=msg.get("timestamp", ""),
            message_type=msg_type,
            raw=msg,
        )

        # Context — can be a swipe-reply OR a forwarded message (or both)
        if "context" in msg:
            ctx = msg["context"]
            # Forwarded message detection
            base.is_forwarded = ctx.get("forwarded", False)
            base.is_frequently_forwarded = ctx.get("frequently_forwarded", False)
            # Only set reply context if there's an actual message ID (not just forwarded)
            if ctx.get("id"):
                base.context = ContextInfo(
                    message_id=ctx.get("id", ""),
                    from_phone=ctx.get("from"),
                )

        # Referral (click-to-WhatsApp ads)
        if "referral" in msg:
            ref = msg["referral"]
            base.referral = ReferralInfo(
                source_url=ref.get("source_url"),
                source_type=ref.get("source_type"),
                source_id=ref.get("source_id"),
                headline=ref.get("headline"),
                body=ref.get("body"),
                media_type=ref.get("media_type"),
                media_url=ref.get("media_url"),
            )

        # Type-specific parsing
        if msg_type == "text":
            base.text = msg.get("text", {}).get("body", "")

        elif msg_type in ("image", "video", "audio", "document", "sticker"):
            media_data = msg.get(msg_type, {})
            base.media = MediaInfo(
                media_id=media_data.get("id", ""),
                mime_type=media_data.get("mime_type", ""),
                sha256=media_data.get("sha256"),
                filename=media_data.get("filename"),
                caption=media_data.get("caption"),
                voice=media_data.get("voice", False),
            )
            if media_data.get("caption"):
                base.text = media_data["caption"]

        elif msg_type == "location":
            loc = msg.get("location", {})
            base.location = LocationInfo(
                latitude=loc.get("latitude", 0.0),
                longitude=loc.get("longitude", 0.0),
                name=loc.get("name"),
                address=loc.get("address"),
            )

        elif msg_type == "contacts":
            raw_contacts = msg.get("contacts", [])
            base.contacts = []
            for c in raw_contacts:
                base.contacts.append(ContactCard(
                    name=c.get("name", {}),
                    phones=c.get("phones", []),
                    emails=c.get("emails", []),
                    addresses=c.get("addresses", []),
                    org=c.get("org"),
                    urls=c.get("urls", []),
                ))

        elif msg_type == "reaction":
            rxn = msg.get("reaction", {})
            base.reaction = ReactionInfo(
                message_id=rxn.get("message_id", ""),
                emoji=rxn.get("emoji", ""),
            )

        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            reply_type = interactive.get("type", "")
            reply_data = interactive.get(reply_type, {})
            base.interactive = InteractiveReply(
                reply_type=reply_type,
                reply_id=reply_data.get("id", ""),
                reply_title=reply_data.get("title", ""),
                reply_description=reply_data.get("description"),
            )

        elif msg_type == "order":
            order_data = msg.get("order", {})
            base.order = OrderInfo(
                catalog_id=order_data.get("catalog_id", ""),
                products=order_data.get("product_items", []),
                text=order_data.get("text"),
            )

        elif msg_type == "button":
            base.text = msg.get("button", {}).get("text", "")

        elif msg_type == "system":
            base.text = msg.get("system", {}).get("body", "")

        else:
            logger.warning("Unknown message type: %s", msg_type)

        return base

    def _parse_status(self, status: dict) -> StatusUpdate:
        """Parse a status update from the webhook."""
        return StatusUpdate(
            message_id=status.get("id", ""),
            status=status.get("status", ""),
            timestamp=status.get("timestamp", ""),
            recipient_phone=status.get("recipient_id", ""),
            errors=status.get("errors", []),
            raw=status,
        )

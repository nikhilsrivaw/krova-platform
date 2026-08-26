"""
Parsing WhatsApp Cloud API webhooks.

Structure, per Meta's payload reference:

    object: "whatsapp_business_account"
    entry[]                          .id is the WABA id
      changes[]
        field: "messages"            same field for inbound AND statuses
        value
          metadata.phone_number_id   which of our numbers received it
          contacts[]                 .wa_id, .profile.name
          messages[]                 inbound
          statuses[]                 delivery receipts for what we sent
          errors[]                   delivery failures

Three things this module exists to get right:

  One payload can carry several entries, several changes, and several messages.
  Handling only the first is a bug that hides until a busy morning.

  Inbound messages and status updates arrive under the same field name. Telling
  them apart means looking for which key is present, not which field it claims.

  Anything unrecognised is preserved rather than dropped. New message types
  appear without warning, and the raw payload is what lets us replay history
  once we understand them.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from shared.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class InboundMessage:
    """One message a customer sent us."""

    waba_id: str
    phone_number_id: str          # which of our numbers received it
    display_phone_number: str | None
    external_id: str              # wamid - the idempotency key
    from_phone: str               # sender, as WhatsApp gives it
    profile_name: str | None
    message_type: str             # text | image | audio | document | ...
    text: str | None
    occurred_at: datetime
    media: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StatusUpdate:
    """What happened to a message we sent."""

    waba_id: str
    phone_number_id: str
    external_id: str              # the wamid we were given when sending
    status: str                   # sent | delivered | read | failed
    recipient_phone: str
    occurred_at: datetime
    # Present on billable messages. This is where per-message cost is learned,
    # and the category (marketing / utility / service) is what it turns on.
    pricing_category: str | None = None
    billable: bool | None = None
    errors: list[dict] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedWebhook:
    messages: list[InboundMessage] = field(default_factory=list)
    statuses: list[StatusUpdate] = field(default_factory=list)
    # Fields we don't handle yet: account_update, quality updates, template
    # status changes. Kept so nothing arrives unnoticed.
    other: list[dict] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.messages or self.statuses or self.other)


def _timestamp(value: Any) -> datetime:
    """WhatsApp sends Unix seconds as a string."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        # Never lose a message over an unreadable timestamp. Now is wrong but
        # recoverable; dropping it is not.
        logger.warning("unreadable webhook timestamp %r, using now", value)
        return datetime.now(timezone.utc)


def _extract_text(message: dict) -> tuple[str | None, dict]:
    """
    Pull readable text and any media reference out of a message.

    Returns (text, media). Everything a human could read becomes text, because
    that is what the extraction layer reads; media keeps its ids for later
    download.
    """
    kind = message.get("type", "")
    media: dict[str, Any] = {}

    match kind:
        case "text":
            return message.get("text", {}).get("body"), media

        case "image" | "video" | "audio" | "document" | "sticker":
            payload = message.get(kind, {}) or {}
            media = {
                "kind": kind,
                "id": payload.get("id"),
                "mime_type": payload.get("mime_type"),
                "sha256": payload.get("sha256"),
                "filename": payload.get("filename"),
            }
            # A caption is what the customer actually wrote.
            return payload.get("caption"), media

        case "button":
            return (message.get("button", {}) or {}).get("text"), media

        case "interactive":
            interactive = message.get("interactive", {}) or {}
            for key in ("button_reply", "list_reply"):
                if key in interactive:
                    reply = interactive[key] or {}
                    return reply.get("title") or reply.get("id"), media
            return None, media

        case "location":
            loc = message.get("location", {}) or {}
            media = {"kind": "location", **loc}
            name = loc.get("name") or loc.get("address")
            return name or f"Location: {loc.get('latitude')}, {loc.get('longitude')}", media

        case "reaction":
            reaction = message.get("reaction", {}) or {}
            return reaction.get("emoji"), media

        case "contacts":
            return "Shared a contact", {"kind": "contacts", "contacts": message.get("contacts")}

        case "order":
            return "Placed an order", {"kind": "order", "order": message.get("order")}

        case _:
            logger.info("unhandled WhatsApp message type %r - stored raw", kind)
            return None, media


def parse(payload: dict) -> ParsedWebhook:
    """
    Turn a webhook body into messages and status updates.

    Never raises on a malformed payload. Meta expects a 200 quickly, and
    returning 500 on something we could not read makes Meta retry it - then
    disable the webhook if it keeps failing. Log it, keep what parsed, move on.
    """
    result = ParsedWebhook()

    if payload.get("object") != "whatsapp_business_account":
        logger.warning("ignoring webhook for object %r", payload.get("object"))
        return result

    for entry in payload.get("entry") or []:
        waba_id = str(entry.get("id", ""))

        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id", ""))
            display_phone = metadata.get("display_phone_number")

            # wa_id -> profile name, so a message can be attributed to a person
            # rather than a bare number the first time they write.
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts") or []
            }

            for message in value.get("messages") or []:
                try:
                    text, media = _extract_text(message)
                    sender = str(message.get("from", ""))
                    result.messages.append(
                        InboundMessage(
                            waba_id=waba_id,
                            phone_number_id=phone_number_id,
                            display_phone_number=display_phone,
                            external_id=str(message.get("id", "")),
                            from_phone=sender,
                            profile_name=names.get(sender),
                            message_type=str(message.get("type", "unknown")),
                            text=text,
                            occurred_at=_timestamp(message.get("timestamp")),
                            media=media,
                            raw=message,
                        )
                    )
                except Exception:
                    logger.exception("could not parse a WhatsApp message, skipping it")

            for status in value.get("statuses") or []:
                try:
                    pricing = status.get("pricing") or {}
                    result.statuses.append(
                        StatusUpdate(
                            waba_id=waba_id,
                            phone_number_id=phone_number_id,
                            external_id=str(status.get("id", "")),
                            status=str(status.get("status", "")),
                            recipient_phone=str(status.get("recipient_id", "")),
                            occurred_at=_timestamp(status.get("timestamp")),
                            pricing_category=pricing.get("category"),
                            billable=pricing.get("billable"),
                            errors=status.get("errors") or [],
                            raw=status,
                        )
                    )
                except Exception:
                    logger.exception("could not parse a WhatsApp status, skipping it")

            if not value.get("messages") and not value.get("statuses"):
                result.other.append(
                    {"field": change.get("field"), "waba_id": waba_id, "value": value}
                )

    return result

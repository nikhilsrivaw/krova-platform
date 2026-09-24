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
    # Present only when this is the first message after a Click-to-WhatsApp
    # ad click - Meta never repeats it on later messages from the same
    # person, so this is the one chance to capture it. None the rest of the
    # time, which is the normal case.
    referral: dict[str, Any] | None = None
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
class NativeOrderItem:
    product_retailer_id: str
    quantity: int
    price_paise: int | None


@dataclass(slots=True)
class NativeOrder:
    """
    A cart the customer built and submitted from the business's own Meta
    catalog, inside WhatsApp itself - not synced in from Shopify, this
    happened entirely within the chat. Deliberately shaped to map onto the
    same Order model shopify's parser fills in
    (shared/channels/shopify/webhook.py) - one order table, whichever
    channel it came from, rather than a second one only for chat-native
    orders.
    """

    catalog_id: str | None
    note: str | None
    items: list[NativeOrderItem]
    total_paise: int | None


def _paise_from_decimal_string(value: Any) -> int | None:
    if value is None:
        return None
    try:
        from decimal import Decimal

        return int((Decimal(str(value)) * 100).to_integral_value())
    except Exception:
        return None


def parse_native_order(order_data: dict) -> NativeOrder:
    """Turn the raw `order` object WhatsApp sends into something Order-shaped."""
    items = []
    total = 0
    any_price = False
    for raw_item in order_data.get("product_items") or []:
        price = _paise_from_decimal_string(raw_item.get("item_price"))
        try:
            quantity = int(raw_item.get("quantity", 1))
        except (TypeError, ValueError):
            quantity = 1
        if price is not None:
            total += price * quantity
            any_price = True
        items.append(
            NativeOrderItem(
                product_retailer_id=str(raw_item.get("product_retailer_id", "")),
                quantity=quantity,
                price_paise=price,
            )
        )
    return NativeOrder(
        catalog_id=order_data.get("catalog_id"),
        note=order_data.get("text") or None,
        items=items,
        total_paise=total if any_price else None,
    )


@dataclass(slots=True)
class EchoMessage:
    """
    One message the business itself sent from the WhatsApp Business app on
    their own phone, mirrored to us by Meta's Coexistence feature.

    This is what makes a sales team's real conversations visible without
    asking anyone to change how they work: the rep quotes a dealer from
    their phone exactly as they always have, and the message still lands
    here. Arrives under its own webhook field (`smb_message_echoes`), not
    under `messages`, and carries `to` - the customer - where an inbound
    message carries `from`.

    Stored as an outbound message, so commitment extraction runs over it
    (a promise a rep made is exactly the thing worth catching) while reply
    drafting does not - a human has already answered.
    """

    waba_id: str
    phone_number_id: str
    display_phone_number: str | None
    external_id: str              # wamid - idempotency key, per Meta's own guidance
    to_phone: str                 # the customer this was sent to
    message_type: str
    text: str | None
    occurred_at: datetime
    media: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HistoryMessage:
    """
    One message from the chat history Meta backfills at Coexistence
    onboarding - up to six months of the business's existing 1:1 threads.

    This is the part that turns a new connection into instant institutional
    memory: the dealer relationships that previously existed only on a
    rep's handset arrive as real, queryable conversation. Group chats are
    excluded by Meta and never appear here.

    Direction is derived by comparing the message's `from` against the
    business's own display number, because a history thread carries both
    sides - unlike `messages` (always inbound) or `message_echoes` (always
    outbound).
    """

    waba_id: str
    phone_number_id: str
    display_phone_number: str | None
    external_id: str
    customer_phone: str           # the thread's counterparty
    is_outbound: bool             # the business sent it
    message_type: str
    text: str | None
    occurred_at: datetime
    media: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedWebhook:
    messages: list[InboundMessage] = field(default_factory=list)
    statuses: list[StatusUpdate] = field(default_factory=list)
    # Messages the business sent from their own phone (Coexistence).
    echoes: list[EchoMessage] = field(default_factory=list)
    # Backfilled history from Coexistence onboarding.
    history: list[HistoryMessage] = field(default_factory=list)
    # Fields we don't handle yet: account_update, quality updates, template
    # status changes. Kept so nothing arrives unnoticed.
    other: list[dict] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(
            self.messages or self.statuses or self.echoes or self.history or self.other
        )


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
            button = message.get("button", {}) or {}
            # `payload` is the developer-defined value baked into the
            # template's own approved quick-reply button at registration
            # time - stable and unique-by-design, unlike `text` (the
            # human-readable label, which a caller must not match on for
            # anything that needs to be reliable, since two unrelated
            # templates could reuse the same visible label). Additive:
            # every existing reader of this tuple's `text` half is
            # unaffected, this only adds a new key to `media`.
            if button.get("payload"):
                media = {"kind": "button_reply", "payload": button.get("payload")}
            return button.get("text"), media

        case "interactive":
            interactive = message.get("interactive", {}) or {}
            for key in ("button_reply", "list_reply"):
                if key in interactive:
                    reply = interactive[key] or {}
                    return reply.get("title") or reply.get("id"), media
            if "nfm_reply" in interactive:
                # A completed WhatsApp Flow. response_json arrives as a JSON
                # *string*, not an object - Meta's one inconsistency in an
                # otherwise all-object payload, and worth decoding here so
                # every downstream reader gets real fields, not a string to
                # re-parse.
                reply = interactive["nfm_reply"] or {}
                import json as _json

                try:
                    fields = _json.loads(reply.get("response_json") or "{}")
                except (TypeError, ValueError):
                    fields = {}
                media = {
                    "kind": "flow_reply",
                    "flow_name": reply.get("name"),
                    # WhatsApp echoes the flow_token we sent back inside
                    # response_json itself once the flow's terminal screen
                    # completes - that is the join key to FlowSendLog, not
                    # anything in the envelope around it.
                    "flow_token": fields.get("flow_token"),
                    "fields": {k: v for k, v in fields.items() if k != "flow_token"},
                }
                readable = media["fields"]
                summary = ", ".join(f"{k}: {v}" for k, v in readable.items()) if readable else None
                return (f"Completed a form: {summary}" if summary else "Completed a form"), media
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

        case "order_status":
            # A payment gateway's own status update on a WhatsApp Payments
            # (India) order-details message - arrives as an inbound message
            # of this type, not (only) inside the statuses array. UNVERIFIED
            # exact field nesting against a live send - the raw payload is
            # kept in full either way, so nothing is lost even if the
            # specific keys read here turn out wrong; see
            # services/api/routers/webhooks.py::_apply_payment_status.
            os_ = message.get("order_status", {}) or {}
            return "Order status update", {
                "kind": "order_status",
                "reference_id": os_.get("reference_id") or (os_.get("order") or {}).get("reference_id"),
                "order_status": os_,
            }

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
                            referral=message.get("referral"),
                            raw=message,
                        )
                    )
                except Exception:
                    logger.exception("could not parse a WhatsApp message, skipping it")

            # Coexistence: what the business sent from their own phone.
            # Same message shape as an inbound one, so _extract_text applies
            # unchanged - only the direction and the counterparty differ.
            for echo in value.get("message_echoes") or []:
                try:
                    text, media = _extract_text(echo)
                    recipient = str(echo.get("to", ""))
                    if not recipient:
                        # Without a recipient there is no customer to attribute
                        # this to, and a message attributed to nobody is worse
                        # than one we skipped.
                        logger.warning("message echo with no 'to', skipping")
                        continue
                    result.echoes.append(
                        EchoMessage(
                            waba_id=waba_id,
                            phone_number_id=phone_number_id,
                            display_phone_number=display_phone,
                            external_id=str(echo.get("id", "")),
                            to_phone=recipient,
                            message_type=str(echo.get("type", "unknown")),
                            text=text,
                            occurred_at=_timestamp(echo.get("timestamp")),
                            media=media,
                            raw=echo,
                        )
                    )
                except Exception:
                    logger.exception("could not parse a WhatsApp message echo, skipping it")

            # Coexistence onboarding backfill. Structure is threads-of-
            # messages rather than a flat list, and each message carries both
            # `from` and `to`, so direction is derived rather than implied by
            # which key it arrived under.
            #
            # The inner shape (threads[].messages[] with from/to) is
            # documented; the outer Cloud-API wrapper is inferred from how
            # every other field on this webhook is shaped. Anything that does
            # not match is logged loudly rather than silently dropped, so a
            # real mismatch surfaces the first time it happens.
            for chunk in value.get("history") or []:
                if not isinstance(chunk, dict):
                    logger.warning("history chunk was not an object, skipping")
                    continue
                for thread in chunk.get("threads") or []:
                    counterparty = str(thread.get("id", ""))
                    if not counterparty:
                        logger.warning("history thread with no id, skipping")
                        continue
                    for message in thread.get("messages") or []:
                        try:
                            text, media = _extract_text(message)
                            sender = str(message.get("from", ""))
                            # The business's own number identifies its side of
                            # the thread. Without a display number to compare
                            # against, fall back to "not the counterparty".
                            if display_phone:
                                is_outbound = sender == str(display_phone)
                            else:
                                is_outbound = sender != counterparty
                            result.history.append(
                                HistoryMessage(
                                    waba_id=waba_id,
                                    phone_number_id=phone_number_id,
                                    display_phone_number=display_phone,
                                    external_id=str(message.get("id", "")),
                                    customer_phone=counterparty,
                                    is_outbound=is_outbound,
                                    message_type=str(message.get("type", "unknown")),
                                    text=text,
                                    occurred_at=_timestamp(message.get("timestamp")),
                                    media=media,
                                    raw=message,
                                )
                            )
                        except Exception:
                            logger.exception("could not parse a history message, skipping it")

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

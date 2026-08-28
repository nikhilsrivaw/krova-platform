"""
Turning Meta's raw Instagram webhook payload into something the rest of the
platform can use - the Instagram equivalent of shared/channels/whatsapp/webhook.py.

Two shapes arrive under the same `object: "instagram"` envelope, and they
look nothing alike: a DM sits in `entry[].messaging[]`, a comment sits in
`entry[].changes[]` with `field: "comments"`. Both get parsed here so the
webhook route itself never has to know the difference.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class InboundDirectMessage:
    """One DM a customer sent this Instagram Business account."""

    ig_account_id: str        # which of our connected accounts received it
    external_id: str          # mid - the idempotency key
    from_ig_id: str           # sender's Instagram-scoped id
    text: str | None
    occurred_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InboundComment:
    """A comment on one of our posts or Reels."""

    ig_account_id: str
    external_id: str          # the comment's own id - idempotency key
    from_ig_id: str
    from_username: str | None
    text: str | None
    media_id: str | None
    occurred_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedInstagramWebhook:
    messages: list[InboundDirectMessage] = field(default_factory=list)
    comments: list[InboundComment] = field(default_factory=list)
    # Fields we don't handle yet: messaging_postbacks, mentions, story
    # replies. Kept so nothing arrives unnoticed rather than silently
    # dropped.
    other: list[dict] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.messages or self.comments or self.other)


def _as_datetime(ms: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def parse(payload: dict) -> ParsedInstagramWebhook:
    """
    Read one Instagram webhook delivery.

    Every entry belongs to one Instagram Business account (entry["id"]) -
    that id is what find_connection() in ingest-time lookups will key off,
    the same way phone_number_id does for WhatsApp.
    """
    result = ParsedInstagramWebhook()

    if payload.get("object") != "instagram":
        return result

    for entry in payload.get("entry") or []:
        ig_account_id = str(entry.get("id") or "")

        for msg in entry.get("messaging") or []:
            message = msg.get("message") or {}
            mid = message.get("mid")
            sender_id = (msg.get("sender") or {}).get("id")
            if not mid or not sender_id:
                continue
            # Echoes of our own sent messages arrive here too - not customer
            # input, and ingesting them as inbound would put our own words
            # in the customer's mouth.
            if message.get("is_echo"):
                continue
            result.messages.append(
                InboundDirectMessage(
                    ig_account_id=ig_account_id,
                    external_id=str(mid),
                    from_ig_id=str(sender_id),
                    text=message.get("text"),
                    occurred_at=_as_datetime(msg.get("timestamp")),
                    raw=msg,
                )
            )

        for change in entry.get("changes") or []:
            if change.get("field") != "comments":
                result.other.append(change)
                continue
            value = change.get("value") or {}
            comment_id = value.get("id")
            from_data = value.get("from") or {}
            from_id = from_data.get("id")
            if not comment_id or not from_id:
                continue
            result.comments.append(
                InboundComment(
                    ig_account_id=ig_account_id,
                    external_id=str(comment_id),
                    from_ig_id=str(from_id),
                    from_username=from_data.get("username"),
                    text=value.get("text"),
                    media_id=(value.get("media") or {}).get("id"),
                    occurred_at=_as_datetime(entry.get("time", 0) * 1000 if entry.get("time") else None),
                    raw=change,
                )
            )

    return result

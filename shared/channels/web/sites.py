"""
Website chat for a business's own site: which sites may run the widget, and a person's replies reaching it.

A business adds its site on the Website chat page (services/api/routers/web_chat.py) and pastes the snippet. The
domain is kept bare ("example.com"); the widget then works on example.com and www.example.com, nothing else.

A person can reply to a visitor from the Inbox once the visitor has given a phone number (before that there is no
customer to reply to - shared/channels/web/reply.py). The reply is stored as an outbound website message with the
person on it, and the open widget picks it up by asking every few seconds (GET /widget/{site_key}/updates). While
a person is in the conversation the AI stays out of it, so the visitor never gets two answers.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Channel, Direction, Message, WebSession

# A person who replied this recently is talking to the visitor - the AI does not answer over them.
PERSON_ACTIVE = timedelta(minutes=15)
# A visitor seen on the site this recently can still get a person's reply (it shows when they next open the chat).
REPLY_WINDOW = timedelta(days=7)

_DOMAIN = re.compile(r"^(?=.{3,255}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_domain(raw: str) -> str | None:
    """'https://www.Example.com/contact' -> 'example.com'. None when it is not a real domain."""
    text = (raw or "").strip().lower()
    if not text:
        return None
    host = urlparse(text if "://" in text else f"//{text}").hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host if _DOMAIN.match(host) else None


def host_allowed(origin_host: str | None, domain: str) -> bool:
    """The widget runs on the site's own domain and its www. form only."""
    if not origin_host:
        return False
    host = origin_host.lower()
    return host == domain or host == f"www.{domain}"


def clean_colour(raw: str | None) -> str | None:
    return raw if raw and _COLOUR.match(raw) else None


async def person_replied_recently(db: AsyncSession, customer_id: uuid.UUID, now: datetime | None = None) -> bool:
    """A person sent this customer a website message in the last few minutes."""
    since = (now or datetime.now(timezone.utc)) - PERSON_ACTIVE
    row = await db.execute(
        select(Message.id).where(
            Message.customer_id == customer_id, Message.channel == Channel.web,
            Message.direction == Direction.outbound, Message.sent_by_user_id.is_not(None),
            Message.occurred_at > since,
        ).limit(1)
    )
    return row.first() is not None


async def latest_session(db: AsyncSession, business_id: uuid.UUID, customer_id: uuid.UUID) -> WebSession | None:
    """The visitor's most recent website chat, if they have had one."""
    rows = await db.execute(
        select(WebSession).where(WebSession.business_id == business_id, WebSession.customer_id == customer_id)
        .order_by(WebSession.last_seen_at.desc()).limit(1)
    )
    return rows.scalars().first()


async def team_replies(db: AsyncSession, customer_id: uuid.UUID, after: datetime | None, limit: int = 20) -> list[Message]:
    """A person's website messages to this customer after `after` (oldest first) - what the open widget shows."""
    query = select(Message).where(
        Message.customer_id == customer_id, Message.channel == Channel.web,
        Message.direction == Direction.outbound, Message.sent_by_user_id.is_not(None),
    )
    if after is not None:
        query = query.where(Message.occurred_at > after)
    rows = await db.execute(query.order_by(Message.occurred_at.asc()).limit(limit))
    return list(rows.scalars().all())

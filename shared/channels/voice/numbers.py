"""
A business can own several voice numbers. This is the one place that knows how to tell them
apart and which to use for what.

Each number is a ChannelConnection. What the owner says about a number lives in its `extra`:

  label            "Sales", "Delhi branch" - what the owner calls it (the number itself if blank)
  use              "both" (default), "inbound" or "outbound"
  default_outbound at most one per business: the number calls go out from unless something more
                   specific applies

Its greeting, language, voice, staff phone and keypad menu already lived in `extra`, so every
number has always been configurable on its own; what was missing was choosing between them.

Outbound calls never "take the first number" any more. They ask pick_outbound(), which prefers:
  1. the number the caller named (a campaign's "call from", say), if it may call out;
  2. for a campaign with a purpose, a number whose series is allowed for it - India keeps
     promotional calls on 140 and service calls on 080/022, so one business with both kinds of
     number must not be sent out of the wrong one;
  3. the number marked default for outgoing calls;
  4. the oldest number that may call out - the same answer every time, not whichever row the
     database happened to return.
"""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Channel, ChannelConnection, ConnectionStatus

USES = ("both", "inbound", "outbound")

# India: service and transactional calls on 080 or 022, promotional on 140 (160 is for regulated
# financial entities and is recognised but not offered to ordinary businesses).
SERIES_FOR_PURPOSE: dict[str, set[str]] = {"service": {"080", "022"}, "promotional": {"140"}}

SERIES_LABEL = {"080": "Service (080)", "022": "Service (022)", "140": "Promotional (140)", "160": "Regulated (160)"}


def series_of(number: str | None) -> str | None:
    """The Indian number series a phone number belongs to, read from its digits."""
    digits = re.sub(r"\D", "", number or "")
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[2:]
    if digits.startswith("140"):
        return "140"
    if digits.startswith("160"):
        return "160"
    if digits.startswith("80"):
        return "080"
    if digits.startswith("22"):
        return "022"
    return None


def use_of(connection: ChannelConnection) -> str:
    value = (connection.extra or {}).get("use")
    return value if value in USES else "both"


def label_of(connection: ChannelConnection) -> str:
    return str((connection.extra or {}).get("label") or "").strip() or connection.external_account_id


def is_default_outbound(connection: ChannelConnection) -> bool:
    return bool((connection.extra or {}).get("default_outbound"))


def allows_outbound(connection: ChannelConnection) -> bool:
    return use_of(connection) in ("both", "outbound")


def allows_inbound(connection: ChannelConnection) -> bool:
    return use_of(connection) in ("both", "inbound")


async def active(db: AsyncSession, business_id: uuid.UUID) -> list[ChannelConnection]:
    """Every connected voice number, oldest first (a stable order, so 'the first' is always the same one)."""
    rows = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.voice,
            ChannelConnection.status == ConnectionStatus.active,
        ).order_by(ChannelConnection.created_at, ChannelConnection.id)
    )
    return list(rows.scalars().all())


def choose_outbound(
    numbers: list[ChannelConnection], *, purpose: str | None = None, preferred: uuid.UUID | None = None,
) -> ChannelConnection | None:
    """The pure part of pick_outbound: choose among numbers already loaded (oldest first)."""
    callers = [c for c in numbers if allows_outbound(c)]
    if not callers:
        return None
    if preferred is not None:
        return next((c for c in callers if c.id == preferred), None)

    pool = callers
    allowed = SERIES_FOR_PURPOSE.get(purpose or "")
    if allowed:
        matching = [c for c in callers if series_of(c.external_account_id) in allowed]
        # None matching: fall through to the default, so the caller's own check can say precisely
        # why this number is wrong rather than the business hearing "no number".
        pool = matching or callers

    return next((c for c in pool if is_default_outbound(c)), pool[0])


async def pick_outbound(
    db: AsyncSession, business_id: uuid.UUID, *, purpose: str | None = None, preferred: uuid.UUID | None = None,
) -> ChannelConnection | None:
    """The number an outgoing call for this business should use. See the module docstring for the order."""
    return choose_outbound(await active(db, business_id), purpose=purpose, preferred=preferred)


def staff_phone_of(numbers: list[ChannelConnection]) -> str | None:
    """
    The business's staff phone for alerts and transfers. It is set per number, so take the default
    outgoing number's if it has one, else the first number that has any.
    """
    ordered = sorted(numbers, key=lambda c: not is_default_outbound(c))
    for connection in ordered:
        phone = (connection.extra or {}).get("staff_phone_number")
        if phone:
            return str(phone)
    return None


async def staff_phone(db: AsyncSession, business_id: uuid.UUID) -> str | None:
    """
    Where an urgent alert SMS goes: the first person with a phone on the default number's "who answers" list
    (then the other numbers'), else the old per-number staff phone.
    """
    from shared.channels.voice import line_profile

    connections = await active(db, business_id)
    for connection in sorted(connections, key=lambda c: not is_default_outbound(c)):
        phone = await line_profile.first_phone(db, business_id, connection.extra)
        if phone:
            return phone
    return staff_phone_of(connections)


class NumberError(ValueError):
    """The change to a number cannot be made; the message is shown to the owner."""


def clean_label(value: str | None) -> str:
    label = " ".join((value or "").split())
    if len(label) > 40:
        raise NumberError("A number's name can be at most 40 characters.")
    return label


def clean_use(value: str | None) -> str:
    if value not in USES:
        raise NumberError("A number can be for incoming calls, outgoing calls, or both.")
    return value


def apply_default(numbers: list[ChannelConnection], chosen: ChannelConnection, make_default: bool) -> None:
    """
    Make `chosen` the default for outgoing calls (clearing it from the others), or clear it. The
    default must be able to call out, so an incoming-only number cannot be it.
    """
    if make_default:
        if not allows_outbound(chosen):
            raise NumberError("A number that only takes incoming calls cannot be the default for outgoing calls.")
        for connection in numbers:
            extra = dict(connection.extra or {})
            extra["default_outbound"] = connection.id == chosen.id
            connection.extra = extra
    else:
        extra = dict(chosen.extra or {})
        extra["default_outbound"] = False
        chosen.extra = extra

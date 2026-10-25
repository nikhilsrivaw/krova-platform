"""
Who a call should ring.

Before this a business had one staff phone number, so on a team of five every
transferred call rang the same phone. Now each member can have their own number
(BusinessMember.phone) and a call goes to the right one:

  1. the customer's own team member, if they are on the team, not Away, and have a number;
  2. then the other available members, the one handed a chat longest ago first;
  3. then the business's old staff number, if it has one.

They are tried one after another, each for a short time, so a customer is not left
ringing a dozen phones at once and we always know exactly who picked up. A business
that never set a member phone behaves as before: it rings its one staff number.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import BusinessMember, Customer, User

# Members tried before the business's own number. Each attempt rings ~18 seconds, and a
# customer should not wait on the line longer than a minute and a bit.
MAX_MEMBERS = 3
RING_SECONDS = 18


@dataclass(frozen=True, slots=True)
class Target:
    number: str          # E.164 without the plus
    user_id: uuid.UUID | None  # None for the business's own fallback number
    name: str
    # Set only by a number's own "who answers" list (shared/channels/voice/line_profile.py): targets sharing a
    # step ring together, and each step rings for its own time. None = the old one-at-a-time chain.
    step: int | None = None
    ring_seconds: int = 18
    together: bool = False


async def targets_for(
    db: AsyncSession, *, business_id: uuid.UUID, customer_id: uuid.UUID | None, fallback: str | None,
) -> list[Target]:
    """The ordered list of numbers to try for a call involving this customer."""
    rows = (await db.execute(
        select(BusinessMember, User)
        .join(User, User.id == BusinessMember.user_id)
        .where(
            BusinessMember.business_id == business_id,
            BusinessMember.phone.is_not(None),
            BusinessMember.available == True,  # noqa: E712
            User.is_active == True,  # noqa: E712
        )
        .order_by(BusinessMember.last_routed_at.asc().nullsfirst(), BusinessMember.created_at.asc())
    )).all()

    owner_id = None
    if customer_id is not None:
        customer = await db.get(Customer, customer_id)
        if customer is not None and customer.business_id == business_id:
            owner_id = customer.assigned_to_user_id

    members = [(m, u) for m, u in rows if m.phone]
    members.sort(key=lambda pair: pair[0].user_id != owner_id)  # stable: the owner first, rest keep their order

    return order(
        [Target(m.phone, m.user_id, u.full_name or u.username or "a teammate") for m, u in members],
        fallback,
    )


def order(members: list[Target], fallback: str | None) -> list[Target]:
    """Cap the members, drop repeated numbers, and put the business's own number last."""
    out: list[Target] = []
    seen: set[str] = set()
    for target in members[:MAX_MEMBERS]:
        if target.number not in seen:
            seen.add(target.number)
            out.append(target)
    if fallback and fallback not in seen:
        out.append(Target(fallback, None, "the team"))
    return out


# ── the chain Plivo walks, one number at a time ─────────────────────────────

def encode(targets: list[Target]) -> str:
    """number:user,number:user - the user part is '-' for the business's own number."""
    return ",".join(f"{t.number}:{t.user_id or '-'}" for t in targets)


def encode_plan(targets: list[Target]) -> str:
    """Steps for a list with levels: 'ring[*]~number:user,number:user;...' - '*' marks a step that rings together."""
    steps: dict[int, list[Target]] = {}
    for t in targets:
        steps.setdefault(t.step or 0, []).append(t)
    parts = []
    for _, group in sorted(steps.items()):
        head = f"{group[0].ring_seconds}{'*' if group[0].together else ''}"
        parts.append(head + "~" + encode(group))
    return ";".join(parts)


@dataclass(frozen=True, slots=True)
class Step:
    ring_seconds: int
    together: bool
    stops: list[tuple[str, uuid.UUID | None]]


def decode_plan(raw: str) -> list[Step]:
    out: list[Step] = []
    for part in raw.split(";"):
        head, _, body = part.partition("~")
        together = head.endswith("*")
        try:
            ring = int(head.rstrip("*"))
        except ValueError:
            ring = RING_SECONDS
        stops = decode(body)
        if stops:
            out.append(Step(max(5, min(ring, 60)), together, stops))
    return out


def decode(raw: str) -> list[tuple[str, uuid.UUID | None]]:
    out: list[tuple[str, uuid.UUID | None]] = []
    for part in raw.split(","):
        number, _, user = part.partition(":")
        number = "".join(ch for ch in number if ch.isdigit())
        if not number:
            continue
        try:
            out.append((number, None if user in ("", "-") else uuid.UUID(user)))
        except ValueError:
            out.append((number, None))
    return out

"""
What one voice number is for, and who answers it - so a business with several numbers can give each its own job.

Everything lives in that number's ChannelConnection.extra (beside its greeting, voice and keypad menu), so a
number's settings travel with it and there is no table to migrate:

  role, role_note    what the AI is told on calls that come IN on this number ("this is the sales line ...")
  pickup             who a call is put through to, in levels: level 1 first, then level 2, then level 3. A level
                     rings its people one by one or all together (first to answer gets the call).
  no_answer          what happens when nobody picks up: a callback promise on the Ledger (default), a
                     voicemail, or just an apology.

A number with no pickup list behaves exactly as before: every team member with a phone, one at a time, then the
number's old staff phone (shared/team/call_routing.py). Team members are stored by id and their phone and
"Away" status are read at call time, so a change on the Team page applies to the next call.

See docs/new/voice-number-profiles.md.
"""

import uuid
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import BusinessMember, Customer, User
from shared.team import call_routing
from shared.team.call_routing import Target

ROLES: dict[str, dict[str, str]] = {
    "sales": {
        "label": "Sales",
        "note": "This is the sales line. Most callers are new customers: answer about what is sold and what it "
                "costs, and help them book, order or get a callback. Someone with a problem about an existing "
                "order or service should be handed to a person.",
    },
    "support": {
        "label": "Support",
        "note": "This is the support line. Most callers are existing customers with a question or a problem about "
                "an order, a booking or a service. Be patient, find out exactly what went wrong, and hand over to a "
                "person whenever you cannot fix it yourself.",
    },
    "booking": {
        "label": "Bookings",
        "note": "This line is for bookings and appointments: help callers book, move or cancel, and answer timing "
                "questions.",
    },
    "branch": {
        "label": "Branch",
        "note": "This number belongs to one branch or location of the business. Answer for that branch.",
    },
    "custom": {"label": "Custom", "note": ""},
}

NOTE_LIMIT = 400
MAX_LEVELS = 3
MAX_PEOPLE = 5
RING_RANGE = (10, 45)
DEFAULT_RING = 18
WAIT_RANGE = (30, 120)
DEFAULT_WAIT = 60
MODES = ("one_by_one", "together")
NO_ANSWER = ("callback", "voicemail", "apology")
CALLBACK_RANGE = (1, 48)
DEFAULT_CALLBACK_HOURS = 2


class ProfileError(ValueError):
    """A setting that cannot be saved; the message is shown to the owner."""


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def read(extra: dict | None) -> dict:
    """This number's profile with every default filled in. Never raises on old or odd data."""
    extra = extra or {}
    role = extra.get("role") if extra.get("role") in ROLES else None
    pickup = extra.get("pickup") or {}
    levels = []
    for level in (pickup.get("levels") or [])[:MAX_LEVELS]:
        people = [p for p in (level.get("people") or []) if isinstance(p, dict)][:MAX_PEOPLE]
        if people:
            levels.append({
                "mode": level.get("mode") if level.get("mode") in MODES else "one_by_one",
                "ring_seconds": _clamp(level.get("ring_seconds"), *RING_RANGE, DEFAULT_RING),
                "people": people,
            })
    return {
        "role": role,
        "role_note": str(extra.get("role_note") or "")[:NOTE_LIMIT],
        "pickup": {"levels": levels, "max_wait": _clamp(pickup.get("max_wait"), *WAIT_RANGE, DEFAULT_WAIT)},
        "no_answer": extra.get("no_answer") if extra.get("no_answer") in NO_ANSWER else "callback",
        "callback_hours": _clamp(extra.get("callback_hours"), *CALLBACK_RANGE, DEFAULT_CALLBACK_HOURS),
    }


def clean(raw: dict, *, member_ids: set[str], normalise_phone: Callable[[str], str]) -> dict:
    """What the owner sent, checked and trimmed into what is stored. Raises ProfileError with a plain reason."""
    role = raw.get("role") or None
    if role is not None and role not in ROLES:
        raise ProfileError("Choose one of the listed roles.")
    note = str(raw.get("role_note") or "").strip()
    if len(note) > NOTE_LIMIT:
        raise ProfileError(f"Keep what this number is for under {NOTE_LIMIT} characters.")

    levels_in = (raw.get("pickup") or {}).get("levels") or []
    if len(levels_in) > MAX_LEVELS:
        raise ProfileError(f"Up to {MAX_LEVELS} levels.")
    levels = []
    for n, level in enumerate(levels_in, start=1):
        people_in = level.get("people") or []
        if len(people_in) > MAX_PEOPLE:
            raise ProfileError(f"Level {n}: up to {MAX_PEOPLE} people.")
        people, seen = [], set()
        for person in people_in:
            if person.get("user_id"):
                uid = str(person["user_id"])
                if uid not in member_ids:
                    raise ProfileError(f"Level {n}: someone on the list is no longer on your team.")
                key, entry = uid, {"user_id": uid}
            else:
                name = str(person.get("name") or "").strip()[:40]
                try:
                    phone = normalise_phone(str(person.get("phone") or ""))
                except Exception as exc:  # noqa: BLE001 - the normaliser's own message is the useful one
                    raise ProfileError(f"Level {n}: {person.get('phone') or 'a number'} is not a phone number ({exc}).") from exc
                key, entry = phone, {"phone": phone, "name": name or "Team"}
            if key not in seen:
                seen.add(key)
                people.append(entry)
        if not people:
            continue
        mode = level.get("mode") or "one_by_one"
        if mode not in MODES:
            raise ProfileError("A level rings one by one or all together.")
        levels.append({
            "mode": mode, "ring_seconds": _clamp(level.get("ring_seconds"), *RING_RANGE, DEFAULT_RING), "people": people,
        })

    no_answer = raw.get("no_answer") or "callback"
    if no_answer not in NO_ANSWER:
        raise ProfileError("Choose what happens when nobody answers.")
    pickup_raw = raw.get("pickup") or {}
    return {
        "role": role,
        "role_note": note,
        "pickup": {"levels": levels, "max_wait": _clamp(pickup_raw.get("max_wait"), *WAIT_RANGE, DEFAULT_WAIT)},
        "no_answer": no_answer,
        "callback_hours": _clamp(raw.get("callback_hours"), *CALLBACK_RANGE, DEFAULT_CALLBACK_HOURS),
    }


def line_note(extra: dict | None, label: str) -> str | None:
    """What the AI is told about the number a caller rang. None when the owner set nothing."""
    profile = read(extra)
    preset = ROLES[profile["role"]]["note"] if profile["role"] else ""
    own = profile["role_note"].strip()
    if not preset and not own:
        return None
    name = f'"{label}"' if label else "this number"
    lines = [f"This caller rang {name}, one of the business's phone numbers."]
    if preset:
        lines.append(preset)
    if own:
        lines.append(f"The owner's own words about this number: {own}")
    return " ".join(lines)


# ── who a call is put through to ─────────────────────────────────────────────

async def _members(db: AsyncSession, business_id: uuid.UUID) -> dict[str, tuple[str, str]]:
    """Team members who can be rung right now: user id -> (phone, name). Away members are left out."""
    rows = (await db.execute(
        select(BusinessMember.user_id, BusinessMember.phone, User.full_name, User.username)
        .join(User, User.id == BusinessMember.user_id)
        .where(
            BusinessMember.business_id == business_id,
            BusinessMember.phone.is_not(None),
            BusinessMember.available == True,  # noqa: E712
            User.is_active == True,  # noqa: E712
        )
    )).all()
    return {str(uid): (phone, full or user or "a teammate") for uid, phone, full, user in rows if phone}


def build_plan(
    profile: dict, members: dict[str, tuple[str, str]], *, assigned_user_id: str | None,
) -> list[Target]:
    """
    The pure part: the ordered steps for a number with a pickup list. The customer's own member first, then each
    level - one step per person, or one step for a level that rings together. Stops adding steps once the next one
    would pass the number's longest wait.
    """
    levels = profile["pickup"]["levels"]
    budget = profile["pickup"]["max_wait"]
    out: list[Target] = []
    seen: set[str] = set()
    step = 0
    used = 0

    def person(entry: dict) -> tuple[str, uuid.UUID | None, str] | None:
        if entry.get("user_id"):
            found = members.get(str(entry["user_id"]))
            if found is None:
                return None
            return found[0], uuid.UUID(str(entry["user_id"])), found[1]
        if entry.get("phone"):
            return str(entry["phone"]), None, str(entry.get("name") or "the team")
        return None

    def add(people: list[tuple[str, uuid.UUID | None, str]], ring: int, together: bool) -> None:
        nonlocal step, used
        fresh = [p for p in people if p[0] not in seen]
        if not fresh or (out and used + ring > budget):
            return
        for number, uid, name in fresh:
            seen.add(number)
            out.append(Target(number, uid, name, step=step, ring_seconds=ring, together=together and len(fresh) > 1))
        step += 1
        used += ring

    first_ring = levels[0]["ring_seconds"] if levels else DEFAULT_RING
    if assigned_user_id:
        found = person({"user_id": assigned_user_id})
        if found:
            add([found], first_ring, False)
    for level in levels:
        people = [p for p in (person(e) for e in level["people"]) if p]
        together = level["mode"] == "together"
        if together:
            add(people, level["ring_seconds"], True)
        else:
            for p in people:
                add([p], level["ring_seconds"], False)
    return out


async def plan(
    db: AsyncSession, *, business_id: uuid.UUID, extra: dict | None, customer_id: uuid.UUID | None,
    fallback: str | None,
) -> list[Target]:
    """Who this call rings, in order. A number with no pickup list keeps the old team-wide chain."""
    profile = read(extra)
    if not profile["pickup"]["levels"]:
        return await call_routing.targets_for(db, business_id=business_id, customer_id=customer_id, fallback=fallback)
    customer = await db.get(Customer, customer_id) if customer_id else None
    if customer is not None and customer.business_id != business_id:
        customer = None
    return build_plan(
        profile, await _members(db, business_id),
        assigned_user_id=str(customer.assigned_to_user_id) if customer and customer.assigned_to_user_id else None,
    )


async def first_phone(db: AsyncSession, business_id: uuid.UUID, extra: dict | None) -> str | None:
    """The first person on this number's list who has a phone - where an urgent alert SMS goes."""
    profile = read(extra)
    members = None
    for level in profile["pickup"]["levels"]:
        for entry in level["people"]:
            if entry.get("phone"):
                return str(entry["phone"])
            if entry.get("user_id"):
                if members is None:
                    members = await _members(db, business_id)
                found = members.get(str(entry["user_id"]))
                if found:
                    return found[0]
    return None

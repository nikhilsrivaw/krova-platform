"""
What the AI may do for a customer by itself - on a call, on WhatsApp, on Instagram - and the rules every action follows.

The owner picks, per action and per channel, what the AI is allowed to do (Settings -> "What the AI can do"). Kept
in Business.settings["ai_actions"]; nothing set means the defaults below - what the AI could already do before this
existed (book, join a queue, cancel a booking in a chat) stays on, plus moving and cancelling, which every one of them
is now asked to confirm first. Taking or cancelling an order is off until the owner turns it on: it is money.

The rules, enforced here in code, not left to the prompt:
  - only the customer's own booking, token or order, re-read from the database at the moment of acting;
  - a change (cancel, move, order) is never done on the first ask. The AI proposes it, KROVA itself asks a fixed
    confirmation ("Confirm: cancel your booking on Sat 1 Nov, 5:00 PM?") and keeps the exact action with it; only a
    clear yes as the very next message carries it out - the saved action, not whatever the model says next;
  - a booking can only be cancelled or moved up to the owner's cut-off ("not within 4 hours of it"); later than that
    a person handles it;
  - an order is priced from the Catalogue page, never from the conversation;
  - the team hears about every change it made (push "ai_action"), and the record says the AI did it and on which
    channel.

Website chat is left out on purpose: a visitor's number there is taken on trust, so anyone could type someone else's
number and change their booking.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.utils.logging import get_logger

logger = get_logger(__name__)

CHANNELS = ("voice", "whatsapp", "instagram")
# key -> (what the owner reads, default channels)
ACTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "book": ("Book an appointment, meeting or viewing", CHANNELS),
    "cancel_booking": ("Cancel a booking", CHANNELS),
    "reschedule_booking": ("Move a booking to another time", CHANNELS),
    "book_token": ("Give a walk-in queue token", CHANNELS),
    "cancel_token": ("Cancel a queue token", CHANNELS),
    "take_order": ("Take an order from the Catalogue", ()),
    "cancel_order": ("Cancel an unpaid order", ()),
}
# Actions that change something the customer already has - asked to confirm first.
CHANGES = ("cancel_booking", "reschedule_booking", "cancel_token", "take_order", "cancel_order")
MAX_CUTOFF_HOURS = 168
# A confirmation stays open this long; after that a "yes" means nothing and the customer asks again.
PENDING_FOR = timedelta(minutes=30)
MAX_ORDER_LINES = 10


# ── settings ────────────────────────────────────────────────────────────────

def read(business) -> dict:
    raw = ((getattr(business, "settings", None) or {}).get("ai_actions") or {})
    chosen = raw.get("actions") if isinstance(raw.get("actions"), dict) else {}
    actions = {}
    for key, (_label, default) in ACTIONS.items():
        value = chosen.get(key)
        actions[key] = [c for c in CHANNELS if c in value] if isinstance(value, list) else list(default)
    cutoff = raw.get("cutoff_hours")
    return {
        "actions": actions,
        "cutoff_hours": cutoff if isinstance(cutoff, int) and 0 < cutoff <= MAX_CUTOFF_HOURS else None,
    }


def write(business, *, actions: dict[str, list[str]], cutoff_hours: int | None) -> dict:
    if cutoff_hours is not None and not 0 < cutoff_hours <= MAX_CUTOFF_HOURS:
        raise ValueError(f"The cut-off is 1 to {MAX_CUTOFF_HOURS} hours, or none.")
    clean = {key: [c for c in CHANNELS if c in (actions.get(key) or [])] for key in ACTIONS}
    settings = dict(business.settings or {})
    settings["ai_actions"] = {"actions": clean, "cutoff_hours": cutoff_hours}
    business.settings = settings
    return read(business)


def allowed(business, action: str, channel: str | None) -> bool:
    """Whether the AI may do this on this channel. Website chat keeps what it always had: booking and tokens only."""
    if channel == "web":
        return action in ("book", "book_token")
    if channel not in CHANNELS:
        return False
    return channel in read(business)["actions"].get(action, [])


def allowed_on(business, channel: str | None) -> list[str]:
    return [key for key in ACTIONS if allowed(business, key, channel)]


# ── what the customer said ──────────────────────────────────────────────────

_YES = {
    "haan", "han", "haa", "ha", "haanji", "hanji", "ji", "jee", "yes", "yeah", "yep", "yup", "ya", "ok", "okay", "okk",
    "okey", "k", "sure", "confirm", "confirmed", "done", "theek", "thik", "bilkul", "zaroor", "jarur", "chalega",
    "kardo", "kar", "karo", "karde", "kijiye", "please", "pls", "correct", "right", "proceed", "go",
    "हाँ", "हां", "जी", "ठीक", "करो", "करदो", "बिल्कुल", "ज़रूर", "जरूर",
}
_NO = {
    "nahi", "nahin", "nhi", "nai", "na", "no", "nope", "mat", "ruko", "ruk", "wait", "dont", "don't", "not",
    "नहीं", "नही", "मत", "रुको",
}


def _words(text: str | None) -> list[str]:
    """Words split on spaces and punctuation - not on regex word characters, which break Hindi at its vowel signs."""
    return [w for w in re.split(r"[\s.,!?;:()\"।|/-]+", (text or "").lower()) if w]


def affirmed(text: str | None) -> bool:
    """A clear yes - and no 'no' anywhere in it ("haan" yes, "nahi mat karo" no, "ruko" no)."""
    words = _words(text)
    if not words or len(words) > 12:
        return False
    if any(w in _NO for w in words):
        return False
    return any(w in _YES for w in words)


def declined(text: str | None) -> bool:
    words = _words(text)
    return bool(words) and any(w in _NO for w in words) and not any(w in ("haan", "yes", "हाँ", "हां") for w in words)


# ── what the AI proposed ────────────────────────────────────────────────────

@dataclass(slots=True)
class Proposal:
    """One change the model asked for. Fields as the model wrote them; nothing here is trusted yet."""
    kind: str  # one of CHANGES
    booking_at: str | None = None       # cancel / reschedule: the existing booking's time, as listed to the model
    new_at: str | None = None           # reschedule: the new time, from the availability list
    order_number: str | None = None     # cancel_order
    lines: list[tuple[str, int]] = field(default_factory=list)  # take_order: (product name, quantity)
    payment: str | None = None          # take_order: "link" | "cod"


@dataclass(slots=True)
class Plan:
    """A proposal checked against the database - ready to confirm, or the reason it cannot be done."""
    ok: bool
    kind: str
    data: dict[str, Any] = field(default_factory=dict)  # what to do, by id - saved with the confirmation
    summary: str = ""                                    # in words, for the confirmation and the team
    problem: str | None = None                           # for the team, when not ok
    say: str | None = None                               # for the customer, when not ok


def _when(business, at: datetime) -> str:
    tz = ZoneInfo(getattr(business, "timezone", None) or "Asia/Kolkata")
    return at.astimezone(tz).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _parse(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _rupees(paise: int) -> str:
    return f"₹{paise / 100:,.0f}"


def _too_late(business, starts_at: datetime, now: datetime) -> bool:
    hours = read(business)["cutoff_hours"]
    return hours is not None and starts_at - now < timedelta(hours=hours)


async def plan(db: AsyncSession, *, business, customer, proposal: Proposal, now: datetime | None = None) -> Plan:
    """Check a proposal against the customer's real records. Nothing is changed here."""
    now = now or datetime.now(timezone.utc)
    if proposal.kind in ("cancel_booking", "reschedule_booking"):
        return await _plan_booking(db, business=business, customer=customer, proposal=proposal, now=now)
    if proposal.kind == "cancel_token":
        return await _plan_token(db, business=business, customer=customer)
    if proposal.kind == "take_order":
        return await _plan_order(db, business=business, proposal=proposal)
    if proposal.kind == "cancel_order":
        return await _plan_cancel_order(db, business=business, customer=customer, proposal=proposal)
    return Plan(ok=False, kind=proposal.kind, problem="unknown action")


async def _plan_booking(db, *, business, customer, proposal: Proposal, now: datetime) -> Plan:
    from shared import verticals
    from shared.db.models import Appointment, AppointmentStatus, Doctor
    from shared.scheduling import availability

    kind = proposal.kind
    if not verticals.has_capability(business, "scheduling"):
        return Plan(ok=False, kind=kind, problem="this business has no bookings")
    at = _parse(proposal.booking_at)
    if at is None:
        return Plan(ok=False, kind=kind, problem=f"unreadable booking time {proposal.booking_at!r}")
    appointment = (await db.execute(select(Appointment).where(
        Appointment.business_id == business.id, Appointment.customer_id == customer.id, Appointment.starts_at == at,
        Appointment.status.in_([AppointmentStatus.confirmed, AppointmentStatus.awaiting_deposit,
                                AppointmentStatus.requested]),
    ))).scalars().first()
    if appointment is None:
        return Plan(ok=False, kind=kind, problem=f"no upcoming booking of theirs at {proposal.booking_at}",
                    say="I can't find an upcoming booking at that time - our team will check and get back to you.")
    starts = appointment.starts_at if appointment.starts_at.tzinfo else appointment.starts_at.replace(tzinfo=timezone.utc)
    if _too_late(business, starts, now):
        hours = read(business)["cutoff_hours"]
        return Plan(ok=False, kind=kind, problem=f"asked to change the {_when(business, starts)} booking inside the {hours}h cut-off",
                    say="It's too close to your booking for me to change it here - our team will call you about it.")
    data = {"appointment_id": str(appointment.id)}
    if kind == "cancel_booking":
        return Plan(ok=True, kind=kind, data=data, summary=f"cancel your booking on {_when(business, starts)}")
    new_at = _parse(proposal.new_at)
    doctor = await db.get(Doctor, appointment.doctor_id)
    if new_at is None or doctor is None:
        return Plan(ok=False, kind=kind, problem=f"no new time given to move the {_when(business, starts)} booking to")
    if new_at == starts:
        return Plan(ok=False, kind=kind, problem="the new time is the same as the old one")
    day = await availability.open_slots(db, business=business, doctor=doctor, on_date=new_at.astimezone(
        ZoneInfo(business.timezone)).date())
    if not any(s.starts_at == new_at for s in day):
        return Plan(ok=False, kind=kind, problem=f"{proposal.new_at} is not open for {doctor.name}",
                    say="That time isn't free - our team will help you pick another one.")
    data["new_at"] = new_at.isoformat()
    return Plan(ok=True, kind=kind, data=data,
                summary=f"move your booking from {_when(business, starts)} to {_when(business, new_at)}")


async def _plan_token(db, *, business, customer) -> Plan:
    from shared.db.models import QueueEntry, QueueStatus

    entry = (await db.execute(select(QueueEntry).where(
        QueueEntry.business_id == business.id, QueueEntry.customer_id == customer.id,
        QueueEntry.status == QueueStatus.waiting,
    ).order_by(QueueEntry.created_at.desc()).limit(1))).scalars().first()
    if entry is None:
        return Plan(ok=False, kind="cancel_token", problem="no waiting token of theirs",
                    say="I can't see a token waiting for you right now.")
    return Plan(ok=True, kind="cancel_token", data={"entry_id": str(entry.id)},
                summary=f"cancel your token number {entry.queue_number}")


async def _products(db, business_id: uuid.UUID) -> list[dict[str, Any]]:
    """Sellable lines, named the way the Catalogue page names them."""
    from shared.catalogue.chat_orders import line_name
    from shared.db.models import Product, ProductVariant

    rows = (await db.execute(
        select(ProductVariant, Product.title).join(Product, Product.id == ProductVariant.product_id)
        .where(ProductVariant.business_id == business_id).limit(2000)
    )).all()
    out = []
    for variant, title in rows:
        if not variant.price_paise or variant.available is False:
            continue
        out.append({"variant_id": variant.id, "name": line_name(title, variant.title), "title": title,
                    "price_paise": variant.price_paise})
    return out


def _match_product(products: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    wanted = " ".join((name or "").lower().split())
    exact = [p for p in products if " ".join(p["name"].lower().split()) == wanted]
    if len(exact) == 1:
        return exact[0]
    by_title = [p for p in products if " ".join(p["title"].lower().split()) == wanted]
    return by_title[0] if len(by_title) == 1 else None


async def _plan_order(db, *, business, proposal: Proposal) -> Plan:
    from shared import verticals
    from shared.catalogue import chat_orders
    from shared.payments import fee_list, links

    kind = "take_order"
    if not verticals.has_capability(business, "order_sync") or not fee_list.enabled(business):
        return Plan(ok=False, kind=kind, problem="orders are not switched on for this business")
    if not proposal.lines or len(proposal.lines) > MAX_ORDER_LINES:
        return Plan(ok=False, kind=kind, problem="no products named for the order")
    products = await _products(db, business.id)
    lines: list[tuple[uuid.UUID, int]] = []
    for name, quantity in proposal.lines:
        match = _match_product(products, name)
        if match is None:
            return Plan(ok=False, kind=kind, problem=f"{name!r} is not one product on the Catalogue page",
                        say="I couldn't match that to one product on our list - our team will help you order.")
        lines.append((match["variant_id"], max(1, min(chat_orders.MAX_QUANTITY, int(quantity or 1)))))
    try:
        items = await chat_orders.build_items(db, business.id, lines)
    except chat_orders.OrderError as exc:
        return Plan(ok=False, kind=kind, problem=str(exc), say="I couldn't set that order up - our team will help you.")
    payment = proposal.payment if proposal.payment in ("link", "cod") else None
    if payment is None:
        payment = "link" if await links.connection_for(db, business.id) is not None else "cod"
    if payment == "link" and await links.connection_for(db, business.id) is None:
        payment = "cod"
    total = chat_orders.total(items)
    how = "pay by link" if payment == "link" else "cash on delivery"
    return Plan(ok=True, kind=kind, data={"lines": [[str(v), q] for v, q in lines], "payment": payment},
                summary=f"place an order for {chat_orders.label(items)} - {_rupees(total)}, {how}")


async def _plan_cancel_order(db, *, business, customer, proposal: Proposal) -> Plan:
    from shared.catalogue import chat_orders
    from shared.db.models import Order, OrderStatus

    kind = "cancel_order"
    query = select(Order).where(Order.business_id == business.id, Order.customer_id == customer.id)
    number = (proposal.order_number or "").strip().lstrip("#")
    if number:
        query = query.where(Order.order_number.ilike(number) | Order.order_number.ilike(f"#{number}"))
    else:
        query = query.where(Order.status == OrderStatus.pending)
    orders = (await db.execute(query.order_by(Order.placed_at.desc().nullslast()).limit(2))).scalars().all()
    if not orders or (not number and len(orders) > 1):
        return Plan(ok=False, kind=kind, problem=f"could not tell which order to cancel ({number or 'no number'})",
                    say="Which order should I cancel? Please tell me the order number.")
    order = orders[0]
    if order.status != OrderStatus.pending or order.source_platform not in chat_orders.LINKABLE_SOURCES:
        return Plan(ok=False, kind=kind, problem=f"order {order.order_number} is {getattr(order.status, 'value', order.status)} - needs a person",
                    say="That order is already paid or on its way, so our team will handle cancelling it.")
    return Plan(ok=True, kind=kind, data={"order_id": str(order.id)},
                summary=f"cancel order {order.order_number} ({chat_orders.label(order.items)})")


# ── confirming ──────────────────────────────────────────────────────────────

def confirmation(plan_: Plan, language: str | None) -> str:
    """The fixed question KROVA asks before any change. The same words every time, so the next 'yes' is about this."""
    if language == "hindi":
        return f"पक्का कर लें: {plan_.summary} - कर दूँ? (हाँ / नहीं)"
    if language == "hinglish":
        return f"Confirm kar lein: {plan_.summary} - kar doon? (haan / nahi)"
    return f"Just to confirm: {plan_.summary}? (yes / no)"


def pending(plan_: Plan, now: datetime | None = None) -> dict:
    """What is saved with the confirmation - by id, so the 'yes' changes exactly what was asked about."""
    return {"kind": plan_.kind, "data": plan_.data, "summary": plan_.summary,
            "asked_at": (now or datetime.now(timezone.utc)).isoformat()}


def still_open(saved: dict | None, now: datetime | None = None) -> bool:
    if not saved or saved.get("kind") not in CHANGES:
        return False
    asked = _parse(saved.get("asked_at"))
    return asked is not None and (now or datetime.now(timezone.utc)) - asked <= PENDING_FOR


# ── doing it ────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class Done:
    ok: bool
    say: str
    problem: str | None = None
    link: str | None = None


_CHANNEL_WORDS = {"voice": "a call", "whatsapp": "WhatsApp", "instagram": "Instagram"}


async def carry_out(db: AsyncSession, *, business, customer, saved: dict, channel: str,
                    now: datetime | None = None) -> Done:
    """Do the confirmed change, re-checking everything first - minutes passed since the question."""
    now = now or datetime.now(timezone.utc)
    kind, data = saved.get("kind"), saved.get("data") or {}
    if not allowed(business, kind, channel):
        return Done(ok=False, say="I can't do that here - our team will help you.", problem=f"{kind} is switched off")
    via = _CHANNEL_WORDS.get(channel, channel)
    try:
        if kind in ("cancel_booking", "reschedule_booking"):
            done = await _do_booking(db, business=business, customer=customer, kind=kind, data=data, now=now, via=via)
        elif kind == "cancel_token":
            done = await _do_token(db, business=business, customer=customer, data=data)
        elif kind == "take_order":
            done = await _do_order(db, business=business, customer=customer, data=data, via=via)
        elif kind == "cancel_order":
            done = await _do_cancel_order(db, business=business, customer=customer, data=data)
        else:
            done = Done(ok=False, say="Our team will help you with that.", problem="unknown action")
    except Exception:
        logger.exception("ai action %s failed business=%s customer=%s", kind, business.id, customer.id)
        done = Done(ok=False, say="Something went wrong on my side - our team will help you with that.",
                    problem=f"{kind} failed with an error")
    if done.ok:
        await _tell_team(db, business=business, customer=customer, text=f"{saved.get('summary') or kind} - done on {via}")
    return done


async def _do_booking(db, *, business, customer, kind: str, data: dict, now: datetime, via: str) -> Done:
    from shared.db.models import Appointment, AppointmentStatus, Doctor
    from shared.scheduling import availability, booking
    from shared.scheduling.booking import SlotUnavailable

    appointment = await db.get(Appointment, uuid.UUID(data["appointment_id"]))
    live = (AppointmentStatus.confirmed, AppointmentStatus.awaiting_deposit, AppointmentStatus.requested)
    if appointment is None or appointment.customer_id != customer.id or appointment.status not in live:
        return Done(ok=False, say="That booking has already changed - our team will check with you.",
                    problem="the booking changed before the customer said yes")
    starts = appointment.starts_at if appointment.starts_at.tzinfo else appointment.starts_at.replace(tzinfo=timezone.utc)
    if _too_late(business, starts, now):
        return Done(ok=False, say="It's now too close to your booking to change it here - our team will call you.",
                    problem="the cut-off passed before the customer said yes")
    if kind == "cancel_booking":
        await booking.cancel(db, appointment=appointment, reason=f"cancelled by the customer on {via} (KROVA AI)")
        return Done(ok=True, say=f"Done - your booking on {_when(business, starts)} is cancelled.")
    new_at = _parse(data.get("new_at"))
    doctor = await db.get(Doctor, appointment.doctor_id)
    day = await availability.open_slots(db, business=business, doctor=doctor,
                                        on_date=new_at.astimezone(ZoneInfo(business.timezone)).date())
    slot = next((s for s in day if s.starts_at == new_at), None)
    if slot is None:
        return Done(ok=False, say="That time was just taken - our team will help you pick another one.",
                    problem="the new time was taken before the customer said yes")
    try:
        await booking.reschedule(db, appointment=appointment, new_slot=slot)
    except SlotUnavailable:
        return Done(ok=False, say="That time was just taken - our team will help you pick another one.",
                    problem="the new time was taken while moving the booking")
    return Done(ok=True, say=f"Done - your booking is now on {_when(business, new_at)}.")


async def _do_token(db, *, business, customer, data: dict) -> Done:
    from shared.db.models import QueueEntry, QueueStatus
    from shared.integrations import google_calendar

    entry = await db.get(QueueEntry, uuid.UUID(data["entry_id"]))
    if entry is None or entry.customer_id != customer.id or entry.status != QueueStatus.waiting:
        return Done(ok=False, say="That token has already changed.", problem="the token changed before the customer said yes")
    entry.status = QueueStatus.cancelled
    entry.completed_at = datetime.now(timezone.utc)
    await db.flush()
    if entry.google_calendar_event_id:
        try:
            await google_calendar.sync_queue_entry(db, business=business, entry=entry, action="cancel")
        except Exception:
            logger.exception("calendar cancel-sync failed for queue entry=%s", entry.id)
    return Done(ok=True, say=f"Done - token number {entry.queue_number} is cancelled.")


async def _do_order(db, *, business, customer, data: dict, via: str) -> Done:
    from shared.catalogue import chat_orders
    from shared.payments import links

    lines = [(uuid.UUID(v), int(q)) for v, q in data.get("lines") or []]
    try:
        items = await chat_orders.build_items(db, business.id, lines)
    except chat_orders.OrderError as exc:
        return Done(ok=False, say="I couldn't place that order - our team will help you.", problem=str(exc))
    cod = data.get("payment") != "link"
    order = await chat_orders.create(db, business=business, customer=customer, items=items, cod=cod,
                                     note=f"Taken by KROVA AI on {via}", user_id=None)
    raw = dict(order.raw_payload or {})
    raw["taken_by_ai_on"] = via
    order.raw_payload = raw
    total = _rupees(order.total_paise or 0)
    if cod:
        await chat_orders.send_summary(db, business=business, order=order, user_id=None)
        return Done(ok=True, say=f"Done - order {order.order_number} is placed: {total}, cash on delivery.")
    try:
        link, sent, _reason = await chat_orders.send_link(db, business=business, order=order, user_id=None)
    except links.LinkError as exc:
        return Done(ok=True, say=f"Your order {order.order_number} is placed ({total}). Our team will send you the payment link.",
                    problem=f"order placed but no payment link: {exc}")
    url = getattr(link, "short_url", None) if link is not None else None
    if sent:
        return Done(ok=True, say=f"Done - order {order.order_number} is placed ({total}). I've sent the payment link on WhatsApp.", link=url)
    if url:
        return Done(ok=True, say=f"Done - order {order.order_number} is placed ({total}). Pay here: {url}", link=url)
    return Done(ok=True, say=f"Your order {order.order_number} is placed ({total}). Our team will send you the payment link.",
                problem="order placed but the payment link was not sent")


async def _do_cancel_order(db, *, business, customer, data: dict) -> Done:
    from shared.care import post_call_actions
    from shared.catalogue import chat_orders
    from shared.db.models import Commitment, CommitmentStatus, Order, OrderStatus

    order = await db.get(Order, uuid.UUID(data["order_id"]))
    if order is None or order.customer_id != customer.id or order.status != OrderStatus.pending:
        return Done(ok=False, say="That order has already changed - our team will check it.",
                    problem="the order changed before the customer said yes")
    order.status = OrderStatus.cancelled
    # its open payment link stops counting as money owed
    rows = (await db.execute(select(Commitment).where(
        Commitment.business_id == business.id, Commitment.source_system == chat_orders.ORDER_PAYMENT,
        Commitment.external_ref == str(order.id), Commitment.status == CommitmentStatus.open,
    ))).scalars().all()
    for commitment in rows:
        commitment.status = CommitmentStatus.cancelled
    await db.flush()
    await post_call_actions.fire_safely(
        db, business_id=business.id, trigger_type="order.status_changed", customer_id=customer.id,
        context={"status": "cancelled", "previous_status": "pending", "order_number": order.order_number,
                 "total_paise": order.total_paise, "is_cod": order.is_cod},
    )
    return Done(ok=True, say=f"Done - order {order.order_number} is cancelled.")


async def _tell_team(db, *, business, customer, text: str) -> None:
    """The person who has the chat hears about it - or the owner and admins when nobody does."""
    try:
        from shared.db.models import BusinessMember, BusinessRole
        from shared.integrations import web_push

        people = [customer.assigned_to_user_id] if getattr(customer, "assigned_to_user_id", None) else list(
            (await db.execute(select(BusinessMember.user_id).where(
                BusinessMember.business_id == business.id,
                BusinessMember.role.in_((BusinessRole.owner, BusinessRole.admin)),
            ))).scalars().all())
        await web_push.send_to_users(
            db, business_id=business.id, user_ids=people, kind="ai_action",
            payload={"title": f"AI: {customer.display_name or 'a customer'}", "body": text[:140],
                     "url": f"/app/inbox/{customer.id}"},
        )
    except Exception:
        logger.exception("ai action push failed business=%s", business.id)


# ── what the customer is talking about now ──────────────────────────────────
# What they have on file (bookings, orders, dues, dates) is only shown to the AI once they bring the subject up in
# this conversation - so it answers what they ask now instead of steering every call back to last week's booking.

TOPIC_WORDS: dict[str, set[str]] = {
    "booking": {
        "booking", "book", "booked", "appointment", "appt", "slot", "meeting", "meet", "visit", "viewing", "consult",
        "consultation", "checkup", "reschedule", "postpone", "prepone", "cancel", "timing", "time", "date", "kal",
        "parso", "aaj", "tomorrow", "today", "kab", "milna", "mulakat", "mulaqat", "schedule", "session", "class",
        "बुकिंग", "अपॉइंटमेंट", "समय", "कल", "मिलना",
    },
    "order": {
        "order", "orders", "delivery", "deliver", "delivered", "parcel", "courier", "shipped", "shipping", "tracking",
        "dispatch", "cod", "product", "item", "return", "refund", "buy", "kharid", "kharidna", "mangwa", "mangwana",
        "saman", "samaan", "maal", "pahunch", "aaya", "aayega", "cancel", "ऑर्डर", "डिलीवरी", "सामान",
    },
    "money": {
        "payment", "pay", "paid", "fees", "fee", "due", "dues", "baaki", "baki", "bakaya", "balance", "amount", "kitna",
        "kitne", "paisa", "paise", "rupees", "rs", "invoice", "bill", "receipt", "upi", "link", "emi", "owe", "pending",
        "outstanding", "udhar", "udhaar", "hisab", "hisaab", "refund", "भुगतान", "फीस", "बाकी", "पैसे",
    },
    "token": {"token", "queue", "line", "number", "turn", "baari", "bari", "wait", "waiting", "टोकन", "नंबर"},
    "case": {"case", "claim", "insurance", "status", "hearing", "file", "matter", "property", "flat", "plot", "site"},
    "dates": {"renewal", "renew", "expiry", "expire", "expired", "membership", "plan", "validity", "khatam", "date",
              "subscription"},
}


def topics(texts: list[str]) -> set[str]:
    """Which of TOPIC_WORDS the customer has brought up in these messages."""
    words = set(_words(" ".join(texts)))
    return {name for name, vocab in TOPIC_WORDS.items() if words & vocab}

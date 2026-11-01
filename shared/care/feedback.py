"""
Feedback: asking customers how it went - after a visit, a walk-in token, a delivered order or a resolved chat - and
keeping what they say.

The business chooses on the Feedback page: whether it is on, after what, how long after, and how - WhatsApp (an
approved template with three buttons: Great / Okay / Not good), a short survey call, or WhatsApp first and a call if
they have not answered in a few hours. One customer is asked at most once every `cooldown_days`.

WhatsApp: the buttons carry "fb:<request id>:<5|3|1>" (notify._send's button payloads), so a tap is matched to exactly
the right request. After "Okay" or "Not good" they are asked what went wrong and their next message is the comment.
Call: a survey CallScript run through the business's automatic "Feedback calls" campaign; the call's answers
(call_script_extract, relay.py) come back through record_call().

Afterwards: a rating of 1 or 2 goes to Talk to team ("unhappy customer") so whoever is free calls them; 4 or 5 is sent
the Google review link when the business set one and its type allows asking for reviews. The template is filed as
UTILITY because it names the specific visit or order - Meta's rule for feedback (a generic survey is MARKETING).
"""

import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Business, Customer, FeedbackRequest
from shared.utils.logging import get_logger

logger = get_logger(__name__)

TEMPLATE = "feedback_request"
TRIGGERS = {
    "visit": "After a booking or appointment",
    "token": "After a walk-in token is done",
    "order": "After an order is delivered",
    "chat": "After a chat is marked resolved",
}
CHANNELS = ("whatsapp", "voice", "both")
BUTTONS = ((5, "Great"), (3, "Okay"), (1, "Not good"))
DEFAULTS = {
    "enabled": False, "triggers": ["visit", "token", "order"], "delay_minutes": 120, "channel": "whatsapp",
    "call_after_hours": 24, "cooldown_days": 7, "ask_review": True,
}
# Older than this, a visit or order is not worth asking about any more.
LOOKBACK = timedelta(days=3)
# A tap on an old message still counts; typing a comment must come soon after.
COMMENT_FOR = timedelta(hours=24)
CALL_QUESTIONS = [
    "On a scale of 1 to 5, how would you rate your recent experience with us?",
    "Is there anything we could do better?",
]
SCRIPT_NAME = "Feedback (automatic)"


# ── settings ────────────────────────────────────────────────────────────────

def read(business) -> dict:
    raw = ((getattr(business, "settings", None) or {}).get("feedback") or {})

    def num(key, low, high):
        value = raw.get(key)
        return value if isinstance(value, int) and low <= value <= high else DEFAULTS[key]

    triggers = raw.get("triggers")
    return {
        "enabled": bool(raw.get("enabled", DEFAULTS["enabled"])),
        "triggers": [t for t in TRIGGERS if t in triggers] if isinstance(triggers, list) else list(DEFAULTS["triggers"]),
        "delay_minutes": num("delay_minutes", 0, 7 * 24 * 60),
        "channel": raw.get("channel") if raw.get("channel") in CHANNELS else DEFAULTS["channel"],
        "call_after_hours": num("call_after_hours", 1, 72),
        "cooldown_days": num("cooldown_days", 0, 90),
        "ask_review": bool(raw.get("ask_review", DEFAULTS["ask_review"])),
    }


def write(business, values: dict) -> dict:
    merged = {**read(business), **{k: v for k, v in values.items() if k in DEFAULTS}}
    settings = dict(business.settings or {})
    settings["feedback"] = merged
    business.settings = settings
    return read(business)


def review_url(business) -> str | None:
    """The Google review link - only when set and the business type allows asking for reviews."""
    from shared.scheduling.recall import type_allows_reviews

    url = (business.settings or {}).get("google_review_url")
    return url if url and type_allows_reviews(business) else None


# ── finding who to ask ──────────────────────────────────────────────────────

def _day(business, at: datetime) -> str:
    tz = ZoneInfo(getattr(business, "timezone", None) or "Asia/Kolkata")
    return at.astimezone(tz).strftime("%d %b").lstrip("0")


async def _candidates(db: AsyncSession, business: Business, trigger: str, now: datetime, delay: timedelta) -> list[tuple]:
    """(customer_id, ref, about, happened_at) for things that finished between LOOKBACK and `delay` ago."""
    from shared.db.models import Appointment, AppointmentStatus, Doctor, Order, OrderStatus, QueueEntry, QueueStatus

    newest, oldest = now - delay, now - LOOKBACK
    if trigger == "visit":
        rows = (await db.execute(
            select(Appointment.customer_id, Appointment.id, Appointment.starts_at, Doctor.name)
            .outerjoin(Doctor, Doctor.id == Appointment.doctor_id)
            .where(Appointment.business_id == business.id,
                   Appointment.status.in_((AppointmentStatus.confirmed, AppointmentStatus.visited)),
                   Appointment.starts_at <= newest, Appointment.starts_at >= oldest)
        )).all()
        return [(c, f"appt:{i}", f"visit on {_day(business, at)}" + (f" with {d}" if d else ""), at) for c, i, at, d in rows]
    if trigger == "token":
        rows = (await db.execute(
            select(QueueEntry.customer_id, QueueEntry.id, QueueEntry.completed_at)
            .where(QueueEntry.business_id == business.id, QueueEntry.status == QueueStatus.done,
                   QueueEntry.customer_id.is_not(None), QueueEntry.completed_at <= newest,
                   QueueEntry.completed_at >= oldest)
        )).all()
        return [(c, f"token:{i}", f"visit on {_day(business, at)}", at) for c, i, at in rows]
    if trigger == "order":
        rows = (await db.execute(
            select(Order.customer_id, Order.id, Order.order_number, Order.updated_at)
            .where(Order.business_id == business.id, Order.status == OrderStatus.delivered,
                   Order.customer_id.is_not(None), Order.updated_at <= newest, Order.updated_at >= oldest)
        )).all()
        return [(c, f"order:{i}", f"order {n}" if n else "recent order", at) for c, i, n, at in rows]
    if trigger == "chat":
        rows = (await db.execute(
            select(Customer.id, Customer.resolved_at)
            .where(Customer.business_id == business.id, Customer.resolved_at.is_not(None),
                   Customer.resolved_at <= newest, Customer.resolved_at >= oldest)
        )).all()
        return [(c, f"chat:{c}:{at:%Y%m%d}", f"chat with us on {_day(business, at)}", at) for c, at in rows]
    return []


async def _recently_asked(db: AsyncSession, customer_id: uuid.UUID, since: datetime) -> bool:
    row = await db.execute(select(FeedbackRequest.id).where(
        FeedbackRequest.customer_id == customer_id, FeedbackRequest.created_at >= since).limit(1))
    return row.first() is not None


async def plan_requests(db: AsyncSession, business: Business, now: datetime | None = None) -> int:
    """File a request for everything that finished and was not asked about yet. Returns how many."""
    now = now or datetime.now(timezone.utc)
    s = read(business)
    if not s["enabled"]:
        return 0
    delay = timedelta(minutes=s["delay_minutes"])
    made = 0
    for trigger in s["triggers"]:
        for customer_id, ref, about, _at in await _candidates(db, business, trigger, now, delay):
            customer = await db.get(Customer, customer_id)
            if customer is None or customer.is_private:
                continue
            if s["cooldown_days"] and await _recently_asked(db, customer_id, now - timedelta(days=s["cooldown_days"])):
                continue
            try:
                async with db.begin_nested():
                    db.add(FeedbackRequest(
                        business_id=business.id, customer_id=customer_id, trigger=trigger, ref=ref[:64],
                        about=about[:200], channel=s["channel"], status="due", send_at=now,
                    ))
                    await db.flush()
                made += 1
            except IntegrityError:
                pass  # asked about this one already
    return made


# ── asking ──────────────────────────────────────────────────────────────────

async def _send_whatsapp(db: AsyncSession, business: Business, request: FeedbackRequest) -> bool:
    from shared.scheduling import notify

    customer = await db.get(Customer, request.customer_id)
    if customer is None:
        return False
    return await notify.send_feedback_request(
        db, business=business, customer=customer, about=request.about, request_id=request.id,
        ratings=tuple(rating for rating, _label in BUTTONS),
    )


async def _feedback_campaign(db: AsyncSession, business: Business):
    """The business's automatic survey script and campaign for feedback calls - made the first time they are needed."""
    from shared.db.models import Audience, CallCampaign, CallCampaignStatus, CallScript

    script = (await db.execute(select(CallScript).where(
        CallScript.business_id == business.id, CallScript.name == SCRIPT_NAME))).scalars().first()
    if script is None:
        script = CallScript(business_id=business.id, name=SCRIPT_NAME, purpose="survey", questions=list(CALL_QUESTIONS))
        db.add(script)
        await db.flush()
    campaign = (await db.execute(select(CallCampaign).where(
        CallCampaign.business_id == business.id, CallCampaign.call_script_id == script.id))).scalars().first()
    if campaign is None:
        campaign = CallCampaign(
            # An empty typed-number audience: sending this campaign by hand from the Voice page reaches nobody -
            # its people are only ever added one at a time, by _start_call.
            business_id=business.id, name="Feedback calls (automatic)", audience=Audience.numbers,
            audience_params={"numbers": []}, objective="Ask how their recent visit or order went", purpose="service",
            status=CallCampaignStatus.sending, call_script_id=script.id, extra={"automatic": "feedback"},
        )
        db.add(campaign)
        await db.flush()
    return script, campaign


async def _start_call(db: AsyncSession, business: Business, request: FeedbackRequest) -> bool:
    from shared.billing.guards import voice_calls_blocked
    from shared.db import queue
    from shared.db.models import CallCampaignRecipient, CallCampaignRecipientStatus

    if await voice_calls_blocked(db, business.id):
        request.note = "Calls are paused (wallet or plan)"
        return False
    _script, campaign = await _feedback_campaign(db, business)
    recipient = CallCampaignRecipient(call_campaign_id=campaign.id, customer_id=request.customer_id,
                                      status=CallCampaignRecipientStatus.pending, created_at=datetime.now(timezone.utc))
    db.add(recipient)
    await db.flush()
    await queue.enqueue("call_campaign_dial", {"recipient_id": str(recipient.id)}, db)
    return True


async def send_due(db: AsyncSession, now: datetime | None = None) -> int:
    """Send what is due: WhatsApp, a call, or the follow-up call for "both" when WhatsApp got no answer."""
    now = now or datetime.now(timezone.utc)
    rows = (await db.execute(select(FeedbackRequest).where(
        (FeedbackRequest.status == "due") & (FeedbackRequest.send_at <= now)
        | (FeedbackRequest.status == "sent") & FeedbackRequest.call_at.is_not(None) & (FeedbackRequest.call_at <= now)
    ).limit(200))).scalars().all()
    done = 0
    for request in rows:
        business = await db.get(Business, request.business_id)
        if business is None or not read(business)["enabled"]:
            request.status, request.note = "skipped", "Feedback is switched off"
            continue
        try:
            async with db.begin_nested():
                if request.status == "sent":  # "both": no answer on WhatsApp - call now
                    request.call_at = None
                    if await _start_call(db, business, request):
                        request.status = "calling"
                    else:
                        request.status = "no_reply"
                elif request.channel == "voice":
                    request.status = "calling" if await _start_call(db, business, request) else "skipped"
                else:
                    sent = await _send_whatsapp(db, business, request)
                    if sent:
                        request.status, request.sent_at = "sent", now
                        if request.channel == "both":
                            request.call_at = now + timedelta(hours=read(business)["call_after_hours"])
                    elif request.channel == "both" and await _start_call(db, business, request):
                        request.status = "calling"
                    else:
                        request.status = "skipped"
                        request.note = request.note or "The feedback template is not approved yet, or WhatsApp is not connected"
                if request.status == "calling":
                    request.sent_at = request.sent_at or now
            done += 1
        except Exception:
            logger.exception("feedback send failed request=%s", request.id)
    await db.flush()
    return done


async def sweep(db: AsyncSession) -> int:
    """Every few minutes: file new requests for businesses that switched feedback on, then send what is due."""
    businesses = (await db.execute(select(Business).where(Business.is_active == True))).scalars().all()  # noqa: E712
    for business in businesses:
        if read(business)["enabled"]:
            try:
                async with db.begin_nested():
                    await plan_requests(db, business)
            except Exception:
                logger.exception("feedback planning failed business=%s", business.id)
    return await send_due(db)


# ── answers ─────────────────────────────────────────────────────────────────

_RATING_WORDS = {
    5: ("excellent", "great", "very good", "bahut achha", "bahut accha", "best", "awesome", "perfect", "badhiya", "zabardast"),
    4: ("good", "achha", "accha", "acha", "nice", "theek thaak se zyada"),
    3: ("okay", "ok", "average", "theek", "thik", "thik thak", "theek thaak", "chalega"),
    2: ("not great", "not good", "kuch khaas nahi", "kharab sa"),
    1: ("bad", "very bad", "kharab", "bekar", "bakwas", "worst", "terrible", "ganda"),
}


def rating_from(text: str | None) -> int | None:
    """A 1-5 rating from what someone said ("4", "char", "bahut achha"). None when it does not say."""
    words = (text or "").lower()
    digits = re.findall(r"\b([1-5])\b", words)
    if digits:
        return int(digits[0])
    spoken = {"ek": 1, "one": 1, "do": 2, "two": 2, "teen": 3, "three": 3, "char": 4, "chaar": 4, "four": 4,
              "paanch": 5, "panch": 5, "five": 5}
    for word, value in spoken.items():
        if re.search(rf"\b{word}\b", words):
            return value
    for value in (2, 1, 5, 3, 4):  # the more specific phrases first ("not good" before "good")
        if any(re.search(rf"\b{re.escape(p)}\b", words) for p in _RATING_WORDS[value]):
            return value
    return None


async def _after_rating(db: AsyncSession, business: Business, request: FeedbackRequest, customer=None) -> None:
    """An unhappy customer goes to Talk to team, so whoever is free calls them."""
    if request.rating is not None and request.rating <= 2:
        from shared.team import requests as team_requests

        customer = customer or await db.get(Customer, request.customer_id)
        if customer is not None:
            text = f"Unhappy - rated {request.rating}/5 for their {request.about}"
            if request.comment:
                text += f": {request.comment}"
            await team_requests.open_request(db, business_id=business.id, customer=customer,
                                             channel=request.answered_via if request.answered_via in ("voice", "whatsapp") else "whatsapp",
                                             what=text)


def _thanks(rating: int, link: str | None) -> str:
    if rating >= 4:
        return (f"Thank you! We're glad it went well. If you have a minute, a Google review helps us a lot: {link}"
                if link else "Thank you! We're glad it went well.")
    return "Sorry it wasn't better. What went wrong? Just type it here - it goes straight to our team."


async def handle_reply(db: AsyncSession, *, business: Business, customer: Customer, text: str | None,
                       media: dict | None, now: datetime | None = None) -> str | None:
    """
    A WhatsApp message that answers a feedback request: a button tap, or the comment after "Okay" / "Not good".
    Returns the reply to send, or None when the message is not about feedback (the AI answers it as usual).
    """
    now = now or datetime.now(timezone.utc)
    payload = (media or {}).get("payload") if (media or {}).get("kind") == "button_reply" else None
    if payload and str(payload).startswith("fb:"):
        try:
            _fb, request_id, value = str(payload).split(":")
            request = await db.get(FeedbackRequest, uuid.UUID(request_id))
            rating = int(value)
        except (ValueError, TypeError):
            return None
        if request is None or request.customer_id != customer.id or not 1 <= rating <= 5:
            return None
        if request.rating is not None:
            return "Thanks - we already have your feedback for this one."
        request.rating, request.answered_via, request.answered_at, request.status = rating, "whatsapp", now, "answered"
        request.call_at = None
        request.awaiting_comment = rating <= 3
        link = review_url(business) if rating >= 4 and read(business)["ask_review"] else None
        request.review_sent = bool(link)
        await _after_rating(db, business, request, customer)
        await db.flush()
        return _thanks(rating, link)

    waiting = (await db.execute(select(FeedbackRequest).where(
        FeedbackRequest.customer_id == customer.id, FeedbackRequest.awaiting_comment == True,  # noqa: E712
        FeedbackRequest.answered_at >= now - COMMENT_FOR,
    ).order_by(FeedbackRequest.answered_at.desc()).limit(1))).scalars().first()
    if waiting is None or not (text or "").strip():
        return None
    waiting.comment, waiting.awaiting_comment = text.strip()[:2000], False
    if waiting.rating is not None and waiting.rating <= 2:
        await _after_rating(db, business, waiting, customer)  # adds the comment to their Talk to team request
    await db.flush()
    return "Thank you for telling us - our team will look into it."


async def record_call(db: AsyncSession, *, script_id: uuid.UUID, customer_id: uuid.UUID | None, answers: dict) -> bool:
    """A feedback call's answers (relay.py, after the call). True when it was one."""
    from shared.db.models import CallScript

    script = await db.get(CallScript, script_id)
    if script is None or script.name != SCRIPT_NAME or customer_id is None:
        return False
    request = (await db.execute(select(FeedbackRequest).where(
        FeedbackRequest.customer_id == customer_id, FeedbackRequest.status == "calling",
    ).order_by(FeedbackRequest.sent_at.desc().nullslast()).limit(1))).scalars().first()
    if request is None:
        return False
    values = [str(v) for v in (answers or {}).values() if v]
    request.rating = rating_from(values[0]) if values else None
    request.comment = (values[1] if len(values) > 1 else None) or request.comment
    request.answered_via, request.answered_at = "voice", datetime.now(timezone.utc)
    request.status = "answered" if request.rating is not None or request.comment else "no_reply"
    business = await db.get(Business, request.business_id)
    if business is not None:
        await _after_rating(db, business, request)
    await db.flush()
    return True


# ── the page ────────────────────────────────────────────────────────────────

async def stats(db: AsyncSession, business_id: uuid.UUID, days: int = 30) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = and_(FeedbackRequest.business_id == business_id, FeedbackRequest.created_at >= since)
    asked = (await db.execute(select(func.count(FeedbackRequest.id)).where(
        base, FeedbackRequest.status.in_(("sent", "calling", "answered", "no_reply"))))).scalar() or 0
    rows = (await db.execute(select(FeedbackRequest.rating, func.count(FeedbackRequest.id)).where(
        base, FeedbackRequest.rating.is_not(None)).group_by(FeedbackRequest.rating))).all()
    spread = {r: int(n) for r, n in rows}
    answered = sum(spread.values())
    average = round(sum(r * n for r, n in spread.items()) / answered, 2) if answered else None
    return {
        "days": days, "asked": int(asked), "answered": answered, "average": average,
        "happy": sum(n for r, n in spread.items() if r >= 4), "unhappy": sum(n for r, n in spread.items() if r <= 2),
        "spread": {str(r): spread.get(r, 0) for r in range(1, 6)},
    }

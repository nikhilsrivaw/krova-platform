"""
Feedback: asking customers how it went, and keeping what they say.

What gets asked about:
  - things KROVA saw finish (the business ticks which): a visit or appointment, a walk-in token, a delivered order, a
    resolved chat - from the last LOOKBACK days, `delay_minutes` after they finished;
  - things that happened outside KROVA (a bus ticket sold on another app, a sale at the counter) that the business adds
    itself - an uploaded file (CSV or Excel), a pasted list, a form, or the API. Each row says who (name, phone), what
    ("Delhi to Jaipur trip") and when; it is asked about `delay_minutes` after that time.

How - rounds: the business lists up to MAX_ROUNDS attempts, each WhatsApp or a call, each a number of hours after the
one before ("WhatsApp right away; if no answer, call after 24 hours; if still nothing, call again after 48"). They stop
the moment the customer answers. Nobody is asked outside the business's own asking hours (9 AM to 8 PM by default),
and one customer at most once every `cooldown_days`.

WhatsApp: an approved template naming the visit or trip, with three buttons (Great / Okay / Not good) whose payloads
"fb:<request id>:<5|3|1>" match a tap to its request; after Okay or Not good they are asked what went wrong and their
next message is the comment. Filed UTILITY because it names the specific visit or order (Meta's rule; a generic
survey is MARKETING) - no offer may ever be added to it.
Call: a survey CallScript run through the business's automatic "Feedback calls" campaign; the call's answers
(call_script_extract, relay.py) come back through record_call().

Afterwards: a rating of 1 or 2 goes to Talk to team ("unhappy customer") so whoever is free calls them. 4 or 5 gets the
review link (the business's own link, or its Google review link) when its type may ask for reviews - straight away on
WhatsApp after a tap, or as the feedback_review_link template after a call.
"""

import csv
import hashlib
import re
import uuid
from datetime import datetime, time, timedelta, timezone
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
# Requests added by hand or by the API - always asked about, whatever is ticked above.
EXTERNAL = "external"
CHANNELS = ("whatsapp", "voice", "both")
ROUND_CHANNELS = ("whatsapp", "voice")
MAX_ROUNDS = 5
BUTTONS = ((5, "Great"), (3, "Okay"), (1, "Not good"))
DEFAULTS = {
    "enabled": False, "triggers": ["visit", "token", "order"], "delay_minutes": 120, "channel": "whatsapp",
    "call_after_hours": 24, "cooldown_days": 7, "ask_review": True, "rounds": None, "ask_from": 9, "ask_until": 20,
    "review_link": None,
}
# Older than this, a visit or order KROVA saw is not worth asking about; one added by hand may be a little older.
LOOKBACK = timedelta(days=3)
MANUAL_LOOKBACK = timedelta(days=30)
# After the last round: how long to wait for an answer before calling it "no reply".
LAST_WAIT = {"whatsapp": timedelta(hours=48), "voice": timedelta(hours=3)}
# A tap on an old message still counts; typing a comment must come soon after.
COMMENT_FOR = timedelta(hours=24)
CALL_QUESTIONS = [
    "On a scale of 1 to 5, how would you rate your recent experience with us?",
    "Is there anything we could do better?",
]
SCRIPT_NAME = "Feedback (automatic)"
MAX_ROWS = 2000
MAX_FILE_BYTES = 2 * 1024 * 1024


# ── settings ────────────────────────────────────────────────────────────────

def _rounds_from(raw: dict) -> list[dict]:
    """The rounds as saved, cleaned - or, for settings saved before rounds existed, made from the old channel choice."""
    rounds = raw.get("rounds")
    out: list[dict] = []
    if isinstance(rounds, list):
        for i, r in enumerate(rounds[:MAX_ROUNDS]):
            if not isinstance(r, dict) or r.get("channel") not in ROUND_CHANNELS:
                continue
            wait = r.get("wait_hours")
            wait = wait if isinstance(wait, int) and 1 <= wait <= 168 else 24
            out.append({"channel": r["channel"], "wait_hours": 0 if not out else wait})
    if out:
        return out
    channel = raw.get("channel") if raw.get("channel") in CHANNELS else "whatsapp"
    after = raw.get("call_after_hours") if isinstance(raw.get("call_after_hours"), int) else 24
    if channel == "both":
        return [{"channel": "whatsapp", "wait_hours": 0}, {"channel": "voice", "wait_hours": max(1, min(168, after))}]
    return [{"channel": channel, "wait_hours": 0}]


def _summary(rounds: list[dict]) -> str:
    kinds = {r["channel"] for r in rounds}
    return "both" if len(kinds) > 1 else rounds[0]["channel"]


def read(business) -> dict:
    raw = ((getattr(business, "settings", None) or {}).get("feedback") or {})

    def num(key, low, high):
        value = raw.get(key)
        return value if isinstance(value, int) and low <= value <= high else DEFAULTS[key]

    triggers = raw.get("triggers")
    rounds = _rounds_from(raw)
    ask_from, ask_until = num("ask_from", 0, 23), num("ask_until", 1, 24)
    if ask_until <= ask_from:
        ask_from, ask_until = DEFAULTS["ask_from"], DEFAULTS["ask_until"]
    link = raw.get("review_link")
    return {
        "enabled": bool(raw.get("enabled", DEFAULTS["enabled"])),
        "triggers": [t for t in TRIGGERS if t in triggers] if isinstance(triggers, list) else list(DEFAULTS["triggers"]),
        "delay_minutes": num("delay_minutes", 0, 7 * 24 * 60),
        "channel": _summary(rounds),
        "call_after_hours": num("call_after_hours", 1, 72),
        "cooldown_days": num("cooldown_days", 0, 90),
        "ask_review": bool(raw.get("ask_review", DEFAULTS["ask_review"])),
        "rounds": rounds,
        "ask_from": ask_from,
        "ask_until": ask_until,
        "review_link": link if isinstance(link, str) and link.startswith(("https://", "http://")) else None,
    }


def write(business, values: dict) -> dict:
    current = ((business.settings or {}).get("feedback") or {})
    merged = {**current, **{k: v for k, v in values.items() if k in DEFAULTS}}
    if "rounds" in values:
        merged["rounds"] = _rounds_from({"rounds": values.get("rounds")})
    if isinstance(merged.get("review_link"), str):
        merged["review_link"] = merged["review_link"].strip()[:500] or None
    settings = dict(business.settings or {})
    settings["feedback"] = merged
    business.settings = settings
    return read(business)


def review_url(business) -> str | None:
    """The business's own review link (any site), else its Google review link - when its type may ask for reviews."""
    from shared.scheduling.recall import type_allows_reviews

    url = read(business)["review_link"] or (business.settings or {}).get("google_review_url")
    return url if url and type_allows_reviews(business) else None


def _tz(business) -> ZoneInfo:
    return ZoneInfo(getattr(business, "timezone", None) or "Asia/Kolkata")


def asking_hours(business, now: datetime) -> bool:
    """Inside the business's own asking hours (its time zone)."""
    s = read(business)
    hour = now.astimezone(_tz(business)).hour
    return s["ask_from"] <= hour < s["ask_until"]


# ── finding who to ask ──────────────────────────────────────────────────────

def _day(business, at: datetime) -> str:
    return at.astimezone(_tz(business)).strftime("%d %b").lstrip("0")


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


PENDING = ("due", "sent", "calling")


async def _recently_asked(db: AsyncSession, customer_id: uuid.UUID, since: datetime) -> str | None:
    """
    Why this customer should not get another request now: "waiting" - one is already lined up or under way;
    "asked" - one actually went out since `since`. None otherwise. A request that never went out (skipped, or
    switched off before its turn) does not count - nobody asked them anything.
    """
    rows = (await db.execute(select(FeedbackRequest.status).where(
        FeedbackRequest.customer_id == customer_id,
        (FeedbackRequest.status.in_(PENDING)) | (FeedbackRequest.sent_at >= since),
    ).limit(5))).all()
    statuses = {r[0] for r in rows}
    if statuses & set(PENDING):
        return "waiting"
    return "asked" if statuses else None


# What people type when they mean the thing itself: "how was the product?" -> "product".
_QUESTION = re.compile(
    r"^(?:(?:how|hows|how's)\s+(?:was|is|did|were)\s+(?:you\s+(?:find|like)\s+)?|feedback\s+(?:on|for|about)\s+|"
    r"please\s+rate\s+)?(?:(?:the|your|my|our)\b)?\s*",
    re.IGNORECASE,
)
_TAIL = re.compile(r"\s*(?:kaisa|kaisi|kaise)\s*(?:tha|thi|the|laga|lagi|rha|rhi|raha|rahi)?\s*$|[\s?!.]+$",
                   re.IGNORECASE)


def clean_what(text: str) -> str:
    """The thing to ask about, from what was typed - without "how was the ..." or a question mark."""
    what = " ".join(str(text or "").split())
    for _ in range(2):
        what = _TAIL.sub("", _QUESTION.sub("", what, count=1)).strip()
    return what[:120]


async def plan_requests(db: AsyncSession, business: Business, now: datetime | None = None) -> int:
    """File a request for everything KROVA saw finish that was not asked about yet. Returns how many."""
    now = now or datetime.now(timezone.utc)
    s = read(business)
    if not s["enabled"]:
        return 0
    delay = timedelta(minutes=s["delay_minutes"])
    made = 0
    for trigger in s["triggers"]:
        for customer_id, ref, about, at in await _candidates(db, business, trigger, now, delay):
            customer = await db.get(Customer, customer_id)
            if customer is None or customer.is_private:
                continue
            if s["cooldown_days"] and await _recently_asked(db, customer_id, now - timedelta(days=s["cooldown_days"])):
                continue  # lined up already, or asked recently
            try:
                async with db.begin_nested():
                    db.add(FeedbackRequest(
                        business_id=business.id, customer_id=customer_id, trigger=trigger, ref=ref[:64],
                        about=about[:200], channel=s["channel"], status="due", send_at=now, next_at=now,
                        source="auto", happened_at=at,
                    ))
                    await db.flush()
                made += 1
            except IntegrityError:
                pass  # asked about this one already
    return made


# ── things that happened outside KROVA: a file, a pasted list, a form, the API ─

_COLUMNS = {
    "name": ("name", "customer", "customer name", "passenger", "passenger name", "full name", "naam", "client", "guest"),
    "phone": ("phone", "mobile", "mobile number", "phone number", "mobile no", "phone no", "contact", "contact number",
              "whatsapp", "whatsapp number", "number", "mob", "cell"),
    "about": ("about", "service", "trip", "route", "journey", "product", "order", "item", "visit", "what", "details",
              "description", "booking", "package", "course", "treatment"),
    "date": ("date", "travel date", "journey date", "visit date", "service date", "date of travel", "delivered on",
             "completed on", "when", "datetime", "date time", "booking date", "check out", "checkout"),
    "time": ("time", "departure time", "arrival time", "visit time", "slot"),
    "ref": ("ref", "reference", "booking id", "pnr", "ticket", "ticket no", "ticket number", "order id", "invoice",
            "invoice no", "id"),
}


def _header_key(header: str) -> str | None:
    text = " ".join(re.sub(r"[^a-z0-9 ]", " ", (header or "").lower()).split())
    for key, names in _COLUMNS.items():
        if text in names:
            return key
    for key, names in _COLUMNS.items():  # "Passenger Mobile", "Travel Date (dd/mm)"
        if any(name in text for name in names if len(name) > 3):
            return key
    return None


def _looks_like_phone(value: str) -> bool:
    return len(re.sub(r"\D", "", value or "")) >= 10


def _rows_from_table(headers: list[str], records: list[list]) -> list[dict]:
    """Map a table's columns to name / phone / about / date / time / ref. A table with no header row is read as
    name, phone, what, date - the order the page shows."""
    keys = [_header_key(h) for h in headers]
    if "phone" not in keys:
        if any(_looks_like_phone(str(h)) for h in headers):  # no header row - the first line is data
            records = [headers, *records]
            keys = ["name", "phone", "about", "date", "time", "ref"][: len(headers)]
        else:
            raise ValueError("No phone number column. Name one column Phone (or Mobile), or paste without a header "
                             "in this order: name, phone, what, date.")
    out = []
    for values in records:
        row: dict = {}
        for key, value in zip(keys, values):
            if key and key not in row and value not in (None, ""):
                row[key] = value
        if any(v not in (None, "") for v in values):
            out.append(row)
    return out


def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not read this file as text.")


def _split_text(text: str) -> list[list[str]]:
    """A pasted list or CSV text: tab (copied from Excel or Google Sheets), comma or semicolon - whichever it uses."""
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:5])
    delimiter = "\t" if "\t" in sample else max((",", ";", "|"), key=sample.count)
    return [[cell.strip() for cell in row] for row in csv.reader(lines, delimiter=delimiter)]


def parse_file(filename: str, content: bytes) -> list[dict]:
    """CSV, Excel (.xlsx) or a text list. Raises ValueError with a message for the owner."""
    name = (filename or "").lower()
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("The file is over 2 MB. Split it into smaller files.")
    if name.endswith(".xls"):
        raise ValueError("Old Excel files (.xls) can't be read. Open it and save it as .xlsx or CSV.")
    if name.endswith(".xlsx"):
        from shared.integrations.receivables import _read_xlsx

        headers, records = _read_xlsx(content)
        return _rows_from_table(headers, [[r.get(h) for h in headers] for r in records])
    table = _split_text(_decode(content))
    if not table:
        raise ValueError("The file is empty.")
    return _rows_from_table(table[0], table[1:])


def parse_text(text: str) -> list[dict]:
    table = _split_text(text or "")
    if not table:
        raise ValueError("Paste at least one line: name, phone, what, date.")
    return _rows_from_table(table[0], table[1:])


def _when(business, date_value, time_value=None) -> datetime | None:
    """A date (and time) as people write it - 02/11/2026, 2 Nov 2026, 2026-11-02 18:30, or an Excel date cell."""
    from dateutil import parser as dateparser

    tz = _tz(business)
    if isinstance(date_value, datetime):
        at = date_value
    elif hasattr(date_value, "year") and hasattr(date_value, "month"):
        at = datetime.combine(date_value, time(12, 0))
    else:
        text = str(date_value or "").strip()
        if not text:
            return None
        try:
            at = dateparser.parse(text, dayfirst=not re.match(r"^\d{4}-", text))
        except (ValueError, OverflowError):
            return None
    if time_value not in (None, ""):
        try:
            clock = time_value if isinstance(time_value, time) else dateparser.parse(str(time_value)).time()
            at = datetime.combine(at.date(), clock)
        except (ValueError, OverflowError):
            pass
    return at if at.tzinfo else at.replace(tzinfo=tz)


async def add_external(db: AsyncSession, business: Business, rows: list[dict], *, source: str,
                       now: datetime | None = None) -> dict:
    """
    Ask these customers about something that happened outside KROVA. Each row: name, phone, about (what), date (when
    it happened, optional - now if missing), time and ref (optional). Returns {added, skipped: [{row, reason}]}.
    """
    from shared.db.models import IdentityKind
    from shared.identity import resolver
    from shared.identity.normalise import InvalidIdentifier, normalise_phone

    now = now or datetime.now(timezone.utc)
    s = read(business)
    if len(rows) > MAX_ROWS:
        raise ValueError(f"Up to {MAX_ROWS} customers at a time.")
    delay = timedelta(minutes=s["delay_minutes"])
    added, skipped = 0, []
    for number, row in enumerate(rows, start=1):
        try:
            phone = normalise_phone(str(row.get("phone") or ""))
        except InvalidIdentifier:
            skipped.append({"row": number, "reason": f"Phone number not readable: {row.get('phone') or '(empty)'}"})
            continue
        happened = _when(business, row.get("date"), row.get("time")) if row.get("date") not in (None, "") else None
        if row.get("date") not in (None, "") and happened is None:
            skipped.append({"row": number, "reason": f"Date not readable: {row.get('date')}"})
            continue
        happened = happened or now
        if happened < now - MANUAL_LOOKBACK:
            skipped.append({"row": number, "reason": "Happened more than 30 days ago"})
            continue
        what = clean_what(row.get("about")) or "visit"
        # Meta treats feedback as a utility message only when it names the specific visit or trip - so it carries a date.
        about = what if re.search(r"\d", what) else f"{what} on {_day(business, happened)}"
        name = " ".join(str(row.get("name") or "").split())[:120] or None
        resolution = await resolver.resolve(business.id, IdentityKind.phone, phone, db, display_name=name)
        customer = resolution.customer
        if customer.is_private:
            skipped.append({"row": number, "reason": "This customer is marked private"})
            continue
        if s["cooldown_days"]:
            why = await _recently_asked(db, customer.id, now - timedelta(days=s["cooldown_days"]))
            if why == "waiting":
                skipped.append({"row": number, "reason": "Already on the list, waiting to be asked"})
                continue
            if why == "asked":
                skipped.append({"row": number, "reason": f"Asked in the last {s['cooldown_days']} days"})
                continue
        key = str(row.get("ref") or "").strip() or (
            f"{phone}:{happened:%Y%m%d%H%M}:{hashlib.sha1(what.lower().encode()).hexdigest()[:10]}")
        due = max(now, happened + delay)
        try:
            async with db.begin_nested():
                db.add(FeedbackRequest(
                    business_id=business.id, customer_id=customer.id, trigger=EXTERNAL, ref=f"ext:{key}"[:64],
                    about=about[:200], channel=s["channel"], status="due", send_at=due, next_at=due,
                    source=source, happened_at=happened,
                ))
                await db.flush()
            added += 1
        except IntegrityError:
            skipped.append({"row": number, "reason": "Already added"})
    logger.info("feedback list business=%s source=%s added=%s skipped=%s", business.id, source, added, len(skipped))
    return {"added": added, "skipped": skipped}


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


async def _next_round(db: AsyncSession, business: Business, request: FeedbackRequest, now: datetime) -> None:
    """Ask once more - the next round on the business's list - or, after the last, call it 'no reply'."""
    rounds = read(business)["rounds"]
    if request.round >= len(rounds):
        request.status, request.next_at = "no_reply", None
        return
    step = rounds[request.round]
    request.round += 1
    if step["channel"] == "voice":
        done = await _start_call(db, business, request)
        status = "calling"
    else:
        done = await _send_whatsapp(db, business, request)
        status = "sent"
        if not done:
            request.note = "The feedback template is not approved yet, or WhatsApp is not connected"
    if done:
        request.status = status
        request.sent_at = request.sent_at or now
    if request.round < len(rounds):
        # the next round after its gap - or straight away when this one could not go out
        request.next_at = now + timedelta(hours=rounds[request.round]["wait_hours"]) if done else now
    elif done:
        request.next_at = now + LAST_WAIT[step["channel"]]  # time to answer the last ask
    else:
        request.status = "no_reply" if request.sent_at else "skipped"
        request.next_at = None


async def send_due(db: AsyncSession, now: datetime | None = None) -> int:
    """Every round that is due: the first ask, an ask again after no answer, or giving up after the last."""
    now = now or datetime.now(timezone.utc)
    rows = (await db.execute(select(FeedbackRequest).where(
        FeedbackRequest.status.in_(("due", "sent", "calling")), FeedbackRequest.rating.is_(None),
        FeedbackRequest.next_at.is_not(None), FeedbackRequest.next_at <= now,
    ).order_by(FeedbackRequest.next_at).limit(200))).scalars().all()
    done = 0
    businesses: dict = {}
    for request in rows:
        business = businesses.get(request.business_id) or await db.get(Business, request.business_id)
        businesses[request.business_id] = business
        if business is None or not read(business)["enabled"]:
            continue  # waits until feedback is switched on again
        last_round_over = request.round >= len(read(business)["rounds"])
        if not last_round_over and not asking_hours(business, now):
            continue  # closing as "no reply" sends nothing, so it is not held to the asking hours
        try:
            async with db.begin_nested():
                await _next_round(db, business, request, now)
            done += 1
        except Exception:
            logger.exception("feedback round failed request=%s", request.id)
    await db.flush()
    return done


async def sweep(db: AsyncSession) -> int:
    """Every few minutes: file new requests for businesses that switched feedback on, then run the rounds due."""
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
        return (f"Thank you! We're glad it went well. If you have a minute, a review helps us a lot: {link}"
                if link else "Thank you! We're glad it went well.")
    return "Sorry it wasn't better. What went wrong? Just type it here - it goes straight to our team."


def _answered(request: FeedbackRequest, rating: int | None, via: str, now: datetime) -> None:
    request.rating, request.answered_via, request.answered_at = rating, via, now
    request.status, request.next_at = "answered", None


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
        _answered(request, rating, "whatsapp", now)
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
    rating = rating_from(values[0]) if values else None
    comment = (values[1] if len(values) > 1 else None) or request.comment
    if rating is None and not comment:
        return True  # picked up but did not answer - the next round (if any) still runs
    _answered(request, rating, "voice", datetime.now(timezone.utc))
    request.comment = comment
    business = await db.get(Business, request.business_id)
    if business is not None:
        await _after_rating(db, business, request)
        link = review_url(business) if (rating or 0) >= 4 and read(business)["ask_review"] else None
        customer = await db.get(Customer, customer_id) if link else None
        if link and customer is not None:
            from shared.scheduling import notify

            request.review_sent = await notify.send_feedback_review_link(
                db, business=business, customer=customer, about=request.about, review_url=link,
            )
    await db.flush()
    return True


# ── the page ────────────────────────────────────────────────────────────────

async def stats(db: AsyncSession, business_id: uuid.UUID, days: int = 30) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = and_(FeedbackRequest.business_id == business_id, FeedbackRequest.created_at >= since)
    asked = (await db.execute(select(func.count(FeedbackRequest.id)).where(
        base, FeedbackRequest.status.in_(("sent", "calling", "answered", "no_reply"))))).scalar() or 0
    waiting = (await db.execute(select(func.count(FeedbackRequest.id)).where(
        base, FeedbackRequest.status.in_(("due", "sent", "calling"))))).scalar() or 0
    rows = (await db.execute(select(FeedbackRequest.rating, func.count(FeedbackRequest.id)).where(
        base, FeedbackRequest.rating.is_not(None)).group_by(FeedbackRequest.rating))).all()
    spread = {r: int(n) for r, n in rows}
    answered = sum(spread.values())
    average = round(sum(r * n for r, n in spread.items()) / answered, 2) if answered else None
    return {
        "days": days, "asked": int(asked), "answered": answered, "average": average, "waiting": int(waiting),
        "happy": sum(n for r, n in spread.items() if r >= 4), "unhappy": sum(n for r, n in spread.items() if r <= 2),
        "spread": {str(r): spread.get(r, 0) for r in range(1, 6)},
    }

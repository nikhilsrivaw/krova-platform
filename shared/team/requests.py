"""
Talk to team: a customer asked to speak to a person - on a call, WhatsApp, Instagram or the website chat - or a missed
call is owed a call back.

It is not an escalation (that is the AI not knowing an answer). It waits on the "Talk to team" page for whoever is free,
owner or team member: they take it, talk to the customer with the whole story in the side panel (the briefing - every
channel's messages, notes, promises, the next visit - shared/team/briefing.py), and close it with a note that stays on
the customer, so the AI and the rest of the team know what was agreed next time.

One open request per customer: asking again while one is open adds to it rather than making another.
Told on creation: the chat's team when it has one, else everyone available (owner and admins included). Still waiting
after REMIND_AFTER: the owner and admins are told once.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import BusinessMember, BusinessRole, Customer, CustomerNote, TeamMember, TeamRequest, User
from shared.utils.logging import get_logger

logger = get_logger(__name__)

OPEN = ("open", "taken")
REMIND_AFTER = timedelta(minutes=15)
CHANNELS = ("voice", "whatsapp", "instagram", "web")

# A customer asking for a person: a person word AND a request in the same message. A person word alone ("team size",
# "manager ka naam") or a bank "transfer" never counts. Shared with the voice loop (shared/channels/voice/pipeline.py),
# where it hands the call over.
PERSON_NOUN = (
    r"(?:insaan\w*|human|real person|person|manager|owner|staff|team|agent|"
    r"executive|representative|operator|sales|someone|somebody|koi|kisi)"
)
PERSON_WANT = (
    r"(?:baat (?:karn|karwa|krn|krwa|kar|karo|karao)\w*|talk to|speak to|speak with|"
    r"connect (?:me|kar|karo|karwa|kardo|kar do|krdo|krwa|to)|bulao|bulwa\w*|"
    r"chahiye|karni hai|karna hai|want to)"
)
# The same, as a call's speech-to-text writes Hindi: in Devanagari, often mixed with English words ("मुझे team से बात
# करनी है"). Devanagari vowel signs are not "word" characters to Python's re, so these match without \b. "किसी"/"कोई"
# alone are weak ("कोई discount चाहिए" is not a person), so they count only with a talk word, never with "चाहिए".
DEVA_NOUN = r"(?:टीम|इंसान|इन्सान|आदमी|मैनेजर|मालिक|ओनर|स्टाफ|एजेंट|व्यक्ति|प्रतिनिधि|ऑपरेटर|एग्जीक्यूटिव|सेल्स)"
DEVA_WEAK = r"(?:किसी|कोई)"
DEVA_TALK = r"(?:बात\s*(?:कर|करा|करवा|हो)|(?:connect|baat|transfer)\s*(?:कर|करा|करवा)|कनेक्ट|बुला|जोड़|जोड|ट्रांसफर|संपर्क)"
DEVA_WANT = rf"(?:{DEVA_TALK}|चाहिए|चाहता|चाहती)"
PERSON_REQUEST = re.compile(
    rf"\b(?=.*\b{PERSON_NOUN}\b)(?=.*\b{PERSON_WANT}\b)"
    r"|\btransfer (?:kar|karo|kardo|kar do|karwa)\w*|\btransfer me\b"
    rf"|^(?=.*(?:\b{PERSON_NOUN}\b|{DEVA_NOUN}))(?=.*(?:\b{PERSON_WANT}\b|{DEVA_WANT}))"
    rf"|^(?=.*{DEVA_WEAK})(?=.*(?:{DEVA_TALK}|\b(?:talk to|speak to|speak with|connect)\b))"
    r"|ट्रांसफर\s*(?:कर|कीजिए|करें)",
    re.IGNORECASE,
)
# "Contact the team" asks for a person, but "the sales team's contact number" asks for a detail.
CONTACT_REQUEST = re.compile(
    rf"\b(?=.*\b{PERSON_NOUN}\b)(?=.*\b(?:contact|reach|sampark)\b)"
    r"(?!.*\b(?:numbers?|nambar|e-?mail|mail|address|details?|info|hours|timings?)\b)",
    re.IGNORECASE,
)
# In a chat, "call me back" is the same ask - they want a person on the phone.
CALLBACK_REQUEST = re.compile(
    r"\b(?:call\s*back|callback|call (?:me|karo|kijiye|kar do|kardo|karna|karein|kare)|mujhe call|"
    r"phone (?:karo|kijiye|kar do|kardo|karein)|ring me)\b",
    re.IGNORECASE,
)


def asks_for_person(text: str | None) -> bool:
    text = text or ""
    return bool(PERSON_REQUEST.search(text) or CONTACT_REQUEST.search(text) or CALLBACK_REQUEST.search(text))


def acknowledgement(language: str | None) -> str:
    """What the customer is told, in a chat, when their ask reaches the team."""
    if language == "hindi":
        return "ज़रूर - मैंने आपकी बात टीम तक पहुँचा दी है। टीम से कोई जल्द ही आपसे संपर्क करेगा। अगर कोई ख़ास समय ठीक रहे तो बता दीजिए।"
    if language == "hinglish":
        return "Zaroor - maine aapki baat team tak pahuncha di hai. Team se koi jaldi aapse baat karega. Koi khaas time theek rahe to bata dijiye."
    return "Sure - I've passed this to our team and someone will get in touch with you soon. If there's a good time to reach you, just let me know."


async def _people_to_tell(db: AsyncSession, business_id: uuid.UUID, team_id: uuid.UUID | None) -> list[uuid.UUID]:
    """The chat's team when it has one, else everyone available - owner and admins always."""
    query = (
        select(BusinessMember.user_id).join(User, User.id == BusinessMember.user_id)
        .where(BusinessMember.business_id == business_id, User.is_active == True)  # noqa: E712
    )
    rows = (await db.execute(query.add_columns(BusinessMember.role, BusinessMember.available))).all()
    supervisors = [r[0] for r in rows if r[1] in (BusinessRole.owner, BusinessRole.admin)]
    available = [r[0] for r in rows if r[2]]
    if team_id is not None:
        members = set((await db.execute(select(TeamMember.user_id).where(TeamMember.team_id == team_id))).scalars().all())
        chosen = [u for u in available if u in members] or available
    else:
        chosen = available
    return list(dict.fromkeys(chosen + supervisors))


async def _push(db: AsyncSession, business_id: uuid.UUID, user_ids: list[uuid.UUID], title: str, body: str) -> None:
    if not user_ids:
        return
    try:
        from shared.integrations import web_push

        await web_push.send_to_users(
            db, business_id=business_id, user_ids=user_ids, kind="team_request",
            payload={"title": title, "body": body[:140], "url": "/app/talk-to-team"},
        )
    except Exception:
        logger.exception("team request push failed business=%s", business_id)


async def open_request(
    db: AsyncSession, *, business_id: uuid.UUID, customer: Customer, channel: str, what: str,
    source: str = "customer", notify: bool = True,
) -> TeamRequest:
    """File the ask - or add to the one already open for this customer."""
    what = " ".join((what or "").split())[:500] or "Wants to talk to the team"
    existing = (await db.execute(
        select(TeamRequest).where(TeamRequest.customer_id == customer.id, TeamRequest.status.in_(OPEN))
        .order_by(TeamRequest.created_at.desc()).limit(1)
    )).scalars().first()
    if existing is not None:
        if what.lower() not in existing.what.lower():
            existing.what = f"{existing.what} | {what}"[-2000:]
        await db.flush()
        return existing
    request = TeamRequest(
        business_id=business_id, customer_id=customer.id, channel=channel if channel in CHANNELS else "whatsapp",
        what=what, source=source, status="open", team_id=getattr(customer, "team_id", None),
    )
    db.add(request)
    await db.flush()
    if notify:
        who = customer.display_name or "A customer"
        await _push(db, business_id, await _people_to_tell(db, business_id, request.team_id),
                    f"Talk to team: {who}", what)
    logger.info("team request business=%s customer=%s channel=%s source=%s", business_id, customer.id, channel, source)
    return request


class Taken(Exception):
    """Someone else is already on it."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


async def take(db: AsyncSession, *, request: TeamRequest, user_id: uuid.UUID, override: bool = False) -> TeamRequest:
    """'I'll take it': the request and the customer's chat become this person's. Someone else on it -> Taken."""
    if request.status == "taken" and request.taken_by_user_id not in (None, user_id) and not override:
        person = await db.get(User, request.taken_by_user_id)
        raise Taken(getattr(person, "full_name", None) or "A teammate")
    now = datetime.now(timezone.utc)
    request.status, request.taken_by_user_id, request.taken_at = "taken", user_id, now
    customer = await db.get(Customer, request.customer_id)
    if customer is not None:
        customer.assigned_to_user_id, customer.assigned_at = user_id, now
    await db.flush()
    return request


async def release(db: AsyncSession, *, request: TeamRequest) -> TeamRequest:
    request.status, request.taken_by_user_id, request.taken_at = "open", None, None
    await db.flush()
    return request


async def finish(db: AsyncSession, *, request: TeamRequest, user_id: uuid.UUID, note: str | None,
                 cancelled: bool = False) -> TeamRequest:
    """Close it. The note goes on the customer, so the whole team - and the AI's briefing - sees what was agreed."""
    request.status = "cancelled" if cancelled else "done"
    request.done_by_user_id, request.done_at = user_id, datetime.now(timezone.utc)
    request.outcome = (note or "").strip()[:2000] or None
    if request.outcome:
        db.add(CustomerNote(
            business_id=request.business_id, customer_id=request.customer_id, author_user_id=user_id,
            body=f"Talk to team - {request.what[:300]}: {request.outcome}"[:2000],
        ))
    await db.flush()
    return request


async def remind_waiting(db: AsyncSession, now: datetime | None = None) -> int:
    """Requests nobody took within REMIND_AFTER: the owner and admins hear once. Returns how many."""
    now = now or datetime.now(timezone.utc)
    rows = (await db.execute(
        select(TeamRequest, Customer.display_name).join(Customer, Customer.id == TeamRequest.customer_id)
        .where(TeamRequest.status == "open", TeamRequest.reminded_at.is_(None),
               TeamRequest.created_at < now - REMIND_AFTER)
        .limit(200)
    )).all()
    for request, name in rows:
        request.reminded_at = now
        supervisors = (await db.execute(select(BusinessMember.user_id).where(
            BusinessMember.business_id == request.business_id,
            BusinessMember.role.in_((BusinessRole.owner, BusinessRole.admin)),
        ))).scalars().all()
        await _push(db, request.business_id, list(supervisors), f"Still waiting: {name or 'a customer'}",
                    f"Asked to talk to the team {int((now - request.created_at).total_seconds() // 60)} min ago - nobody has taken it")
    await db.flush()
    return len(rows)

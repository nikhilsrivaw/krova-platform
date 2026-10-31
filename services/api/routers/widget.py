"""
The public embeddable website chat widget's door - a business embeds
{site_key} into their own site's <script> snippet, a visitor's browser
calls this directly, cross-origin (this runs on the business's own
website, not krova.space - see services/api/widget_cors.py for the
dynamic per-site_key CORS this needs instead of main.py's blanket, static
CORSMiddleware).

Same "public but not just anyone" model as kiosk.py: resolve tenant from
an opaque public identifier (site_key here, kiosk_token there), 404 on
unknown/inactive rather than a leaky distinguishing error. Deliberately no
CurrentUserDep anywhere in this file - DbDep only.

Three guardrails live here, in this order, each failing fast before the
next (and before ever calling the model) - see shared/channels/web/
guardrails.py's own docstring for why each one is a real, researched gap,
not speculative hardening: a business-wide rate limit, a DPDP-driven
consent gate before any conversation content is processed, and a per-
session turn cap.
"""

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import DbDep
from shared.billing.guards import plan_blocked
from shared.channels.web import guardrails, reply as web_reply, sites
from shared.db.models import (
    Business, Channel, ChannelConnection, ConnectionStatus, WebSession, WebWidgetConfig,
)
from shared.identity.normalise import InvalidIdentifier
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/widget", tags=["widget"])

_CONSENT_PROMPT = (
    "This chat is answered by an AI assistant using this business's own "
    "information. By continuing, you agree to share your message (and, if "
    "you choose to book something, your contact details) with them. Reply "
    "to continue."
)
_UNAVAILABLE = "Sorry, this chat is not available right now. Please reach us on WhatsApp or by phone."
DEFAULT_TITLE = "Chat with us"
DEFAULT_GREETING = "Hi! How can I help you today?"
DEFAULT_GREETING_HI = "नमस्ते! बताइए, मैं आपकी क्या मदद कर सकता हूँ?"
_CONTACT_FIRST = "Before we start, please share your name and phone number so we can help you better."
_CONTACT_FIRST_HI = "शुरू करने से पहले अपना नाम और फ़ोन नंबर बता दीजिए, ताकि हम आपकी बेहतर मदद कर सकें।"


async def _config_and_business(site_key: str, db: DbDep) -> tuple[WebWidgetConfig, Business]:
    result = await db.execute(
        select(WebWidgetConfig).where(
            WebWidgetConfig.site_key == site_key, WebWidgetConfig.active.is_(True)
        )
    )
    config = result.scalars().first()
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Widget not found")
    business = await db.get(Business, config.business_id)
    if business is None or not business.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Widget not found")
    return config, business


class ContactIn(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None


class MessageIn(BaseModel):
    session_token: str | None = None
    text: str = Field(min_length=1, max_length=4000)
    # True once the visitor has seen and accepted the widget's own consent
    # notice, client-side, before the chat input ever became usable - see
    # widget_asset.py's JS. The backend still enforces this itself rather
    # than trusting the client sent it honestly.
    consent: bool = False
    # Present the moment a visitor gives contact info - collect-and-trust,
    # no OTP verification, matches the real market norm (PatientCopilot,
    # SchedulingKit) found in research, not something stricter invented
    # for this.
    contact: ContactIn | None = None
    # The page the visitor is on (no query string is kept) - shown to the business with the chat.
    page_url: str | None = Field(default=None, max_length=2000)
    # The visitor's last question sent again together with their details - it is already in the chat, so it is
    # answered again but not stored twice.
    retry: bool = False


class ContactOnlyIn(BaseModel):
    """The "ask name and number first" form, sent before the first message."""
    session_token: str | None = None
    consent: bool = False
    contact: ContactIn
    page_url: str | None = Field(default=None, max_length=2000)


class MessageOut(BaseModel):
    session_token: str
    reply_text: str
    booked: bool
    # True when the visitor tried to book something but has not given
    # contact info yet - the widget's own UI should show a contact form
    # next rather than treat this as an ordinary answered turn.
    needs_contact: bool
    # True when this session has not accepted the consent notice yet -
    # the widget's own UI should show that notice and resend the same
    # text with consent=true, rather than treat this as a real reply.
    needs_consent: bool
    # True once the visitor has given a number, so a person can reply from the Inbox - the widget then
    # asks /updates every few seconds for those replies.
    team_can_reply: bool = False
    # The AI stayed quiet because a person is talking to this visitor; the message reached them.
    with_person: bool = False


class LookOut(BaseModel):
    title: str
    greeting: str
    accent: str | None
    position: str = "right"
    launcher_text: str | None = None
    teaser: str | None = None
    starters: list[str] = []
    ask_contact: str = "when_needed"
    language: str = "en"
    theme: str = "dark"
    # Digits only, for a wa.me link - set when the business turned the WhatsApp button on and has WhatsApp connected.
    whatsapp: str | None = None


class HistoryItem(BaseModel):
    role: str
    text: str
    at: str | None = None
    id: str | None = None


class HistoryOut(BaseModel):
    messages: list[HistoryItem]
    team_can_reply: bool


class ContactOut(BaseModel):
    session_token: str
    team_can_reply: bool


class TeamReply(BaseModel):
    id: str
    text: str
    at: str


class UpdatesOut(BaseModel):
    replies: list[TeamReply]


@router.get("/{site_key}/config", response_model=LookOut)
async def widget_config(site_key: str, db: DbDep) -> LookOut:
    """How the widget looks. 404 hides the widget (switched off, or the business's plan is paused)."""
    config, business = await _config_and_business(site_key, db)
    if await plan_blocked(db, business.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Widget not found")
    if sites.note_loaded(config):
        await db.flush()
    language = config.language or "en"
    whatsapp = None
    if config.whatsapp_button:
        handle = (await db.execute(select(ChannelConnection.external_handle).where(
            ChannelConnection.business_id == business.id, ChannelConnection.channel == Channel.whatsapp,
            ChannelConnection.status == ConnectionStatus.active).limit(1))).scalar_one_or_none()
        digits = "".join(ch for ch in (handle or "") if ch.isdigit())
        whatsapp = digits or None
    return LookOut(
        title=config.title or DEFAULT_TITLE,
        greeting=config.greeting or (DEFAULT_GREETING_HI if language == "hi" else DEFAULT_GREETING),
        accent=config.accent, position=config.position or "right", launcher_text=config.launcher_text,
        teaser=config.teaser, starters=list(config.starters or []), ask_contact=config.ask_contact or "when_needed",
        language=language, theme=config.theme or "dark", whatsapp=whatsapp,
    )


async def _session(db, business: Business, token: str | None) -> WebSession | None:
    if not token:
        return None
    return (await db.execute(select(WebSession).where(
        WebSession.session_token == token, WebSession.business_id == business.id))).scalars().first()


async def _new_session(db, business: Business, config: WebWidgetConfig, now: datetime) -> WebSession:
    session = WebSession(business_id=business.id, site_id=config.id, session_token=secrets.token_urlsafe(24),
                         last_seen_at=now)
    db.add(session)
    await db.flush()
    return session


async def _promote(db, business: Business, session: WebSession, contact: ContactIn) -> None:
    """Collect-and-trust, like the rest of the widget - a number that cannot be read is refused, not stored."""
    try:
        await web_reply.promote_session(
            db, business=business, session=session, display_name=(contact.name or "").strip()[:80] or None,
            phone=(contact.phone or "").strip() or None, email=(contact.email or "").strip() or None,
        )
    except (InvalidIdentifier, ValueError) as exc:
        raise HTTPException(422, "Please enter a valid phone number.") from exc


@router.get("/{site_key}/history", response_model=HistoryOut)
async def widget_history(
    site_key: str, db: DbDep, session_token: str = Query(min_length=8, max_length=64),
) -> HistoryOut:
    """This chat so far, so it is still there after a reload or on another page of the site."""
    config, business = await _config_and_business(site_key, db)
    session = await _session(db, business, session_token)
    if session is None:
        return HistoryOut(messages=[], team_can_reply=False)
    items = await sites.history(db, session)
    return HistoryOut(messages=[HistoryItem(**i) for i in items], team_can_reply=session.customer_id is not None)


@router.post("/{site_key}/contact", response_model=ContactOut)
async def widget_contact(site_key: str, body: ContactOnlyIn, db: DbDep) -> ContactOut:
    """Name and number given before the first message (the site asks for them first)."""
    config, business = await _config_and_business(site_key, db)
    if await plan_blocked(db, business.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Widget not found")
    if not guardrails.check_rate_limit(config):
        await db.flush()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests - please try again shortly")
    if not body.consent:
        raise HTTPException(422, "Please accept the notice first.")
    if not (body.contact.phone or body.contact.email):
        raise HTTPException(422, "Please enter your phone number.")
    now = datetime.now(timezone.utc)
    session = await _session(db, business, body.session_token) or await _new_session(db, business, config, now)
    session.consent_given_at = session.consent_given_at or now
    session.page_url = sites.clean_page(body.page_url) or session.page_url
    session.last_seen_at = now
    if session.customer_id is None:
        await _promote(db, business, session, body.contact)
    await db.flush()
    return ContactOut(session_token=session.session_token, team_can_reply=session.customer_id is not None)


@router.get("/{site_key}/updates", response_model=UpdatesOut)
async def widget_updates(
    site_key: str, db: DbDep, session_token: str = Query(min_length=8, max_length=64),
    after: datetime | None = None,
) -> UpdatesOut:
    """A person's replies from the Inbox since `after` - asked by the open widget every few seconds."""
    config, business = await _config_and_business(site_key, db)
    session = (await db.execute(select(WebSession).where(
        WebSession.session_token == session_token, WebSession.business_id == business.id))).scalars().first()
    if session is None or session.customer_id is None:
        return UpdatesOut(replies=[])
    since = after or session.consent_given_at or session.created_at
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    session.last_seen_at = datetime.now(timezone.utc)
    rows = await sites.team_replies(db, session.customer_id, since)
    await db.flush()
    return UpdatesOut(replies=[
        TeamReply(id=str(m.id), text=m.content or "", at=m.occurred_at.isoformat()) for m in rows if m.content
    ])


@router.post("/{site_key}/message", response_model=MessageOut)
async def widget_message(site_key: str, body: MessageIn, db: DbDep) -> MessageOut:
    config, business = await _config_and_business(site_key, db)
    if await plan_blocked(db, business.id):
        return MessageOut(session_token=body.session_token or "", reply_text=_UNAVAILABLE,
                          booked=False, needs_contact=False, needs_consent=False)

    if not guardrails.check_rate_limit(config):
        await db.flush()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests - please try again shortly")

    now = datetime.now(timezone.utc)
    session = await _session(db, business, body.session_token) or await _new_session(db, business, config, now)
    if session.site_id is None:
        session.site_id = config.id
    session.page_url = sites.clean_page(body.page_url) or session.page_url

    # DPDP: a notice before the conversation starts, not just before
    # contact info is collected - nothing about this message's actual
    # content is read or processed until consent is on record.
    if session.consent_given_at is None:
        if not body.consent:
            await db.flush()
            return MessageOut(
                session_token=session.session_token, reply_text=_CONSENT_PROMPT,
                booked=False, needs_contact=False, needs_consent=True,
            )
        session.consent_given_at = now

    if not guardrails.check_turn_cap(session):
        session.last_seen_at = now
        await db.flush()
        return MessageOut(
            session_token=session.session_token, reply_text=guardrails.TURN_CAP_MESSAGE,
            booked=False, needs_contact=False, needs_consent=False,
        )

    asked_again = None
    if body.contact and session.customer_id is None and (body.contact.phone or body.contact.email):
        await _promote(db, business, session, body.contact)
        if body.retry:
            asked_again = await web_reply.earlier_question(db, session, body.text)

    # The site asks for a name and number first - nothing goes to the AI until it has them.
    if config.ask_contact == "before_chat" and session.customer_id is None:
        session.last_seen_at = now
        await db.flush()
        return MessageOut(
            session_token=session.session_token,
            reply_text=_CONTACT_FIRST_HI if config.language == "hi" else _CONTACT_FIRST,
            booked=False, needs_contact=True, needs_consent=False,
        )

    guardrails.record_turn(session)
    outcome = await web_reply.generate_reply(db, business=business, session=session, text=body.text,
                                             already_stored=asked_again)
    session.last_seen_at = now
    await db.flush()

    logger.info(
        "widget message business=%s session=%s booked=%s needs_contact=%s",
        business.id, session.id, outcome.booked, outcome.needs_contact,
    )

    return MessageOut(
        session_token=session.session_token,
        reply_text=outcome.reply_text,
        booked=outcome.booked,
        needs_contact=outcome.needs_contact,
        needs_consent=False,
        team_can_reply=session.customer_id is not None,
        with_person=outcome.with_person,
    )

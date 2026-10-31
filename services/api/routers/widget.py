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
from shared.db.models import Business, WebSession, WebWidgetConfig
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
    return LookOut(title=config.title or DEFAULT_TITLE, greeting=config.greeting or DEFAULT_GREETING,
                   accent=config.accent)


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

    session: WebSession | None = None
    if body.session_token:
        result = await db.execute(
            select(WebSession).where(
                WebSession.session_token == body.session_token,
                WebSession.business_id == business.id,
            )
        )
        session = result.scalars().first()

    now = datetime.now(timezone.utc)
    if session is None:
        session = WebSession(
            business_id=business.id,
            session_token=secrets.token_urlsafe(24),
            last_seen_at=now,
        )
        db.add(session)
        await db.flush()

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

    if body.contact and session.customer_id is None and (body.contact.phone or body.contact.email):
        await web_reply.promote_session(
            db,
            business=business,
            session=session,
            display_name=body.contact.name,
            phone=body.contact.phone,
            email=body.contact.email,
        )

    guardrails.record_turn(session)
    outcome = await web_reply.generate_reply(db, business=business, session=session, text=body.text)
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

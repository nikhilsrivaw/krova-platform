"""
Payment links: connect the business's own gateway, send a link on WhatsApp, see whether it was paid.

Connecting is owner/admin only (it is the business's money). Sending a link is everyday desk work, so any team member
can do it. See shared/payments/links.py for how sending and the paid check work.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep, OwnerOrAdminDep
from shared.db.models import Business, Commitment, PaymentLink
from shared.payments import links

router = APIRouter(prefix="/payments", tags=["payments"])


class GatewayIn(BaseModel):
    gateway: Literal["razorpay"] = "razorpay"
    key_id: str = Field(min_length=4, max_length=120)
    key_secret: str = Field(min_length=4, max_length=200)


class GatewayOut(BaseModel):
    connected: bool
    gateway: str | None = None
    # never the secret; only enough of the key id to recognise it
    key_id_hint: str | None = None
    connected_at: str | None = None


class LinkOut(BaseModel):
    id: str
    commitment_id: str
    gateway: str
    short_url: str
    amount_paise: int
    status: str
    amount_paid_paise: int
    created_at: str
    paid_at: str | None


class SendOut(BaseModel):
    link: LinkOut
    sent: bool
    # why WhatsApp did not take it; the link still works and can be shared another way
    reason: str | None = None


class SendIn(BaseModel):
    commitment_id: uuid.UUID
    template_name: str | None = None
    template_language: str = "en"


class FeeIn(BaseModel):
    # optional for an appointment: the doctor's consultation fee is used when it is set
    amount_paise: int | None = Field(default=None, ge=100, le=50_000_000)
    template_name: str | None = None
    template_language: str = "en"


def _link_out(link: PaymentLink) -> LinkOut:
    return LinkOut(
        id=str(link.id), commitment_id=str(link.commitment_id), gateway=link.gateway, short_url=link.short_url,
        amount_paise=link.amount_paise, status=link.status, amount_paid_paise=link.amount_paid_paise,
        created_at=(link.created_at or datetime.now(timezone.utc)).isoformat(),
        paid_at=link.paid_at.isoformat() if link.paid_at else None,
    )


def _gateway_out(row) -> GatewayOut:
    if row is None or getattr(row.status, "value", row.status) != "active":
        return GatewayOut(connected=False)
    return GatewayOut(
        connected=True, gateway=row.gateway, key_id_hint=f"{row.key_id[:8]}...{row.key_id[-4:]}",
        connected_at=row.connected_at.isoformat() if row.connected_at else None,
    )


async def _business(db, current_user) -> Business:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status_code=404, detail="Not found")
    return business


def _refuse(exc: links.LinkError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


# ── the gateway ──────────────────────────────────────────────────────────────

@router.get("/gateway", response_model=GatewayOut)
async def get_gateway(current_user: CurrentUserDep, db: DbDep) -> GatewayOut:
    return _gateway_out(await links.connection_for(db, current_user.business))


@router.put("/gateway", response_model=GatewayOut)
async def connect_gateway(body: GatewayIn, current_user: OwnerOrAdminDep, db: DbDep) -> GatewayOut:
    """Save the business's own gateway key. It is checked with the gateway first; a key that does not work is not saved."""
    try:
        row = await links.connect(db, current_user.business, gateway=body.gateway, key_id=body.key_id, key_secret=body.key_secret)
    except links.LinkError as exc:
        raise _refuse(exc) from exc
    return _gateway_out(row)


@router.delete("/gateway", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_gateway(current_user: OwnerOrAdminDep, db: DbDep) -> None:
    row = await links.connection_for(db, current_user.business)
    if row is not None:
        await db.delete(row)


class AutoLinkIn(BaseModel):
    tokens: bool = False
    appointments: bool = False
    # used when the doctor has no consultation fee set (a token the AI books has no doctor)
    default_fee_paise: int | None = Field(default=None, ge=100, le=50_000_000)


@router.get("/auto", response_model=AutoLinkIn)
async def get_auto(current_user: CurrentUserDep, db: DbDep) -> AutoLinkIn:
    return AutoLinkIn(**links.auto_settings(await _business(db, current_user)))


@router.put("/auto", response_model=AutoLinkIn)
async def set_auto(body: AutoLinkIn, current_user: OwnerOrAdminDep, db: DbDep) -> AutoLinkIn:
    """Send the fee link by itself right after the AI books a token or an appointment on WhatsApp."""
    business = await _business(db, current_user)
    settings_ = dict(business.settings or {})
    pay = dict(settings_.get("payments") or {})
    pay["auto_link"] = body.model_dump()
    settings_["payments"] = pay
    business.settings = settings_
    await db.flush()
    return AutoLinkIn(**links.auto_settings(business))


# ── links ────────────────────────────────────────────────────────────────────

async def _send(db, current_user, commitment: Commitment, template_name: str | None, template_language: str) -> SendOut:
    business = await _business(db, current_user)
    try:
        link, sent, reason = await links.send_link(
            db, business=business, commitment=commitment, user_id=current_user.id,
            template_name=template_name, template_language=template_language,
        )
    except links.LinkError as exc:
        raise _refuse(exc) from exc
    return SendOut(link=_link_out(link), sent=sent, reason=reason)


@router.post("/links", response_model=SendOut)
async def send_for_commitment(body: SendIn, current_user: CurrentUserDep, db: DbDep) -> SendOut:
    """A link for what a customer still owes on a ledger item, sent on WhatsApp."""
    commitment = await db.get(Commitment, body.commitment_id)
    if commitment is None or commitment.business_id != current_user.business:
        raise HTTPException(status_code=404, detail="Not found")
    return await _send(db, current_user, commitment, body.template_name, body.template_language)


async def _send_fee(db, current_user, kind: str, ref_id: uuid.UUID, body: FeeIn) -> SendOut:
    business = await _business(db, current_user)
    try:
        link, sent, reason = await links.send_fee_link(
            db, business=business, kind=kind, ref_id=ref_id, amount_paise=body.amount_paise, user_id=current_user.id,
            template_name=body.template_name, template_language=body.template_language,
        )
    except links.LinkError as exc:
        raise _refuse(exc) from exc
    if link is None:
        raise HTTPException(status_code=409, detail=reason or "Nothing to collect.")
    return SendOut(link=_link_out(link), sent=sent, reason=reason)


@router.post("/queue/{entry_id}/link", response_model=SendOut)
async def send_for_token(entry_id: uuid.UUID, body: FeeIn, current_user: CurrentUserDep, db: DbDep) -> SendOut:
    """The fee for a token (amount given, else the doctor's fee, else the default fee), sent as a link - once per token."""
    return await _send_fee(db, current_user, "token", entry_id, body)


@router.post("/appointments/{appointment_id}/link", response_model=SendOut)
async def send_for_appointment(appointment_id: uuid.UUID, body: FeeIn, current_user: CurrentUserDep, db: DbDep) -> SendOut:
    """The fee for an appointment (amount given, else the doctor's fee, else the default fee) - once per appointment."""
    return await _send_fee(db, current_user, "appointment", appointment_id, body)


@router.get("/links", response_model=list[LinkOut])
async def list_links(
    current_user: CurrentUserDep, db: DbDep, commitment_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, le=200),
) -> list[LinkOut]:
    query = select(PaymentLink).where(PaymentLink.business_id == current_user.business)
    if commitment_id is not None:
        query = query.where(PaymentLink.commitment_id == commitment_id)
    rows = (await db.execute(query.order_by(PaymentLink.created_at.desc()).limit(limit))).scalars().all()
    return [_link_out(r) for r in rows]


@router.post("/links/{link_id}/check", response_model=LinkOut)
async def check_link(link_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> LinkOut:
    """Ask the gateway now, instead of waiting for the next automatic check."""
    link = await db.get(PaymentLink, link_id)
    if link is None or link.business_id != current_user.business:
        raise HTTPException(status_code=404, detail="Not found")
    row = await links.connection_for(db, current_user.business)
    if row is None:
        raise HTTPException(status_code=422, detail="Connect your payment gateway in Settings first.")
    await links.refresh_link(db, link, row, datetime.now(timezone.utc))
    await db.flush()
    return _link_out(link)

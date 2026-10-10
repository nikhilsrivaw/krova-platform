"""
Billing: the plan, the wallet, and PayU.

Owner-only: choosing or cancelling a plan. Owner and admin: adding wallet money.
The two PayU endpoints (`/payu/return`, `/payu/webhook`) are public - PayU's
servers and the customer's browser call them - and are safe because they only
carry a transaction id: the result is always fetched from PayU and checked
against our own record before anything is applied.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import DbDep, OwnerOrAdminDep
from shared.audit import activity
from shared.billing import payu, plans, service, wallet
from shared.config.settings import settings
from shared.db.models import Business, Payment, Subscription, User, WalletEntry
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


class PlanOut(BaseModel):
    key: str
    label: str
    base: str
    gst: str
    total: str


class SubscriptionOut(BaseModel):
    plan: str
    status: str
    amount: str
    current_period_end: datetime
    next_charge_at: datetime | None
    cancel_at_period_end: bool
    last_failure: str | None
    retry_until: datetime | None


class EntryOut(BaseModel):
    at: datetime
    kind: str
    amount: str
    balance_after: str
    note: str | None


class PaymentOut(BaseModel):
    at: datetime
    purpose: str
    status: str
    total: str
    failure: str | None


class BillingOut(BaseModel):
    payments_enabled: bool
    plans: list[PlanOut]
    subscription: SubscriptionOut | None
    blocked: bool
    wallet_balance: str | None
    number_rent: str
    low_balance: bool
    entries: list[EntryOut]
    payments: list[PaymentOut]


def _r(paise: int) -> str:
    return f"{paise / 100:.2f}"


@router.get("", response_model=BillingOut)
async def overview(current_user: OwnerOrAdminDep, db: DbDep) -> BillingOut:
    sub = await db.get(Subscription, current_user.business)
    balance = await wallet.balance(db, current_user.business)
    entries = (await db.execute(
        select(WalletEntry).where(WalletEntry.business_id == current_user.business)
        .order_by(WalletEntry.created_at.desc()).limit(20)
    )).scalars().all()
    payments = (await db.execute(
        select(Payment).where(Payment.business_id == current_user.business)
        .order_by(Payment.created_at.desc()).limit(10)
    )).scalars().all()
    catalogue = []
    for key, label in plans.PLAN_LABELS.items():
        base, gst, total = plans.plan_charge(key)
        catalogue.append(PlanOut(key=key, label=label, base=_r(base), gst=_r(gst), total=_r(total)))
    return BillingOut(
        payments_enabled=payu.enabled(),
        plans=catalogue,
        subscription=SubscriptionOut(
            plan=sub.plan, status=sub.status, amount=_r(sub.amount_paise),
            current_period_end=sub.current_period_end, next_charge_at=sub.next_charge_at,
            cancel_at_period_end=sub.cancel_at_period_end, last_failure=sub.last_failure,
            retry_until=sub.retry_until,
        ) if sub else None,
        blocked=service.is_blocked(sub),
        wallet_balance=_r(balance) if balance is not None else None,
        number_rent=_r(plans.NUMBER_RENT_PAISE),
        low_balance=balance is not None and balance < plans.LOW_BALANCE_PAISE,
        entries=[EntryOut(at=e.created_at, kind=e.kind, amount=_r(e.amount_paise),
                          balance_after=_r(e.balance_after_paise), note=e.note) for e in entries],
        payments=[PaymentOut(at=p.created_at, purpose=p.purpose, status=p.status, total=_r(p.total_paise),
                             failure=p.failure) for p in payments],
    )


class Checkout(BaseModel):
    """A form the browser posts to PayU: action URL plus the (already signed) fields."""

    action: str
    fields: dict[str, str]
    total: str


class QuoteOut(BaseModel):
    credit: str
    gst: str
    fee: str
    total: str


@router.get("/quote", response_model=QuoteOut)
async def quote(amount_rupees: int, current_user: OwnerOrAdminDep) -> QuoteOut:
    """What a wallet top-up of this size costs, line by line."""
    try:
        credit, gst, fee, total = plans.topup_charge(amount_rupees * 100, settings.payu_fee_pct)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return QuoteOut(credit=_r(credit), gst=_r(gst), fee=_r(fee), total=_r(total))


class SubscribeIn(BaseModel):
    plan: Literal["starter", "pro", "scale"]


async def _context(current_user, db) -> tuple[Business, User]:
    business = await db.get(Business, current_user.business)
    user = await db.get(User, current_user.id)
    if business is None or user is None:
        raise HTTPException(404, "Account not found")
    return business, user


@router.post("/subscribe", response_model=Checkout)
async def subscribe(body: SubscribeIn, current_user: OwnerOrAdminDep, db: DbDep) -> Checkout:
    if current_user.role != "owner":
        raise HTTPException(403, "Only the owner can choose or change the plan.")
    business, user = await _context(current_user, db)
    try:
        payment, fields = await service.start_plan_checkout(db, business=business, user=user, plan=body.plan)
    except payu.PayUError as exc:
        raise HTTPException(503, str(exc)) from exc
    activity.note(plan=body.plan)
    return Checkout(action=payu.checkout_url(), fields=fields, total=_r(payment.total_paise))


class TopupIn(BaseModel):
    amount_rupees: int = Field(ge=100, le=50_000)


@router.post("/topup", response_model=Checkout)
async def topup(body: TopupIn, current_user: OwnerOrAdminDep, db: DbDep) -> Checkout:
    business, user = await _context(current_user, db)
    try:
        payment, fields = await service.start_topup_checkout(
            db, business=business, user=user, credit_paise=body.amount_rupees * 100
        )
    except payu.PayUError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    activity.note(credit=body.amount_rupees)
    return Checkout(action=payu.checkout_url(), fields=fields, total=_r(payment.total_paise))


@router.post("/cancel")
async def cancel(current_user: OwnerOrAdminDep, db: DbDep) -> dict:
    """Stop renewing. The plan keeps working until the month already paid for ends."""
    if current_user.role != "owner":
        raise HTTPException(403, "Only the owner can cancel the plan.")
    sub = await db.get(Subscription, current_user.business, with_for_update=True)
    if sub is None:
        raise HTTPException(404, "There is no plan to cancel.")
    sub.cancel_at_period_end = True
    return {"ends_on": sub.current_period_end}


@router.post("/resume")
async def resume(current_user: OwnerOrAdminDep, db: DbDep) -> dict:
    if current_user.role != "owner":
        raise HTTPException(403, "Only the owner can change the plan.")
    sub = await db.get(Subscription, current_user.business, with_for_update=True)
    if sub is None or sub.status == "cancelled":
        raise HTTPException(409, "This plan has already ended - choose a plan again.")
    sub.cancel_at_period_end = False
    return {"resumed": True}


# ── PayU, calling us ─────────────────────────────────────────────────────────

@router.post("/payu/return")
async def payu_return(request: Request, db: DbDep) -> RedirectResponse:
    """The browser comes back from PayU here (success or failure). Nothing in the form is trusted."""
    form = await request.form()
    txnid = str(form.get("txnid") or "")[:25]
    outcome = "unknown"
    if txnid:
        try:
            payment = await service.settle_payment(db, txnid)
            outcome = payment.status if payment else "unknown"
        except Exception:
            logger.exception("payu return failed txnid=%s", txnid)
    return RedirectResponse(
        f"{settings.frontend_base_url.rstrip('/')}/billing?payment={outcome}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/payu/webhook")
async def payu_webhook(request: Request, db: DbDep) -> dict:
    """PayU tells us a transaction changed (a UPI payment finishing, a mandate charge landing)."""
    try:
        if "json" in (request.headers.get("content-type") or ""):
            data = await request.json()
        else:
            data = dict(await request.form())
    except Exception:
        return {"ok": False}
    txnid = str((data or {}).get("txnid") or "")[:25]
    if txnid:
        try:
            await service.settle_payment(db, txnid)
        except Exception:
            logger.exception("payu webhook failed txnid=%s", txnid)
    return {"ok": True}


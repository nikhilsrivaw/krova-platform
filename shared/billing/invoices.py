"""
Making the invoice or receipt for a payment that has just succeeded.

Called from the same place that applies the payment (service._apply_success), once per payment:
  plan / renewal  -> a GST tax invoice from Aqirox Technology Private Limited
  topup           -> a payment receipt (no GST on the wallet credit; the gateway fee is listed on its own)

The buyer's details are copied onto the document as they are right now and never change afterwards.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.billing import gst, plans, supplier
from shared.db.models import BusinessMember, BusinessRole, Invoice, InvoiceCounter, User
from shared.utils.logging import get_logger

logger = get_logger(__name__)

PREFIX = {"invoice": "AQX", "receipt": "AQXR"}


def number_for(kind: str, fy: str, n: int) -> str:
    """AQX/2627/000001 - at most 16 characters, as the GST rules require."""
    return f"{PREFIX[kind]}/{fy}/{n:06d}"


async def _next(db: AsyncSession, kind: str, fy: str) -> int:
    """The next number in a series. One atomic statement, so two payments can never share a number."""
    stmt = (
        pg_insert(InvoiceCounter)
        .values(kind=kind, fy=fy, last=1)
        .on_conflict_do_update(index_elements=[InvoiceCounter.kind, InvoiceCounter.fy], set_={"last": InvoiceCounter.last + 1})
        .returning(InvoiceCounter.last)
    )
    return int((await db.execute(stmt)).scalar_one())


def buyer_details(business, owner_email: str | None) -> dict:
    """The business's saved billing details, or sensible fallbacks (its name, and Aqirox's own state)."""
    saved = (business.settings or {}).get("billing_details") or {}
    gstin = (saved.get("gstin") or "").strip().upper() or None
    state = saved.get("state_code") or (gstin[:2] if gstin else None) or supplier.STATE_CODE
    return {
        "name": (saved.get("legal_name") or business.name or "Customer").strip(),
        "address": (saved.get("address") or "").strip() or None,
        "state_code": state if state in gst.STATES else supplier.STATE_CODE,
        "gstin": gstin,
        "email": (saved.get("email") or owner_email or "").strip() or None,
    }


async def owner_email(db: AsyncSession, business_id: uuid.UUID) -> str | None:
    row = await db.execute(
        select(User.email)
        .join(BusinessMember, BusinessMember.user_id == User.id)
        .where(BusinessMember.business_id == business_id, BusinessMember.role == BusinessRole.owner)
        .order_by(BusinessMember.created_at)
        .limit(1)
    )
    return row.scalar_one_or_none()


async def create_for_payment(
    db: AsyncSession, payment, *, business, period_start: datetime | None = None, period_end: datetime | None = None,
) -> Invoice:
    """The document for a successful payment. Safe to call twice: the second call returns the first one."""
    existing = (await db.execute(select(Invoice).where(Invoice.payment_id == payment.id))).scalar_one_or_none()
    if existing is not None:
        return existing

    now = payment.paid_at or datetime.now(timezone.utc)
    topup = payment.purpose == "topup"
    kind = "receipt" if topup else "invoice"
    fy = gst.financial_year(now)
    buyer = buyer_details(business, await owner_email(db, business.id))

    if topup:
        taxable = cgst = sgst = igst = 0
        lines = [
            {"description": "Wallet top-up (credited in full)", "detail": "Added to your KROVA wallet balance",
             "amount_paise": payment.base_paise},
            {"description": "Payment gateway fee", "detail": "Charged by the payment gateway for processing this payment",
             "amount_paise": payment.fee_paise},
        ]
    else:
        taxable, cgst, sgst, igst = gst.split_gst(payment.total_paise, intra_state=buyer["state_code"] == supplier.STATE_CODE)
        label = plans.PLAN_LABELS.get(payment.plan or "", (payment.plan or "").title())
        lines = [{
            "description": f"KROVA {label} plan - monthly subscription",
            "detail": f"{supplier.PRODUCT_LINE} AI layer for WhatsApp, Instagram and voice.",
            "amount_paise": taxable,
        }]

    invoice = Invoice(
        business_id=business.id, payment_id=payment.id, kind=kind,
        number=number_for(kind, fy, await _next(db, kind, fy)), issued_at=now,
        buyer_name=buyer["name"], buyer_address=buyer["address"], buyer_state_code=buyer["state_code"],
        buyer_gstin=buyer["gstin"], buyer_email=buyer["email"],
        service_from=period_start, service_to=period_end, lines=lines,
        taxable_paise=taxable, cgst_paise=cgst, sgst_paise=sgst, igst_paise=igst,
        fee_paise=payment.fee_paise if topup else 0, total_paise=payment.total_paise,
        payment_mode=payment.mode, payment_ref=payment.txnid, next_renewal=None if topup else period_end,
    )
    db.add(invoice)
    await db.flush()
    logger.info("%s %s issued business=%s payment=%s total=%s", kind, invoice.number, business.id, payment.id, payment.total_paise)
    return invoice

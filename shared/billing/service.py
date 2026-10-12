"""
Taking money and keeping plans alive.

  start_plan_checkout / start_topup_checkout   build the payment + the PayU form
  settle_payment                               the ONE place a payment becomes real
                                               (asks PayU, checks the amount, applies it once)
  renewal_sweep                                every 30 min: tell PayU a charge is coming,
                                               charge it, retry a failed one for a week
  is_blocked                                   has this business been switched to read-only?

The retry rule: a failed renewal is tried again 51 hours later (PayU needs a
48-hour notice before every debit, and UPI AutoPay allows at most 4 attempts),
so a month's charge is attempted on day 0, 2, 4 and 6; if it still has not gone
through after 7 days the plan is suspended - AI sending and campaigns stop, data
stays visible - and starts again the moment a payment succeeds.
"""

import calendar
import hmac
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.billing import payu, plans, wallet
from shared.config.settings import settings
from shared.db.models import Business, Payment, Subscription
from shared.utils.logging import get_logger

logger = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
PRE_DEBIT_LEAD = timedelta(hours=50)    # PayU wants 48h; sweeps run every 30 min
RETRY_GAP = timedelta(hours=51)
RETRY_WINDOW = timedelta(days=7)
MAX_ATTEMPTS = 4                        # per month; UPI AutoPay's limit
MANDATE_YEARS = 5
PENDING_EXPIRY = timedelta(days=3)
# A renewal PayU has no record of is not going to turn up: judge it in hours, so the retry clock
# and the week's grace are not spent waiting on a charge that never happened.
RENEWAL_NOT_FOUND_EXPIRY = timedelta(hours=6)

FAILED_STATES = {"failure", "failed", "bounced", "dropped", "usercancelled", "cancelled", "error"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def add_month(moment: datetime) -> datetime:
    year = moment.year + (moment.month // 12)
    month = moment.month % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def new_txnid(prefix: str = "KR") -> str:
    return (prefix + uuid.uuid4().hex)[:25]


def is_blocked(sub: Subscription | None, now: datetime | None = None) -> bool:
    """Read-only? Only a business that has a subscription can be: earlier customers are untouched."""
    if sub is None:
        return False
    now = now or _now()
    if sub.status == "suspended":
        return True
    return sub.status == "cancelled" and sub.current_period_end < now


def invoice_for(business_id: uuid.UUID, debit_on: date) -> str:
    """
    The invoice number of one scheduled debit. The pre-debit notice and the charge must carry the SAME
    number (PayU matches them by it), so it is derived from what they share - the business and the
    debit date - rather than invented separately at each step.
    """
    return f"INV{business_id.hex[:10]}{debit_on:%y%m%d}"


def run_window_ok(now: datetime) -> bool:
    """UPI AutoPay debits are not allowed 10:00-13:00 and 17:00-21:30 IST."""
    t = now.astimezone(IST)
    minutes = t.hour * 60 + t.minute
    return not (10 * 60 <= minutes < 13 * 60 or 17 * 60 <= minutes < 21 * 60 + 30)


# ── Checkout ─────────────────────────────────────────────────────────────────

def return_url() -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/v1/billing/payu/return"


TEST_COUPON_PAISE = 100  # the first month costs Rs 1 with the test coupon


class CouponError(ValueError):
    """The code is wrong, switched off, or already not usable by this business."""


async def check_test_coupon(db: AsyncSession, business: Business, code: str) -> None:
    """
    The one test coupon: the first month costs Rs 1, AutoPay then takes the full price next month.
    The code lives only in the server's TEST_COUPON_CODE (empty = coupon off), and works only for a
    business that has never paid for a plan.
    """
    expected = settings.test_coupon_code.strip()
    if not expected or not hmac.compare_digest(code.strip().encode(), expected.encode()):
        raise CouponError("That coupon code is not valid.")
    if await db.get(Subscription, business.id) is not None:
        raise CouponError("This coupon is only for a first plan payment.")


async def start_plan_checkout(
    db: AsyncSession, *, business: Business, user, plan: str, coupon: str | None = None
) -> tuple[Payment, dict]:
    base, gst, total = plans.plan_charge(plan)
    # With the coupon the customer pays Rs 1 now, but the plan (and the AutoPay mandate) stay at full
    # price: base/gst keep the real price, total is what is charged today.
    pay_now = total
    if coupon:
        await check_test_coupon(db, business, coupon)
        pay_now = TEST_COUPON_PAISE
    payment = Payment(
        business_id=business.id, purpose="plan", plan=plan, base_paise=base, gst_paise=gst,
        fee_paise=0, total_paise=pay_now, txnid=new_txnid(),
    )
    db.add(payment)
    await db.flush()
    today = _now().astimezone(IST).date()
    start = add_month(datetime.combine(today, datetime.min.time())).date()
    end = date(today.year + MANDATE_YEARS, today.month, min(today.day, 28))
    fields = payu.checkout_fields(
        txnid=payment.txnid, amount_paise=pay_now, productinfo=f"KROVA {plans.PLAN_LABELS[plan]} plan monthly",
        firstname=user.full_name or business.name, email=user.email or "billing@krova.space",
        phone=user.phone or "", return_url=return_url(), udf1=str(business.id),
        si_details=payu.si_details_json(amount_paise=total, start=start, end=end),
    )
    return payment, fields


async def start_topup_checkout(
    db: AsyncSession, *, business: Business, user, credit_paise: int
) -> tuple[Payment, dict]:
    credit, gst, fee, total = plans.topup_charge(credit_paise, settings.payu_fee_pct)
    payment = Payment(
        business_id=business.id, purpose="topup", base_paise=credit, gst_paise=gst, fee_paise=fee,
        total_paise=total, txnid=new_txnid(),
    )
    db.add(payment)
    await db.flush()
    fields = payu.checkout_fields(
        txnid=payment.txnid, amount_paise=total, productinfo="KROVA wallet top-up",
        firstname=user.full_name or business.name, email=user.email or "billing@krova.space",
        phone=user.phone or "", return_url=return_url(), udf1=str(business.id),
    )
    return payment, fields


# ── Settling ─────────────────────────────────────────────────────────────────

async def settle_payment(db: AsyncSession, txnid: str) -> Payment | None:
    """
    Ask PayU what happened to this transaction and apply it - once. Safe to call
    from the browser return, the webhook and the sweep, in any order, any number
    of times: only a payment still `pending` is touched, under a row lock.
    """
    row = await db.execute(select(Payment).where(Payment.txnid == txnid).with_for_update())
    payment = row.scalar_one_or_none()
    if payment is None or payment.status != "pending":
        return payment

    details = await payu.verify_payment(txnid)
    now = _now()
    if details is None:
        expiry = RENEWAL_NOT_FOUND_EXPIRY if payment.purpose == "renewal" else PENDING_EXPIRY
        if payment.created_at < now - expiry:
            await _fail(db, payment, "Payment was never completed", now)
        return payment

    state = str(details.get("status") or "").lower()
    payment.raw = {k: details.get(k) for k in ("mihpayid", "status", "mode", "amt", "error_Message", "unmappedstatus")}
    payment.payu_id = str(details.get("mihpayid") or "") or None
    payment.mode = (details.get("mode") or None)

    if state == "success":
        paid = round(float(details.get("amt") or details.get("amount") or 0) * 100)
        if abs(paid - payment.total_paise) > 1:
            await _fail(db, payment, f"Amount mismatch: expected {payment.total_paise}, PayU says {paid}", now)
            logger.error("payu amount mismatch txnid=%s expected=%s got=%s", txnid, payment.total_paise, paid)
            return payment
        payment.status = "success"
        payment.paid_at = now
        await _apply_success(db, payment, now)
    elif state in FAILED_STATES:
        await _fail(db, payment, str(details.get("error_Message") or state)[:300], now)
    elif payment.created_at < now - PENDING_EXPIRY:
        # Still "pending" at PayU after days: the bank is never going to confirm it.
        await _fail(db, payment, "The bank never confirmed this payment", now)
    return payment


async def _apply_success(db: AsyncSession, payment: Payment, now: datetime) -> None:
    if payment.purpose == "topup":
        await wallet.post(
            db, business_id=payment.business_id, amount_paise=payment.base_paise, kind="topup",
            ref=payment.txnid, note="Wallet top-up",
        )
        return

    sub = await db.get(Subscription, payment.business_id, with_for_update=True)
    business = await db.get(Business, payment.business_id)
    if payment.purpose == "plan":
        end = add_month(now)
        full_price = payment.base_paise + payment.gst_paise  # not total_paise: a coupon lowers today's charge only
        if sub is None:
            sub = Subscription(business_id=payment.business_id, plan=payment.plan, amount_paise=full_price,
                               current_period_end=end)
            db.add(sub)
        sub.plan = payment.plan
        sub.amount_paise = full_price
        sub.authpayuid = payment.payu_id
        sub.current_period_end = end
        sub.next_charge_at = end
    else:  # renewal
        if sub is None:
            return
        sub.current_period_end = add_month(max(sub.current_period_end, now))
        sub.next_charge_at = sub.current_period_end
    sub.status = "active"
    sub.attempts = 0
    sub.retry_until = None
    sub.pre_debit_sent_for = None
    sub.last_failure = None
    sub.cancel_at_period_end = False
    sub.updated_at = now
    if business is not None:
        business.plan = sub.plan


async def _fail(db: AsyncSession, payment: Payment, reason: str, now: datetime) -> None:
    payment.status = "failed"
    payment.failure = reason
    if payment.purpose != "renewal":
        return
    sub = await db.get(Subscription, payment.business_id, with_for_update=True)
    if sub is None:
        return
    sub.last_failure = reason
    sub.retry_until = sub.retry_until or (now + RETRY_WINDOW)
    sub.updated_at = now
    sub.pre_debit_sent_for = None
    if sub.attempts >= MAX_ATTEMPTS or now + RETRY_GAP > sub.retry_until:
        sub.status = "suspended"
        sub.next_charge_at = None
        await wallet._tell_owner(
            db, sub.business_id, "Your KROVA plan is paused",
            "We could not collect the monthly payment. Add a payment method on the Billing page to continue.",
        )
    else:
        sub.status = "past_due"
        sub.next_charge_at = now + RETRY_GAP
        await wallet._tell_owner(
            db, sub.business_id, "Payment failed - we will try again",
            "Your monthly KROVA payment did not go through. Make sure your bank or UPI has enough balance.",
        )


# ── The sweep ────────────────────────────────────────────────────────────────

async def renewal_sweep(db: AsyncSession) -> dict:
    """Pre-debit notices, charges, retries, and settling payments still pending. Returns counts."""
    now = _now()
    out = {"pre_debit": 0, "charged": 0, "settled": 0, "suspended": 0}
    if not payu.enabled():
        return out

    subs = (await db.execute(
        select(Subscription).where(Subscription.status.in_(("active", "past_due", "cancelled")))
    )).scalars().all()
    for sub in subs:
        # The backstop: whatever went wrong (no mandate, a revoked one, PayU refusing every notice), a
        # plan that is still unpaid a week after its month ended is paused. Nothing renews forever on
        # silence.
        if (
            not sub.cancel_at_period_end and sub.status in ("active", "past_due")
            and now > sub.current_period_end + RETRY_WINDOW
        ):
            await _suspend(db, sub, sub.last_failure or "The monthly payment did not go through", now)
            out["suspended"] += 1
            continue
        if sub.cancel_at_period_end:
            if sub.current_period_end < now and sub.status != "cancelled":
                sub.status = "cancelled"
            continue
        if sub.status == "cancelled" or sub.next_charge_at is None or not sub.authpayuid:
            continue
        waiting = await db.execute(
            select(Payment.id).where(
                Payment.business_id == sub.business_id, Payment.purpose == "renewal", Payment.status == "pending"
            ).limit(1)
        )
        if waiting.first() is not None:
            continue  # a charge is still in flight; do not notify or charge again on top of it
        try:
            if sub.pre_debit_sent_for != sub.next_charge_at and now >= sub.next_charge_at - PRE_DEBIT_LEAD:
                debit_on = sub.next_charge_at.astimezone(IST).date()
                is_upi, seq = await _mandate(db, sub)
                reply = await payu.pre_debit(
                    authpayuid=sub.authpayuid, request_id=new_txnid("PD"), debit_date=debit_on,
                    amount_paise=sub.amount_paise, invoice=invoice_for(sub.business_id, debit_on),
                    mandate_seq=seq if is_upi else None,
                )
                # E9254 / E9256: a notice (or the debit itself) is already on file for this one - not an error.
                if str(reply.get("status")) in ("1", "E9254", "E9256"):
                    sub.pre_debit_sent_for = sub.next_charge_at
                    out["pre_debit"] += 1
                else:
                    await _pre_debit_refused(db, sub, reply, now)
                continue
            if sub.pre_debit_sent_for == sub.next_charge_at and now >= sub.next_charge_at and run_window_ok(now):
                await _charge(db, sub, now)
                out["charged"] += 1
        except Exception:
            logger.exception("renewal step failed business=%s", sub.business_id)

    pending = (await db.execute(
        select(Payment.txnid).where(
            Payment.status == "pending", Payment.created_at < now - timedelta(minutes=3)
        )
    )).scalars().all()
    for txnid in pending:
        try:
            await settle_payment(db, txnid)
            out["settled"] += 1
        except Exception:
            logger.exception("settling %s failed", txnid)
    return out


async def _mandate(db: AsyncSession, sub: Subscription) -> tuple[bool, int]:
    """
    (is this mandate UPI AutoPay, the execution number of the next debit). The registration payment was
    execution 1, so the first renewal is 2, the next 3, and so on - UPI AutoPay needs it in the notice.
    """
    registration = (await db.execute(
        select(Payment.mode, Payment.paid_at).where(
            Payment.business_id == sub.business_id, Payment.payu_id == sub.authpayuid, Payment.purpose == "plan",
        ).order_by(Payment.paid_at.desc()).limit(1)
    )).first()
    if registration is None:
        return False, 2
    mode, paid_at = registration
    renewals = (await db.execute(
        select(func.count(Payment.id)).where(
            Payment.business_id == sub.business_id, Payment.purpose == "renewal", Payment.status == "success",
            Payment.paid_at >= paid_at,
        )
    )).scalar_one()
    return str(mode or "").upper() == "UPI", int(renewals) + 2


async def _pre_debit_refused(db: AsyncSession, sub: Subscription, reply: dict, now: datetime) -> None:
    """PayU would not take the notice (an inactive or revoked mandate, a bank decline). Say so, once."""
    message = str(reply.get("msg") or reply.get("message") or reply.get("status") or "PayU refused the notice")[:300]
    logger.warning("pre-debit refused business=%s reply=%s", sub.business_id, reply)
    if sub.last_failure != message:
        sub.last_failure = message
        sub.updated_at = now
        await wallet._tell_owner(
            db, sub.business_id, "Your payment method needs attention",
            "We could not schedule the next KROVA payment - your bank mandate may have been cancelled. "
            "Set up payment again on the Billing page to keep your plan running.",
        )


async def _suspend(db: AsyncSession, sub: Subscription, reason: str, now: datetime) -> None:
    sub.status = "suspended"
    sub.next_charge_at = None
    sub.last_failure = reason[:300]
    sub.updated_at = now
    await wallet._tell_owner(
        db, sub.business_id, "Your KROVA plan is paused",
        "We could not collect the monthly payment. Add a payment method on the Billing page to continue.",
    )


async def _charge(db: AsyncSession, sub: Subscription, now: datetime) -> None:
    debit_on = sub.next_charge_at.astimezone(IST).date()  # the date the notice named, before it moves
    payment = Payment(
        business_id=sub.business_id, purpose="renewal", plan=sub.plan,
        base_paise=round(sub.amount_paise / (1 + plans.GST_PCT / 100)),
        gst_paise=sub.amount_paise - round(sub.amount_paise / (1 + plans.GST_PCT / 100)),
        fee_paise=0, total_paise=sub.amount_paise, txnid=new_txnid(),
    )
    db.add(payment)
    sub.attempts += 1
    sub.pre_debit_sent_for = None
    sub.next_charge_at = now + RETRY_GAP  # replaced on success; if PayU stays silent, retry then
    sub.updated_at = now
    await db.flush()
    reply = await payu.charge_mandate(
        authpayuid=sub.authpayuid, txnid=payment.txnid, amount_paise=sub.amount_paise,
        invoice=invoice_for(sub.business_id, debit_on),
    )
    detail = (reply.get("details") or {}).get(payment.txnid) or {}
    state = str(detail.get("status") or "").lower()
    if str(reply.get("status")) == "0" or state in FAILED_STATES or state == "":
        await _fail(db, payment, str(reply.get("msg") or detail.get("field9") or "Charge was not accepted")[:300], now)
    # captured / pending / in-progress: settle_payment confirms with PayU shortly.

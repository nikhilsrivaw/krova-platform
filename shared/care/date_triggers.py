"""
The time-based half of the automations engine - "a date is coming", not
"something just happened".

Every one of the 27 triggers that existed before this was an event: a
message arrived, a call ended, a signal was extracted. None of them could
express "three days before the payment is due", so every time this product
needed that, it grew another hardcoded sweep with our own timing baked in -
commitment_deadline_calls.py (24 hours, voice only, opt-in),
quotation_followup.py (3/7/14/21 days), cod_confirmation.py (4 hours),
scheduling/recall.py. Five sweeps, five sets of numbers chosen by us for
businesses we have never met.

Nikhil's direction was explicit: no pre-defined rules from us. A business
should set its own the way it would in n8n - the rule is not already there,
you build it. So this module fires three generic triggers carrying the one
thing a rule actually needs to decide for itself:

    commitment.due_soon   days_until, kind, direction, amount_paise, description
    commitment.overdue    days_overdue, kind, direction, amount_paise, description
    quotation.aging       days_open, status, amount_paise, reference
    customer.inactive     days_since_customer_message, days_since_any_message,
                          days_since_last_visit, has_upcoming_visit, stage

and the numeric operators the condition engine already has
(greater_than/less_than/equals/...) do the rest. "Three days before a
payment is due, message them" is `days_until equals 3` plus `kind equals
payment`. "Anything customers owe me more than a week late, make it a staff
task" is `days_overdue greater_than 7` plus `direction equals they_owe`.
Neither is a feature we built for them.

The older sweeps stay. They are opt-in, already relied on, and quietly
turning one off would break a business that is using it. But nothing new of
that shape should be written now that this exists - see
docs/new/automation-builder-audit.md.

## Why this runs once a day, and why that is the dedupe

There is no per-commitment "already fired today" marker anywhere here, on
purpose. The sweep runs on a daily cron, so each open object reaches
apply_rules exactly once per day, and a rule reading `days_until equals 3`
therefore fires exactly once in its life. A rule reading `days_until
less_than_or_equal 3` fires on day 3, day 2, day 1 and day 0 - four
messages - which looks like a bug and is not: it is what the business asked
for, in the operator it chose. The builder UI says so where the operator is
picked, which is the honest place to say it rather than silently
second-guessing the rule here.

Running this job more than once a day would break that property, so the
cadence is part of the contract, not a tuning knob (see
services/api/scheduler.py, where it is registered).

## Why "skipped" is not logged here

apply_rules is called with log_skips=False from every function below. For
all but one day of an object's life a time-based rule's condition is meant
to fail - that is how "days_until equals 3" works - so logging each of
those as "skipped" in AutomationRunLog would write thousands of non-events
a day and bury the few real runs. What did run (or failed, or was queued)
is still logged.

## What it deliberately does not do

No sending, no messaging, no acting. This module only ever fires triggers;
whether anything happens is entirely down to what rules a business built.
A business with no time-based rules gets a sweep that walks no rows at all
- which is the correct amount of opinion for us to have.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.care import post_call_actions
from shared.db.models import (
    OPEN_STATUSES,
    Appointment,
    AppointmentStatus,
    Commitment,
    CommitmentStatus,
    Customer,
    Direction,
    Message,
    PostCallActionRule,
    Quotation,
    WebhookEventType,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How far ahead a commitment can be and still fire commitment.due_soon.
# Not a rule about when to act - that is the business's condition - just a
# ceiling so a promise made for next March does not fire a trigger every
# single day between now and then. Generous enough that any realistic
# `days_until` a business would write still lands inside it.
_DUE_SOON_HORIZON_DAYS = 90

# The same idea on the other side: a commitment overdue for most of a year
# is not something an automation should still fire on daily, and a business
# chasing beyond this has a collections problem, not an automation problem.
_OVERDUE_HORIZON_DAYS = 180

# Quotations stop aging out loud at the same point, for the same reason.
_AGING_HORIZON_DAYS = 180

# A customer silent for longer than a year is not "gone quiet", they are
# gone; a win-back that late belongs in a campaign, not a daily trigger.
# Also what keeps this sweep's per-business work bounded.
_INACTIVE_HORIZON_DAYS = 365

# A visit that happened: marked visited, or still "confirmed" with its time
# already past - most businesses never go back and mark a visit as done,
# so waiting for "visited" alone would make days_since_last_visit None for
# nearly everyone. no_show and cancelled are not visits.
_PAST_VISIT_STATUSES = (AppointmentStatus.visited, AppointmentStatus.confirmed)
_UPCOMING_STATUSES = (AppointmentStatus.requested, AppointmentStatus.confirmed)

_TIME_TRIGGERS = (
    WebhookEventType.commitment_due_soon.value,
    WebhookEventType.commitment_overdue.value,
    WebhookEventType.quotation_aging.value,
    WebhookEventType.customer_inactive.value,
)


async def _businesses_with_time_rules(db: AsyncSession) -> dict[str, set[uuid.UUID]]:
    """
    Which businesses have an active rule on each time-based trigger.

    The whole sweep is skipped for everyone else. Without this it would
    walk every open commitment on the platform every night to fire
    triggers nobody subscribed to - apply_rules would return 0 each time,
    correctly and expensively.
    """
    rows = (
        await db.execute(
            select(PostCallActionRule.trigger_type, PostCallActionRule.business_id)
            .where(
                PostCallActionRule.trigger_type.in_(_TIME_TRIGGERS),
                PostCallActionRule.is_active.is_(True),
            )
            .distinct()
        )
    ).all()
    by_trigger: dict[str, set[uuid.UUID]] = {t: set() for t in _TIME_TRIGGERS}
    for trigger_type, business_id in rows:
        by_trigger[trigger_type].add(business_id)
    return by_trigger


def _whole_days(delta_seconds: float) -> int:
    """
    Days, floored - so "due in 71 hours" is 2 days away, not 2.96.

    Floor rather than round because a business writing `days_until equals
    3` means "there are still three days left", and rounding 2.6 up to 3
    would fire that rule on a commitment with barely two and a half days
    to go. Under-counting is the safe direction: the rule fires a little
    later, never a little early.
    """
    return int(delta_seconds // 86400)


async def fire_date_triggers(db: AsyncSession) -> int:
    """
    Fire the three time-based triggers for every open commitment and
    quotation belonging to a business that actually has a rule on them.
    Returns how many actions ran as a result.

    Commits nothing - the scheduler job owns the transaction, same as
    every other sweep here.
    """
    subscribed = await _businesses_with_time_rules(db)
    if not any(subscribed.values()):
        return 0

    now = datetime.now(timezone.utc)
    ran = 0
    ran += await _fire_commitments(db, now=now, subscribed=subscribed)
    ran += await _fire_quotations(
        db, now=now, businesses=subscribed[WebhookEventType.quotation_aging.value]
    )
    ran += await _fire_inactive(
        db, now=now, businesses=subscribed[WebhookEventType.customer_inactive.value]
    )
    return ran


async def _fire_commitments(
    db: AsyncSession, *, now: datetime, subscribed: dict[str, set[uuid.UUID]]
) -> int:
    due_soon_businesses = subscribed[WebhookEventType.commitment_due_soon.value]
    overdue_businesses = subscribed[WebhookEventType.commitment_overdue.value]
    interested = due_soon_businesses | overdue_businesses
    if not interested:
        return 0

    commitments = (
        await db.execute(
            select(Commitment).where(
                Commitment.business_id.in_(interested),
                Commitment.status == CommitmentStatus.open,
                Commitment.due_at.is_not(None),
            )
        )
    ).scalars().all()

    ran = 0
    for commitment in commitments:
        due_at = commitment.due_at
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)

        # Shared by both triggers - the enums go out as their own string
        # values (payment/delivery/callback, we_owe/they_owe) so a
        # business writes the condition in the same words the Ledger UI
        # already shows it, not an integer only we understand.
        base = {
            "kind": commitment.kind.value,
            "direction": commitment.direction.value,
            "amount_paise": commitment.amount_paise,
            "amount_outstanding_paise": commitment.outstanding_paise,
            "description": commitment.description,
        }

        delta = (due_at - now).total_seconds()
        if delta >= 0:
            if commitment.business_id not in due_soon_businesses:
                continue
            days_until = _whole_days(delta)
            if days_until > _DUE_SOON_HORIZON_DAYS:
                continue
            trigger = WebhookEventType.commitment_due_soon.value
            context = {**base, "days_until": days_until}
        else:
            if commitment.business_id not in overdue_businesses:
                continue
            days_overdue = _whole_days(-delta)
            if days_overdue > _OVERDUE_HORIZON_DAYS:
                continue
            trigger = WebhookEventType.commitment_overdue.value
            context = {**base, "days_overdue": days_overdue}

        try:
            ran += await post_call_actions.apply_rules(
                db,
                business_id=commitment.business_id,
                trigger_type=trigger,
                customer_id=commitment.customer_id,
                # No channel: this trigger did not come from one. A rule
                # that picked a specific channel is therefore skipped,
                # which is right - "only for WhatsApp conversations" is
                # not a thing a due date can be.
                channel=None,
                context=context,
                log_skips=False,
            )
        except Exception:
            logger.exception("date trigger %s failed for commitment=%s", trigger, commitment.id)

    return ran


async def _fire_quotations(db: AsyncSession, *, now: datetime, businesses: set[uuid.UUID]) -> int:
    if not businesses:
        return 0

    quotations = (
        await db.execute(
            select(Quotation).where(
                Quotation.business_id.in_(businesses),
                Quotation.status.in_(OPEN_STATUSES),
            )
        )
    ).scalars().all()

    ran = 0
    for quotation in quotations:
        # Age is measured from when it actually reached the customer, and
        # only falls back to created_at for a quote never marked sent - a
        # draft sitting in the pipeline is still worth chasing, but "sent
        # 9 days ago" is the number a business means by an aging quote.
        started = quotation.sent_at or quotation.created_at
        if started is None:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)

        days_open = _whole_days((now - started).total_seconds())
        if days_open < 0 or days_open > _AGING_HORIZON_DAYS:
            continue

        try:
            ran += await post_call_actions.apply_rules(
                db,
                business_id=quotation.business_id,
                trigger_type=WebhookEventType.quotation_aging.value,
                customer_id=quotation.customer_id,
                channel=None,
                context={
                    "days_open": days_open,
                    "status": quotation.status.value,
                    "amount_paise": quotation.total_paise,
                    "reference": quotation.reference,
                },
                log_skips=False,
            )
        except Exception:
            logger.exception("date trigger quotation.aging failed for quotation=%s", quotation.id)

    return ran


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def inactive_context(
    *,
    now: datetime,
    last_customer_message: datetime,
    last_any_message: datetime | None,
    last_visit: datetime | None,
    has_upcoming_visit: bool,
    stage: str | None,
) -> dict:
    """
    The customer.inactive trigger's context for one customer - pure, so
    the day arithmetic is testable without a database.

    days_since_last_visit is None, not a large number, for someone who has
    never visited: a condition like "days_since_last_visit greater_than 14"
    must not fire for a customer who only ever chatted. OPERATORS already
    treat None as failing every numeric comparison.
    """
    last_customer_message = _aware(last_customer_message)
    last_any_message = _aware(last_any_message) or last_customer_message
    last_visit = _aware(last_visit)
    return {
        "days_since_customer_message": _whole_days((now - last_customer_message).total_seconds()),
        "days_since_any_message": _whole_days((now - last_any_message).total_seconds()),
        "days_since_last_visit": (
            _whole_days((now - last_visit).total_seconds()) if last_visit is not None else None
        ),
        "has_upcoming_visit": has_upcoming_visit,
        "stage": stage,
    }


async def _fire_inactive(db: AsyncSession, *, now: datetime, businesses: set[uuid.UUID]) -> int:
    """
    Fire customer.inactive once for every customer, of a subscribed
    business, who has messaged at least once and not in the last day.

    "Quiet" is measured from the customer's own last message, not
    Customer.last_contact_at - that column also moves when the business
    writes to them (ingest and identity resolution both touch it), so a
    reminder nobody answered would reset the clock on exactly the customer
    this trigger exists to catch.

    Five grouped queries for the whole sweep, not per customer. The
    per-customer cost left is apply_rules itself, bounded by
    _INACTIVE_HORIZON_DAYS and by only running for businesses that have a
    rule on this trigger at all.
    """
    if not businesses:
        return 0

    horizon = now - timedelta(days=_INACTIVE_HORIZON_DAYS)

    last_in_rows = (
        await db.execute(
            select(Message.business_id, Message.customer_id, func.max(Message.occurred_at))
            .where(
                Message.business_id.in_(businesses),
                Message.direction == Direction.inbound,
            )
            .group_by(Message.business_id, Message.customer_id)
            .having(func.max(Message.occurred_at) >= horizon)
        )
    ).all()
    if not last_in_rows:
        return 0
    last_in = {(b, c): t for b, c, t in last_in_rows}

    last_any = {
        c: t for c, t in (
            await db.execute(
                select(Message.customer_id, func.max(Message.occurred_at))
                .where(Message.business_id.in_(businesses))
                .group_by(Message.customer_id)
            )
        ).all()
    }
    last_visit = {
        c: t for c, t in (
            await db.execute(
                select(Appointment.customer_id, func.max(Appointment.starts_at))
                .where(
                    Appointment.business_id.in_(businesses),
                    Appointment.starts_at <= now,
                    Appointment.status.in_(_PAST_VISIT_STATUSES),
                )
                .group_by(Appointment.customer_id)
            )
        ).all()
    }
    upcoming = set(
        (
            await db.execute(
                select(Appointment.customer_id)
                .where(
                    Appointment.business_id.in_(businesses),
                    Appointment.starts_at > now,
                    Appointment.status.in_(_UPCOMING_STATUSES),
                )
                .distinct()
            )
        ).scalars().all()
    )
    # Private customers are never acted on anywhere in this product - the
    # owner marked that thread personal (Customer.is_private).
    customers = {
        c: stage for c, stage in (
            await db.execute(
                select(Customer.id, Customer.stage).where(
                    Customer.business_id.in_(businesses),
                    Customer.is_private.is_(False),
                )
            )
        ).all()
    }

    ran = 0
    for (business_id, customer_id), last_message in last_in.items():
        if customer_id not in customers:
            continue
        context = inactive_context(
            now=now,
            last_customer_message=last_message,
            last_any_message=last_any.get(customer_id),
            last_visit=last_visit.get(customer_id),
            has_upcoming_visit=customer_id in upcoming,
            stage=customers[customer_id],
        )
        # Wrote today: not quiet, nothing to evaluate.
        if context["days_since_customer_message"] < 1:
            continue
        try:
            ran += await post_call_actions.apply_rules(
                db,
                business_id=business_id,
                trigger_type=WebhookEventType.customer_inactive.value,
                customer_id=customer_id,
                channel=None,
                context=context,
                log_skips=False,
            )
        except Exception:
            logger.exception("date trigger customer.inactive failed for customer=%s", customer_id)

    return ran

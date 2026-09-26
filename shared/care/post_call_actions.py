"""
The trigger-to-action bridge - interprets PostCallActionRule rows.

No rules engine, or anything like it, exists anywhere else in this
codebase - every other conditional action here is a hardcoded Python
function wired as one fixed scheduler job (cod_call_failsafe.py's
WhatsApp-unanswered -> call -> escalate chain is the closest analog).
This is genuinely new infrastructure: a business picks a trigger and an
action themselves, rather than a developer hardcoding the pairing.

Named and tabled for its original scope (started as the voice-to-action
bridge, "when a call ends this way, do that") but the mechanism was
already generic - trigger_type is an arbitrary string, never FK-
constrained to call.* specifically. Now called from every place a
WebhookEventType already fires with a resolvable customer (ingest.py's
message.received, booking.py's appointment events, the flow-completion
path in webhooks.py, ...), not only the two call-outcome hook points.
The table keeps its original name (shared/db/models/integrations.py's
PostCallActionRule) rather than a rename migration for a naming-only
gain - see services/api/routers/post_call_rules.py's own _VALID_TRIGGERS
for the current, cross-channel list.

A rule can hold several ordered AutomationStep rows (see that model's own
docstring) - each with its own optional condition and optional delay, no
branching between them (a condition gates one step, it never halts the
ones after it). A business-configurable visual canvas UI is a later round
(Nikhil's own direction); this stays a step list, deliberately not an
open-ended action language (arbitrary webhooks, arbitrary email
templates). Adding a new action type is a new `if` in _run_step_action,
not a schema change - action_config is already free-form JSON per step.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AutomationRunLog,
    AutomationStep,
    AutomationStepRun,
    Business,
    Customer,
    PostCallActionRule,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# The real, honest fields available to a condition for each trigger_type -
# built from what that trigger's own dispatch site already has in hand
# (see apply_rules' own callers), never an open query language over
# arbitrary columns. The API layer (services/api/routers/post_call_rules.py)
# validates a condition's `field` against this same allowlist per
# trigger_type, so a rule can never be saved referencing data that trigger
# will never actually carry.
CONDITION_FIELDS: dict[str, tuple[str, ...]] = {
    "call.completed": ("duration_seconds", "outcome", "sentiment", "escalated", "topic", "requested_service"),
    "call.voicemail": ("campaign_objective",),
    "call.no_answer": ("campaign_objective",),
    "message.received": ("text",),
    "comment.received": ("text",),
    "story_reply.received": ("text",),
    "flow.completed": ("flow_id",),
    "appointment.booked": ("starts_at", "intake_channel"),
    "appointment.cancelled": ("starts_at", "intake_channel", "reason"),
    "appointment.rescheduled": ("starts_at", "intake_channel"),
    # category deliberately NOT a condition field here - it's stamped by a
    # cold-path sweep (shared/care/escalation_failsafe.py::
    # categorize_new_escalations) AFTER notify_escalation()'s own
    # real-time apply_rules() call already fired with context={"reason":
    # reason} - category is never actually in scope at dispatch time, so
    # a condition on it could never match anything. Exposed for display
    # (the /escalations card badge) and, once true dispatch-time timing
    # allows it, a real future condition field - not this round.
    "escalation.raised": ("reason",),
    "queue_token.issued": ("shift", "queue_number"),
    "competitor.mentioned": ("severity", "title", "body"),
    "churn_risk.detected": ("severity", "title", "body"),
    "demo.requested": ("severity", "title", "body"),
    "pricing_question.asked": ("severity", "title", "body"),
    "bug.detected": ("severity", "title", "body"),
    "feature_request.detected": ("severity", "title", "body"),
    "complaint.detected": ("severity", "title", "body"),
    "praise.detected": ("severity", "title", "body"),
    "overdue_followup.detected": ("severity", "title", "body"),
    "report_not_collected.detected": ("severity", "title", "body"),
    "overdue_refund.detected": ("severity", "title", "body"),
    "callback_overdue.detected": ("severity", "title", "body"),
    "intent_leakage.detected": ("severity", "title", "body"),
    "rto_risk.detected": ("severity", "title", "body"),
    "claim.status_changed": ("severity", "title", "body"),
    # The time-based triggers (shared/care/date_triggers.py). These are
    # the ones a condition is genuinely *required* on to be useful - every
    # other trigger here is already specific ("a complaint was detected"),
    # while "a commitment is due" fires for every open commitment every
    # day. days_until / days_overdue / days_open with the numeric
    # operators that already exist is how a business sets its own timing
    # instead of inheriting ours. `kind` and `direction` are the
    # Commitment enums' own string values (payment/delivery/callback/...,
    # we_owe/they_owe), so "chase only money customers owe me" is
    # two conditions on two steps, not a feature we have to build.
    # amount_outstanding_paise is what is still owed after any part-
    # payment, so "chase only if more than ₹500 is left" is a condition.
    "commitment.due_soon": (
        "days_until", "kind", "direction", "amount_paise", "amount_outstanding_paise", "description",
    ),
    "commitment.overdue": (
        "days_overdue", "kind", "direction", "amount_paise", "amount_outstanding_paise", "description",
    ),
    "quotation.aging": ("days_open", "status", "amount_paise", "reference"),
    # days_since_customer_message counts only what the CUSTOMER sent -
    # Customer.last_contact_at is not used because it also moves when the
    # business messages them, so a reminder nobody answered would make a
    # silent customer look active. days_since_last_visit is None for a
    # customer with no past booking, and a numeric condition on None
    # fails closed (OPERATORS), so "no visit in 14 days" never fires for
    # someone who never booked at all. has_upcoming_visit lets a rule skip
    # anyone who already has a booking coming. stage is the business's
    # own pipeline word, so "only members, not leads" is a condition.
    # None is a real value here - a customer with no stage yet, or one
    # whose stage was cleared - so "to_stage equals Joined" and
    # "from_stage equals Lead" both behave as a business would expect.
    "customer.stage_changed": ("from_stage", "to_stage"),
    "customer.inactive": (
        "days_since_customer_message", "days_since_any_message",
        "days_since_last_visit", "has_upcoming_visit", "stage",
    ),
    # escalation_rate.detected / account_health.detected deliberately
    # absent - business-level Insight kinds with no customer_id, so they
    # never reach apply_rules at all (see shared/care/signal_dispatch.py).
    # A condition field here would gate a step that can never run.
}

# Deliberately this short list, not an expression language - a condition is
# {"field": ..., "operator": ..., "value": ...}, one comparison, no AND/OR
# grouping. Research into how Zapier/n8n/Make model this found real SMB
# usage rarely goes past a single filter condition; nested boolean groups
# are the exception, not the common case, and can be added later against
# real demand rather than guessed at now.
# Public - services/api/routers/post_call_rules.py validates a new
# condition's operator against this same set before it's ever saved.
OPERATORS = {
    "equals": lambda a, b: a == b,
    "not_equals": lambda a, b: a != b,
    "contains": lambda a, b: isinstance(a, str) and isinstance(b, str) and b.lower() in a.lower(),
    "greater_than": lambda a, b: a is not None and a > b,
    "less_than": lambda a, b: a is not None and a < b,
    "greater_than_or_equal": lambda a, b: a is not None and a >= b,
    "less_than_or_equal": lambda a, b: a is not None and a <= b,
}


# A ceiling, not a discovered limit - enough for "days_until equals 3 and
# kind equals payment and direction equals they_owe and amount over X"
# with room to spare. Past this a rule is usually two rules pretending to
# be one. The API validates against it (post_call_rules.py).
MAX_CONDITIONS_PER_STEP = 5


def normalize_conditions(condition: dict | list | None) -> list[dict]:
    """
    Every stored shape of a step's condition, as a plain list.

    None/empty -> [] (always runs). A bare {"field", ...} dict -> [dict] -
    the only shape that existed before a step could hold more than one,
    and still what an AutomationStepRun queued before this change carries
    in its snapshot. A list -> itself. So old rules, old queued chains and
    new rules all run through the one evaluator below with no migration.
    """
    if not condition:
        return []
    if isinstance(condition, dict):
        return [condition]
    return [c for c in condition if isinstance(c, dict)]


def _evaluate_one(condition: dict, context: dict) -> tuple[bool, str | None]:
    field = condition.get("field")
    operator = condition.get("operator")
    value = condition.get("value")
    if field not in context:
        logger.warning(
            "condition field %r not available in this trigger's context %r - skipping step",
            field, sorted(context.keys()),
        )
        return False, (
            f"{field!r} was not available on this trigger "
            f"(it carries: {', '.join(sorted(context.keys())) or 'nothing'})"
        )
    op_fn = OPERATORS.get(operator or "")
    if op_fn is None:
        logger.warning("unknown condition operator %r - skipping step", operator)
        return False, f"unknown operator {operator!r}"
    try:
        held = bool(op_fn(context[field], value))
    except TypeError:
        logger.warning(
            "condition %r could not be evaluated against %r - skipping step", condition, context.get(field),
        )
        return False, f"{field} could not be compared to {value!r} (actual: {context.get(field)!r})"
    if held:
        return True, None
    return False, f"{field} {operator} {value!r} - actual value was {context[field]!r}"


def _evaluate_condition(condition: dict | list | None, context: dict) -> tuple[bool, str | None]:
    """
    Whether a step's conditions ALL hold against this trigger's real
    context, and - when they don't - a plain sentence saying which one
    failed and why, in the business's own terms.

    No conditions always holds, preserving the original unconditional
    behaviour. A condition naming a field that isn't actually in
    `context`, or an operator this codebase doesn't know, fails closed
    (the step is skipped, logged) rather than running on data that was
    never really checked - the same "never silently do more than was
    verified" instinct as the agent's own escalate-rather-than-guess rule.

    Stops at the first failure and names it. With several conditions the
    owner needs to know *which* one blocked the step, not merely that one
    did - "2 of 3 conditions failed" answers nothing. The reason string
    feeds AutomationRunLog.detail and the builder's own test panel.
    """
    conditions = normalize_conditions(condition)
    for i, one in enumerate(conditions):
        held, reason = _evaluate_one(one, context)
        if not held:
            if len(conditions) > 1:
                reason = f"condition {i + 1} of {len(conditions)}: {reason}"
            return False, reason
    return True, None


def _snapshot_step(step: AutomationStep) -> dict:
    """The plain-dict shape a chain is built from and resumed from - see
    AutomationStepRun.remaining_steps' own docstring for why a resumed
    chain never re-reads the live AutomationStep rows."""
    return {
        "position": step.position,
        "action_type": step.action_type,
        "action_config": step.action_config or {},
        "condition": step.condition,
        "delay_seconds": step.delay_seconds,
    }


async def apply_rules(
    db: AsyncSession, *, business_id: uuid.UUID, trigger_type: str, customer_id: uuid.UUID | None,
    call_id: uuid.UUID | None = None, channel: str | None = None, context: dict | None = None,
    log_skips: bool = True,
) -> int:
    """
    Run every active rule matching this trigger for this business. Returns
    how many actions ran synchronously (a step queued for later via a
    delay is not counted here - see run_due_steps).

    `channel` is which real channel this trigger fired from (voice,
    whatsapp, instagram, email, web) - every dispatch site now has a real
    value to pass, since it always knows. A rule with no `channel` set
    fires regardless (today's original, unfiltered behaviour); a rule that
    picked a specific channel is skipped when this trigger came from
    somewhere else. Without this, message.received - which fires
    identically for a WhatsApp message, an Instagram DM, and every single
    utterance on a live voice call - could fire a WhatsApp-authored rule
    mid-phone-call.

    `context` is this trigger's own real data, keyed to match
    CONDITION_FIELDS[trigger_type] - what a step's condition is actually
    checked against. None (the default) means no dispatch site has wired
    one yet for this trigger_type; every step's condition then simply
    fails closed (see _condition_holds), same as a step whose condition
    names a field genuinely missing from what was passed.

    `log_skips=False` stops a step whose condition didn't hold from being
    written to AutomationRunLog. Only the daily time-based sweep
    (date_triggers.py) passes it: it fires every rule for every open
    commitment and every quiet customer, every day, and for all but one of
    those days the condition is *supposed* to fail. Logging each of those
    as "skipped" would bury the handful of real runs under thousands of
    non-events, which defeats the whole point of the log. For an event
    trigger a skip is real information ("a complaint came in, and your
    condition didn't match it"), so it stays logged there.

    Runs each rule's steps in position order via _run_chain - a rule can
    hold more than one step, each independently gated by its own
    condition and delay (no branching, no step depends on a previous
    step's outcome - see shared/db/models/integrations.py::AutomationStep's
    own docstring for why that's deliberate, not a gap).
    """
    if customer_id is None:
        # Every action type today needs a customer (a WhatsApp send, an
        # Escalation row tied to who the call was about) - nothing to do
        # for a call with no resolved customer at all.
        return 0

    result = await db.execute(
        select(PostCallActionRule).where(
            PostCallActionRule.business_id == business_id,
            PostCallActionRule.trigger_type == trigger_type,
            PostCallActionRule.is_active.is_(True),
        )
    )
    rules = [
        r for r in result.scalars().all()
        if not r.channel or r.channel == channel
    ]
    if not rules:
        return 0

    customer = await db.get(Customer, customer_id)
    if customer is None:
        return 0

    business = await db.get(Business, business_id)
    if business is None:
        return 0

    context = context or {}

    steps_by_rule: dict[uuid.UUID, list[dict]] = {}
    steps_result = await db.execute(
        select(AutomationStep)
        .where(AutomationStep.rule_id.in_([r.id for r in rules]))
        .order_by(AutomationStep.position)
    )
    for step in steps_result.scalars().all():
        steps_by_rule.setdefault(step.rule_id, []).append(_snapshot_step(step))

    ran = 0
    for rule in rules:
        steps = steps_by_rule.get(rule.id, [])
        if not steps:
            continue
        ran += await _run_chain(
            db, business=business, customer=customer, business_id=business_id, customer_id=customer_id,
            call_id=call_id, channel=channel, trigger_type=trigger_type, context=context,
            rule_id=rule.id, steps=steps, start_index=0, log_ref=str(rule.id),
            log_skips=log_skips,
        )

    return ran


async def _run_chain(
    db: AsyncSession, *, business: Business, customer: Customer, business_id: uuid.UUID, customer_id: uuid.UUID,
    call_id: uuid.UUID | None, channel: str | None, trigger_type: str, context: dict,
    rule_id: uuid.UUID, steps: list[dict], start_index: int, log_ref: str,
    log_skips: bool = True,
) -> int:
    """
    Runs a rule's steps starting at steps[start_index], in position order,
    until either the chain runs out or a step with its own delay pauses it
    - queuing an AutomationStepRun that resumes exactly here once that
    delay elapses (see run_due_steps). A step whose condition doesn't hold
    is skipped, not treated as ending the chain - the rest still runs, per
    this engine's own no-branching design (a condition gates one step, it
    never halts the ones after it).

    Returns how many actions actually ran synchronously in this call.
    """
    ran = 0
    for i in range(start_index, len(steps)):
        step = steps[i]
        # A step snapshotted before AutomationStep.position was carried
        # through (an AutomationStepRun queued by an older build) falls
        # back to its index in the chain - close enough to line the log
        # row up with the builder, and never a reason to drop the log.
        position = step.get("position", i)
        held, reason = _evaluate_condition(step["condition"], context)
        if not held:
            if not log_skips:
                continue
            _log_run(
                db, rule_id=rule_id, business_id=business_id, customer_id=customer_id,
                trigger_type=trigger_type, position=position, action_type=step["action_type"],
                status="skipped", detail=f"condition not met: {reason}", context=context,
            )
            continue

        if step["delay_seconds"]:
            # remaining_steps starts at this step (not i+1) - it's the one
            # whose delay just got queued, and run_due_steps runs it first
            # (unconditionally - its condition already passed, right above)
            # before resuming the chain at i+1 with its own fresh checks.
            db.add(AutomationStepRun(
                rule_id=rule_id, business_id=business_id, customer_id=customer_id,
                call_id=call_id, channel=channel, trigger_type=trigger_type, context=context,
                remaining_steps=steps[i:],
                due_at=datetime.now(timezone.utc) + timedelta(seconds=step["delay_seconds"]),
            ))
            _log_run(
                db, rule_id=rule_id, business_id=business_id, customer_id=customer_id,
                trigger_type=trigger_type, position=position, action_type=step["action_type"],
                status="queued", detail=_describe_delay(step["delay_seconds"]), context=context,
            )
            return ran

        try:
            did = await _run_step_action(
                db, business=business, customer=customer, business_id=business_id, customer_id=customer_id,
                call_id=call_id, channel=channel, trigger_type=trigger_type, context=context,
                action_type=step["action_type"], action_config=step["action_config"],
                log_ref=f"{log_ref}[{i}]",
            )
        except NoRecipient as exc:
            _log_run(
                db, rule_id=rule_id, business_id=business_id, customer_id=customer_id,
                trigger_type=trigger_type, position=position, action_type=step["action_type"],
                status="no_action", detail=str(exc), context=context,
            )
            continue
        except Exception as exc:
            logger.exception("automation rule=%s step[%s] failed to apply", rule_id, i)
            _log_run(
                db, rule_id=rule_id, business_id=business_id, customer_id=customer_id,
                trigger_type=trigger_type, position=position, action_type=step["action_type"],
                status="failed", detail=str(exc) or exc.__class__.__name__, context=context,
            )
            continue

        if did:
            ran += 1
        _log_run(
            db, rule_id=rule_id, business_id=business_id, customer_id=customer_id,
            trigger_type=trigger_type, position=position, action_type=step["action_type"],
            status="ran" if did else "no_action",
            detail=None if did else _NO_ACTION_DETAIL.get(step["action_type"], _NO_ACTION_DEFAULT),
            context=context,
        )

    return ran


# Why a step that ran but did nothing - _run_step_action returning False -
# is a real, expected outcome rather than a failure, per action type. The
# owner reading the history needs the actual reason ("this customer has no
# phone number on file"), not "no action", which just restates the status.
_NO_ACTION_DEFAULT = "nothing to do - the prerequisites for this action were not met"
_NO_ACTION_DETAIL = {
    "whatsapp_followup": "no WhatsApp number on this customer, or WhatsApp is not connected",
    "instagram_followup": "this customer has no Instagram identity, or Instagram is not connected",
    "instagram_comment_reply": "this trigger carried no Instagram comment to reply to",
    "place_call": "no phone number on this customer, or voice is not provisioned",
    "send_sms": "no phone number on this customer, or SMS is not configured",
    "send_email": "no email address on this customer, or no email sender is connected",
    "send_flow": "the flow is missing, not published, or the customer has no WhatsApp number",
    "add_tag": "this customer already carries that tag",
}


def _describe_delay(seconds: int) -> str:
    """"waiting 2 hours" - the delay in the units the builder shows it in."""
    if seconds % 86400 == 0:
        n = seconds // 86400
        return f"waiting {n} day{'s' if n != 1 else ''}"
    if seconds % 3600 == 0:
        n = seconds // 3600
        return f"waiting {n} hour{'s' if n != 1 else ''}"
    n = max(1, seconds // 60)
    return f"waiting {n} minute{'s' if n != 1 else ''}"


def _log_run(
    db: AsyncSession, *, rule_id: uuid.UUID, business_id: uuid.UUID, customer_id: uuid.UUID | None,
    trigger_type: str, position: int, action_type: str, status: str, detail: str | None, context: dict,
) -> None:
    """
    Record one step outcome. Added to the session, never committed here -
    the caller's own transaction owns it, same as every other write in
    this module.

    Deliberately not wrapped in a try/except: a failure to write the log
    should surface, not be swallowed into the silence this table exists to
    end.
    """
    db.add(AutomationRunLog(
        rule_id=rule_id, business_id=business_id, customer_id=customer_id,
        trigger_type=trigger_type, step_position=position, action_type=action_type,
        status=status, detail=detail, context=context or {},
    ))


async def run_due_steps(db: AsyncSession) -> int:
    """
    Resume every AutomationStepRun whose delay has elapsed. Returns how
    many actions actually ran (a skip for a real reason - customer/
    business gone, missing prerequisites - still counts as processed, not
    as ran; a step further down the chain hitting its own delay pauses it
    again rather than counting as done).

    Same one-shot sweep shape as shared/care/escalation_failsafe.py and
    cod_call_failsafe.py: `executed_at` is stamped the moment a row is
    picked up, before the action itself runs, so a crash mid-action never
    causes it to fire twice on the next sweep.
    """
    now = datetime.now(timezone.utc)
    due = (
        await db.execute(
            select(AutomationStepRun).where(
                AutomationStepRun.due_at <= now, AutomationStepRun.executed_at.is_(None),
            )
        )
    ).scalars().all()
    if not due:
        return 0

    ran = 0
    for run in due:
        run.executed_at = now

        steps = run.remaining_steps or []
        if not steps:
            continue

        business = await db.get(Business, run.business_id)
        customer = await db.get(Customer, run.customer_id)
        if business is None or customer is None:
            logger.info("automation_step_run=%s skipped - business or customer no longer exists", run.id)
            continue

        # steps[0] is the one this row was waiting on - its condition
        # already passed once, before it was queued (see _run_chain), so
        # it runs unconditionally here rather than being re-checked.
        first = steps[0]
        first_position = first.get("position", 0)
        try:
            did = await _run_step_action(
                db, business=business, customer=customer, business_id=run.business_id, customer_id=run.customer_id,
                call_id=run.call_id, channel=run.channel, trigger_type=run.trigger_type, context=run.context or {},
                action_type=first["action_type"], action_config=first["action_config"],
                log_ref=f"{run.rule_id}[resume]",
            )
        except NoRecipient as exc:
            _log_run(
                db, rule_id=run.rule_id, business_id=run.business_id, customer_id=run.customer_id,
                trigger_type=run.trigger_type, position=first_position, action_type=first["action_type"],
                status="no_action", detail=str(exc), context=run.context or {},
            )
        except Exception as exc:
            logger.exception("automation_step_run=%s failed to apply", run.id)
            _log_run(
                db, rule_id=run.rule_id, business_id=run.business_id, customer_id=run.customer_id,
                trigger_type=run.trigger_type, position=first_position, action_type=first["action_type"],
                status="failed", detail=str(exc) or exc.__class__.__name__, context=run.context or {},
            )
        else:
            if did:
                ran += 1
            _log_run(
                db, rule_id=run.rule_id, business_id=run.business_id, customer_id=run.customer_id,
                trigger_type=run.trigger_type, position=first_position, action_type=first["action_type"],
                status="ran" if did else "no_action",
                detail=None if did else _NO_ACTION_DETAIL.get(first["action_type"], _NO_ACTION_DEFAULT),
                context=run.context or {},
            )

        # Resume the rest of the chain (if any) - each of these still gets
        # its own condition/delay check, same as a fresh trigger would.
        ran += await _run_chain(
            db, business=business, customer=customer, business_id=run.business_id, customer_id=run.customer_id,
            call_id=run.call_id, channel=run.channel, trigger_type=run.trigger_type, context=run.context or {},
            rule_id=run.rule_id, steps=steps, start_index=1, log_ref=f"{run.rule_id}[resume]",
        )

    return ran


# How long a business can look back at what its automations did. Long
# enough to cover "it didn't fire last month either" and short enough that
# a busy account's log never becomes the biggest table in the database.
# Not a discovered number - the same order as every other retention window
# a tool like this ships with, and the one knob to turn if it proves wrong.
RUN_LOG_RETENTION_DAYS = 60


async def prune_run_logs(db: AsyncSession) -> int:
    """
    Delete AutomationRunLog rows past the retention window. Returns how
    many went.

    This is the cost control that lets the log be written unconditionally
    rather than behind a per-business setting - see AutomationRunLog's own
    docstring for why a log you have to switch on is worthless.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=RUN_LOG_RETENTION_DAYS)
    result = await db.execute(delete(AutomationRunLog).where(AutomationRunLog.created_at < cutoff))
    await db.commit()
    return result.rowcount or 0


async def _run_step_action(
    db: AsyncSession, *, business: Business, customer: Customer, business_id: uuid.UUID, customer_id: uuid.UUID,
    call_id: uuid.UUID | None, channel: str | None, trigger_type: str, context: dict, action_type: str,
    action_config: dict, log_ref: str,
) -> bool:
    """
    Runs one already-resolved action. Shared by apply_rules (immediate
    steps) and run_due_steps (delayed steps) - the actual side effect is
    identical either way, only when it happens differs.

    `action_config["send_to"] == "payer"` redirects a messaging step to
    whoever pays for this customer (Customer.paid_by_customer_id) - "fees
    due → WhatsApp the parent". The payer's own name, phone and channels
    are used; the original customer's name is available to the message as
    {{participant_name}}. With no payer linked the step does nothing and
    says so (NoRecipient), rather than quietly messaging the customer: a
    fee reminder meant for a parent must never land on the child instead.
    """
    if (action_config or {}).get("send_to") == "payer":
        if action_type not in PAYER_ACTIONS:
            raise NoRecipient(f"{action_type} can't be sent to a payer")
        payer = (
            await db.get(Customer, customer.paid_by_customer_id)
            if customer.paid_by_customer_id else None
        )
        if payer is None or payer.business_id != business_id:
            raise NoRecipient("no payer is linked to this customer")
        if payer.is_private:
            raise NoRecipient("the linked payer is marked private")
        context = {**context, "participant_name": customer.display_name or ""}
        customer, customer_id = payer, payer.id
    if action_type == "whatsapp_followup":
        from shared.scheduling import notify

        message = (action_config or {}).get("message") or ""
        if not message:
            logger.warning("automation_step=%s has no message configured, skipping", log_ref)
            return False
        message = _resolve_tokens(message, context).strip()
        if not message:
            logger.warning(
                "automation_step=%s left empty after resolving its tokens, skipping", log_ref,
            )
            return False
        return await notify.send_post_call_followup(db, business=business, customer=customer, message=message)

    if action_type == "create_escalation_task":
        from shared.ai import agent as agent_module

        reason = (action_config or {}).get("reason") or f"Post-call follow-up needed ({trigger_type})"
        # via_automation=True: this escalation was itself raised by a rule,
        # so it must not re-enter apply_rules for escalation.raised - a
        # rule mapping that trigger back to create_escalation_task would
        # otherwise cascade forever.
        #
        # channel or "voice": real value when the caller knows it (every
        # dispatch site does, as of this session's channel-filter work) -
        # "voice" only as a last resort for a call site that predates it
        # and still passes none, not a claim this always came from voice.
        await agent_module.notify_escalation(
            business_id, reason=reason, customer_id=customer_id, channel=channel or "voice", db=db,
            via_automation=True,
        )
        return True

    if action_type == "add_tag":
        return await _add_tag(business_id, customer_id, action_config or {}, db)

    if action_type == "instagram_followup":
        message = (action_config or {}).get("message") or ""
        message = _resolve_tokens(message, context).strip()
        return await _send_instagram_followup(business_id, customer_id, {"message": message}, db)

    if action_type == "instagram_comment_reply":
        message = (action_config or {}).get("message") or ""
        message = _resolve_tokens(message, context).strip()
        return await _send_instagram_comment_reply(
            business_id, customer_id, {"message": message}, context, db,
        )

    if action_type == "send_flow":
        return await _send_flow(business_id, customer_id, action_config or {}, db, context=context, call_id=call_id)

    if action_type == "place_call":
        return await _place_call(business_id, customer_id, action_config or {}, db)

    if action_type == "send_sms":
        return await _send_sms(business_id, customer_id, action_config or {}, db)

    if action_type == "send_email":
        return await _send_email(business_id, customer_id, action_config or {}, db)

    logger.warning("automation_step=%s has unrecognised action_type=%s", log_ref, action_type)
    return False


# The actions that reach a person on a channel, and so can be pointed at
# the payer. add_tag and create_escalation_task are about the customer
# themselves, and an Instagram comment reply belongs to whoever commented.
PAYER_ACTIONS = frozenset({"whatsapp_followup", "send_sms", "send_email", "place_call", "send_flow"})


class NoRecipient(Exception):
    """A step that has nobody to send to - logged as no_action with this
    message as the reason, not as a failure."""


def _resolve_tokens(text: str, context: dict) -> str:
    """
    Substitutes any {{key}} token in `text` with context.get(key) - the
    same `context` dict a step's own condition is already checked against
    (shared/care/post_call_actions.py's own CONDITION_FIELDS names what's
    real per trigger_type). A key genuinely missing from context, or set
    to a falsy value (e.g. requested_service unset), resolves to blank
    rather than sending a business the literal unresolved token.

    Generalizes what was previously whatsapp_followup's own one-token,
    {{summary}}-only substitution (backed by its own extra Call row
    lookup) into something any action's text/data fields can use for free
    - {{summary}} keeps working exactly as before because relay.py's own
    call.completed dispatch already folds Call.summary into context.
    """
    def _sub(match: re.Match) -> str:
        value = context.get(match.group(1))
        return str(value) if value else ""

    return re.sub(r"\{\{(\w+)\}\}", _sub, text)


async def _place_call(business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession) -> bool:
    """
    Ring the customer - reuses shared/channels/voice/outbound.py's own
    place_adhoc_call verbatim (the same function commitment_deadline_calls.py
    already uses for proactive reminders), rather than a second connection/
    subaccount lookup for the same call. That function already logs its
    own skip reasons (no phone on file, no voice number connected) and
    returns False rather than raising for any of them.
    """
    reason = str(config.get("reason") or "").strip()
    if not reason:
        logger.warning("place_call rule for business=%s has no reason configured, skipping", business_id)
        return False

    from shared.channels.voice import outbound

    return await outbound.place_adhoc_call(business_id, customer_id, reason, db)


async def _send_sms(business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession) -> bool:
    """
    Same connection/credential resolution as shared/care/
    escalation_failsafe.py's own SMS send, verbatim - ChannelConnection.
    access_token decrypted as the subaccount auth_token, connection.extra
    ["subaccount_auth_id"] as the auth_id, connection.external_account_id
    as the from_number - not a second lookup path for the same three
    fields. Sends to the customer's own phone identity, unlike the
    failsafe (which sends to the business's staff_phone_number instead).
    """
    message = str(config.get("message") or "").strip()
    if not message:
        logger.warning("send_sms rule for business=%s has no message configured, skipping", business_id)
        return False

    from shared.auth.encryption import decrypt
    from shared.channels.voice import plivo_client
    from shared.db.models import Channel, ChannelConnection, ConnectionStatus, CustomerIdentity, IdentityKind

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.voice,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        logger.info("send_sms rule skipped business=%s: no voice number connected", business_id)
        return False

    auth_id = (connection.extra or {}).get("subaccount_auth_id")
    if not auth_id:
        logger.warning("send_sms rule skipped business=%s: voice connection missing subaccount id", business_id)
        return False

    to_number = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.phone,
            )
        )
    ).scalars().first()
    if to_number is None:
        logger.info("send_sms rule skipped business=%s customer=%s: no phone on file", business_id, customer_id)
        return False

    try:
        await plivo_client.send_sms(
            auth_id=auth_id, auth_token=decrypt(connection.access_token),
            from_number=connection.external_account_id, to_number=to_number, text=message,
        )
    except plivo_client.PlivoError:
        logger.exception("send_sms rule failed business=%s customer=%s", business_id, customer_id)
        return False
    return True


async def _send_instagram_followup(business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession) -> bool:
    """
    A plain Instagram DM to the customer's own IGSID on file - the
    keyword-triggered "reply to a greeting" case this action exists for,
    not a private comment-reply (that stays send_draft.py's job, since it
    needs the source comment_id a rule's own trigger context doesn't
    carry). Same "resolve connection, resolve identity, send, ingest"
    shape as _send_sms/_send_email above - not a second lookup path.

    Free-text, unlike WhatsApp's whatsapp_followup: Instagram has no
    approved-template requirement for messaging inside an open
    conversation window, so this sends the configured message as-is.
    """
    message = str(config.get("message") or "").strip()
    if not message:
        logger.warning("instagram_followup rule for business=%s has no message configured, skipping", business_id)
        return False

    from shared.auth.encryption import decrypt
    from shared.channels import ingest
    from shared.channels.instagram.client import InstagramClient, InstagramSendError
    from shared.db.models import Channel, ChannelConnection, ConnectionStatus, CustomerIdentity, Direction, IdentityKind

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.instagram,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        logger.info("instagram_followup rule skipped business=%s: no Instagram connection", business_id)
        return False

    to = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.instagram,
            )
        )
    ).scalars().first()
    if not to:
        logger.info("instagram_followup rule skipped business=%s customer=%s: no Instagram id on file", business_id, customer_id)
        return False

    client = InstagramClient.for_connection(connection)
    try:
        sent = await client.send_text(to, message)
    except InstagramSendError:
        logger.exception("instagram_followup rule failed business=%s customer=%s", business_id, customer_id)
        return False

    from datetime import datetime, timezone

    await ingest.ingest(
        business_id=business_id, channel=Channel.instagram, direction=Direction.outbound,
        identity_kind=IdentityKind.instagram, identity_value=to,
        external_id=sent.external_id or None, text=message, occurred_at=datetime.now(timezone.utc),
        connection_id=connection.id, enqueue_analysis=False, db=db,
    )
    return True


async def _send_instagram_comment_reply(
    business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, context: dict, db: AsyncSession,
) -> bool:
    """
    A private reply to the comment that triggered this rule - the gap
    _send_instagram_followup's own docstring already named: a keyword
    rule on comment.received needs the source comment_id, which only
    that trigger's context carries (see ingest.py's comment.received
    dispatch). Same Graph contract as send_draft.py's own private-reply
    path (comment_id, not the commenter's IGSID) - one 7-day-window,
    once-per-comment reply, enforced by Meta, not re-checked here.
    """
    message = str(config.get("message") or "").strip()
    if not message:
        logger.warning("instagram_comment_reply rule for business=%s has no message configured, skipping", business_id)
        return False

    comment_id = context.get("comment_id")
    if not comment_id:
        logger.warning(
            "instagram_comment_reply rule for business=%s has no comment_id in context, skipping", business_id,
        )
        return False

    from shared.channels import ingest
    from shared.channels.instagram.client import InstagramClient, InstagramSendError
    from shared.db.models import Channel, ChannelConnection, ConnectionStatus, CustomerIdentity, Direction, IdentityKind

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.instagram,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        logger.info("instagram_comment_reply rule skipped business=%s: no Instagram connection", business_id)
        return False

    to = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.instagram,
            )
        )
    ).scalars().first()

    client = InstagramClient.for_connection(connection)
    try:
        sent = await client.send_private_reply(comment_id, message)
    except InstagramSendError:
        logger.exception(
            "instagram_comment_reply rule failed business=%s customer=%s comment=%s",
            business_id, customer_id, comment_id,
        )
        return False

    if to:
        # Attributable to the customer's own identity when we have one on
        # file; a private reply to a first-time commenter with no prior
        # DM still sent successfully above even when this lookup is
        # empty - not attributing it to a timeline is not the same as
        # the send having failed.
        from datetime import datetime, timezone

        await ingest.ingest(
            business_id=business_id, channel=Channel.instagram, direction=Direction.outbound,
            identity_kind=IdentityKind.instagram, identity_value=to,
            external_id=sent.external_id or None, text=message, occurred_at=datetime.now(timezone.utc),
            connection_id=connection.id, enqueue_analysis=False, db=db,
        )
    return True


async def _send_email(business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession) -> bool:
    """
    Same "verified EmailSendConnection + customer's own email identity"
    shape shared/scheduling/notify.py's own onboarding/expansion nudges
    already use for their email fallback, not a second lookup path for
    the same two things.
    """
    subject = str(config.get("subject") or "").strip()
    body = str(config.get("body") or "").strip()
    if not subject or not body:
        logger.warning("send_email rule for business=%s is missing subject/body, skipping", business_id)
        return False

    from shared.db.models import CustomerIdentity, EmailSendConnection, IdentityKind
    from shared.integrations import postmark

    connection = (
        await db.execute(
            select(EmailSendConnection).where(
                EmailSendConnection.business_id == business_id,
                EmailSendConnection.verified.is_(True),
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        logger.info("send_email rule skipped business=%s: no verified email connection", business_id)
        return False

    to_email = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.email,
            )
        )
    ).scalars().first()
    if to_email is None:
        logger.info("send_email rule skipped business=%s customer=%s: no email on file", business_id, customer_id)
        return False

    try:
        await postmark.send_email(from_email=connection.from_email, to=to_email, subject=subject, text_body=body)
    except postmark.PostmarkError:
        logger.exception("send_email rule failed business=%s customer=%s", business_id, customer_id)
        return False
    return True


async def _add_tag(business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession) -> bool:
    """Same shape as crm.py's own add_tag endpoint, minus a human user - a
    rule's own reasoning fills where that endpoint records who typed it."""
    from shared.db.models import CustomerTag, TagStatus

    label = str(config.get("tag") or "").strip().lower()
    if not label:
        logger.warning("add_tag rule for business=%s has no tag configured, skipping", business_id)
        return False

    existing = (
        await db.execute(
            select(CustomerTag).where(CustomerTag.customer_id == customer_id, CustomerTag.label == label)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.status = TagStatus.confirmed
        return True

    db.add(
        CustomerTag(
            business_id=business_id,
            customer_id=customer_id,
            label=label,
            status=TagStatus.confirmed,
            reasoning="Added automatically by an automation rule",
        )
    )
    return True


async def _send_flow(
    business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession, *,
    context: dict, call_id: uuid.UUID | None,
) -> bool:
    """
    Same mechanics as services/api/routers/flows.py's own send_flow
    endpoint - resolve the flow, the customer's phone, the service window,
    the WhatsApp connection, send, log. Duplicated rather than imported
    because that endpoint is current_user-scoped (a human clicking Send);
    this path has no user, only a business_id and a rule that fired.

    Only ever sends a *published* flow - never draft/test mode, since
    nobody is standing by as an app tester for a rule that fires
    unattended.

    `body` and every string value in `data` go through `_resolve_tokens`
    against this trigger's own `context` - e.g. a call.completed rule can
    send `{"reason": "{{requested_service}}"}` so the Flow (and its own
    doctor/service pre-fill, if the vertical's flow template reads it)
    carries what the caller actually asked for, not a generic message.
    `call_id`, when this fired from a voice call, is recorded on the
    outbound Message's own `raw` metadata as `origin_call_id` - enough to
    tell a voice-triggered WhatsApp booking apart from a plain one without
    a new IntakeChannel value (channel and "what triggered it" are
    different axes; every WhatsApp Flow booking is still, correctly,
    IntakeChannel.whatsapp).
    """
    import datetime as _dt

    from shared.auth.encryption import decrypt
    from shared.channels import ingest
    from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError, within_service_window
    from shared.db.models import (
        Channel,
        ChannelConnection,
        ConnectionStatus,
        CustomerIdentity,
        Direction,
        FlowSendLog,
        FlowStatus,
        IdentityKind,
        Message,
        WhatsAppFlow,
    )

    flow_id_raw = config.get("flow_id")
    body = _resolve_tokens(str(config.get("body") or ""), context).strip()
    screen = str(config.get("screen") or "").strip()
    cta = str(config.get("cta") or "Open").strip()
    data = {
        k: (_resolve_tokens(v, context) if isinstance(v, str) else v)
        for k, v in (config.get("data") or {}).items()
    }
    if not (flow_id_raw and body and screen):
        logger.warning("send_flow rule for business=%s is missing flow_id/body/screen, skipping", business_id)
        return False
    try:
        flow_id = uuid.UUID(str(flow_id_raw))
    except ValueError:
        logger.warning("send_flow rule for business=%s has an invalid flow_id, skipping", business_id)
        return False

    flow = await db.get(WhatsAppFlow, flow_id)
    if flow is None or flow.business_id != business_id or flow.status != FlowStatus.published:
        logger.warning("send_flow rule for business=%s points at a missing/unpublished flow, skipping", business_id)
        return False

    identity = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.phone,
            )
        )
    ).scalars().first()
    if identity is None:
        return False

    last_inbound = (
        await db.execute(
            select(Message.occurred_at)
            .where(Message.customer_id == customer_id, Message.direction == Direction.inbound)
            .order_by(Message.occurred_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if not within_service_window(last_inbound):
        return False

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        return False

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    flow_token = str(uuid.uuid4())
    try:
        result = await client.send_flow_message(
            identity, body, flow_id=flow.meta_flow_id, flow_token=flow_token,
            flow_cta=cta, screen=screen, data=data, draft=False,
        )
    except WhatsAppError:
        logger.exception("send_flow rule for business=%s failed to send", business_id)
        return False

    db.add(FlowSendLog(business_id=business_id, flow_id=flow.id, customer_id=customer_id, flow_token=flow_token))
    await ingest.ingest(
        business_id=business_id,
        channel=Channel.whatsapp,
        direction=Direction.outbound,
        identity_kind=IdentityKind.phone,
        identity_value=identity,
        external_id=result.external_id,
        text=body,
        occurred_at=_dt.datetime.now(_dt.timezone.utc),
        connection_id=connection.id,
        raw={
            "flow_id": flow.meta_flow_id, "screen": screen, "flow_token": flow_token, "automation": True,
            **({"origin_call_id": str(call_id)} if call_id is not None else {}),
        },
        media={"kind": "flow_open", "flow_name": flow.name},
        enqueue_analysis=False,
        db=db,
    )
    return True
# Public aliases for the two helpers the rule builder's own test endpoint
# needs (services/api/routers/post_call_rules.py::test_rule). Both stay
# private-by-convention inside this module's execution path; the preview
# reuses the exact same functions rather than a second, drifting copy of
# "would this condition hold" and "what would the message say", which is
# the only way a preview can be trusted to match what actually runs.
evaluate_condition = _evaluate_condition
resolve_tokens = _resolve_tokens
describe_delay = _describe_delay

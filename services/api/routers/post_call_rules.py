"""
Managing PostCallActionRule rows - the trigger-to-action bridge's own CRUD.

See shared/care/post_call_actions.py for the interpreter these rows
drive, and its own docstring for why the table/router keep their
original "post-call" name despite now covering every channel, not only
voice. Deliberately a plain CRUD surface, no execution logic here at
all - a rule only ever runs from wherever apply_rules() is actually
called (see that module's docstring for the current list of dispatch
points).

A rule owns an ordered list of AutomationStep rows (position 0, 1, 2, ...)
- what actually runs (apply_rules reads AutomationStep rows, never the
rule's own fields). The rule's own action_type/action_config columns stay
a mirror of step 0, kept only because those DB columns are NOT NULL -
nothing else reads them (confirmed by grep before this pass): every real
consumer, inside and outside this file, works off the steps list.

update_rule matches incoming steps to existing ones by position (index 0
keeps row 0's identity, index 1 keeps row 1's, ...) rather than deleting
and recreating every step on every save - a step whose position survives
the edit keeps its id, so a still-pending AutomationStepRun queued
against it (see that model's own docstring) is untouched by an unrelated
edit elsewhere in the same rule. A step whose position no longer exists
in the new list is deleted, which does cascade away any run still
pending against it - the one case that should happen: removing a step
should cancel what it had queued.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.care.post_call_actions import (
    CONDITION_FIELDS,
    MAX_CONDITIONS_PER_STEP,
    OPERATORS,
    RUN_LOG_RETENTION_DAYS,
    describe_delay,
    PAYER_ACTIONS,
    evaluate_condition,
    normalize_conditions,
    resolve_tokens,
)
from shared.db.models import (
    AutomationRunLog,
    AutomationStep,
    Channel,
    PostCallActionRule,
    WebhookEventType,
)

router = APIRouter(prefix="/post-call-rules", tags=["post-call-rules"])

# None (omitted) means "any channel" - a rule fires regardless of which
# channel the trigger came from, today's original default. Set to one of
# these when a rule should only fire for that one channel - see
# apply_rules()'s own docstring for why this exists at all.
_VALID_CHANNELS = {c.value for c in Channel}

_VALID_TRIGGERS = {
    WebhookEventType.call_completed.value,
    WebhookEventType.call_voicemail.value,
    WebhookEventType.call_no_answer.value,
    WebhookEventType.message_received.value,
    WebhookEventType.comment_received.value,
    WebhookEventType.story_mention_received.value,
    WebhookEventType.story_reply_received.value,
    WebhookEventType.flow_completed.value,
    WebhookEventType.appointment_booked.value,
    WebhookEventType.appointment_cancelled.value,
    WebhookEventType.appointment_rescheduled.value,
    WebhookEventType.escalation_raised.value,
    WebhookEventType.queue_token_issued.value,
    WebhookEventType.competitor_mentioned.value,
    WebhookEventType.churn_risk_detected.value,
    WebhookEventType.demo_requested.value,
    WebhookEventType.pricing_question_asked.value,
    WebhookEventType.bug_detected.value,
    WebhookEventType.feature_request_detected.value,
    WebhookEventType.complaint_detected.value,
    WebhookEventType.praise_detected.value,
    WebhookEventType.overdue_followup_detected.value,
    WebhookEventType.report_not_collected_detected.value,
    WebhookEventType.callback_overdue_detected.value,
    WebhookEventType.overdue_refund_detected.value,
    WebhookEventType.intent_leakage_detected.value,
    WebhookEventType.rto_risk_detected.value,
    # Fires from shared/care/signal_dispatch.py like the kinds above, and
    # has always had its own CONDITION_FIELDS entry - it was simply never
    # added here, so the /automations page offered it and saving one
    # returned 422. Adding it is the fix; nothing else changes.
    WebhookEventType.claim_status_changed.value,
    # The time-based triggers - fired daily by shared/care/date_triggers.py
    # rather than by anything arriving. These are the ones that let a
    # business set its own timing ("3 days before a payment is due")
    # instead of inheriting the numbers baked into our own sweeps.
    WebhookEventType.commitment_due_soon.value,
    WebhookEventType.commitment_overdue.value,
    WebhookEventType.quotation_aging.value,
    WebhookEventType.customer_inactive.value,
    WebhookEventType.customer_stage_changed.value,
    # escalation_rate_detected / account_health_detected deliberately
    # excluded - business-level signals with no customer_id, so a rule on
    # either could never actually fire (see shared/care/signal_dispatch.py).
}
_VALID_ACTIONS = {
    "whatsapp_followup", "create_escalation_task", "add_tag", "send_flow",
    "place_call", "send_sms", "send_email", "instagram_followup",
    "instagram_comment_reply",
}

# A generous ceiling, not a real product limit discovered anywhere - just
# enough to reject an obvious typo (a business meaning minutes and typing
# seconds by mistake) without guessing at a "real" maximum useful delay.
_MAX_DELAY_SECONDS = 30 * 24 * 3600

# Also not a discovered real limit - just a sanity cap so a rule can't be
# saved with an unbounded chain, same reasoning as _MAX_DELAY_SECONDS.
_MAX_STEPS = 10


class ConditionOut(BaseModel):
    field: str
    operator: str
    value: object


class ConditionIn(BaseModel):
    field: str
    operator: str
    value: object


class StepOut(BaseModel):
    action_type: str
    action_config: dict
    # Every condition on the step, all of which must hold. Always a list,
    # whatever shape the row was stored in (see normalize_conditions).
    conditions: list[ConditionOut] = []
    # The first of `conditions`, kept only so a client written before a
    # step could hold more than one still reads something sensible. It is
    # not the whole truth for a multi-condition step - read `conditions`.
    condition: ConditionOut | None = None
    # None = runs immediately, today's original behaviour for a step with
    # no delay set. See shared/db/models/integrations.py::AutomationStepRun
    # for how a delayed step actually fires later.
    delay_seconds: int | None = None


class StepIn(BaseModel):
    action_type: str
    action_config: dict = {}
    # All must hold for the step to run (AND). Empty/omitted = always
    # runs. See shared/care/post_call_actions.py::CONDITION_FIELDS for the
    # per-trigger_type allowlist each `field` is checked against.
    conditions: list[ConditionIn] | None = None
    # The single-condition shape from before `conditions` existed - still
    # accepted so an older client keeps working. Ignored whenever
    # `conditions` is present: StepOut returns both fields, so a client
    # that echoes a step back unchanged (pause/resume does exactly that)
    # would otherwise save its first condition twice.
    condition: ConditionIn | None = None
    # None (omitted, the default) = runs immediately.
    delay_seconds: int | None = None


class PostCallRuleOut(BaseModel):
    id: str
    name: str | None = None
    trigger_type: str
    is_active: bool
    channel: str | None = None
    steps: list[StepOut]


def _to_out(rule: PostCallActionRule, steps: list[AutomationStep]) -> PostCallRuleOut:
    return PostCallRuleOut(
        id=str(rule.id),
        name=rule.name,
        trigger_type=rule.trigger_type,
        is_active=rule.is_active,
        channel=rule.channel,
        steps=[
            _step_out(s)
            for s in steps
        ],
    )


def _step_out(step: AutomationStep) -> StepOut:
    conditions = [ConditionOut(**c) for c in normalize_conditions(step.condition)]
    return StepOut(
        action_type=step.action_type,
        action_config=step.action_config or {},
        conditions=conditions,
        condition=conditions[0] if conditions else None,
        delay_seconds=step.delay_seconds,
    )


def _step_conditions(step: StepIn) -> list[ConditionIn]:
    """The step's conditions - `conditions` when sent, else the legacy
    single `condition`. Never both: see StepIn.condition for why."""
    if step.conditions is not None:
        return list(step.conditions)
    return [step.condition] if step.condition is not None else []


def _stored_conditions(step: StepIn) -> list[dict] | None:
    """What goes in AutomationStep.condition: always a list, or None."""
    combined = _step_conditions(step)
    return [c.model_dump() for c in combined] or None


async def _steps_by_rule(rule_ids: list[uuid.UUID], db: DbDep) -> dict[uuid.UUID, list[AutomationStep]]:
    """Every rule's own steps, in position order, keyed by rule_id."""
    if not rule_ids:
        return {}
    result = await db.execute(
        select(AutomationStep).where(AutomationStep.rule_id.in_(rule_ids)).order_by(AutomationStep.position)
    )
    by_rule: dict[uuid.UUID, list[AutomationStep]] = {}
    for step in result.scalars().all():
        by_rule.setdefault(step.rule_id, []).append(step)
    return by_rule


@router.get("", response_model=list[PostCallRuleOut])
async def list_rules(current_user: CurrentUserDep, db: DbDep) -> list[PostCallRuleOut]:
    result = await db.execute(
        select(PostCallActionRule).where(PostCallActionRule.business_id == current_user.business)
    )
    rules = result.scalars().all()
    steps = await _steps_by_rule([r.id for r in rules], db)
    return [_to_out(r, steps.get(r.id, [])) for r in rules]


class PostCallRuleIn(BaseModel):
    # Purely descriptive, the business's own label - never interpreted,
    # never validated beyond length. See PostCallActionRule.name's own
    # docstring for why this exists.
    name: str | None = Field(default=None, max_length=255)
    trigger_type: str
    is_active: bool = True
    # None (omitted) = any channel, matching the model's own default.
    channel: str | None = None
    steps: list[StepIn] = Field(min_length=1)


def _validate(body: PostCallRuleIn) -> None:
    if body.trigger_type not in _VALID_TRIGGERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"trigger_type must be one of {sorted(_VALID_TRIGGERS)}",
        )
    if body.channel is not None and body.channel not in _VALID_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"channel must be one of {sorted(_VALID_CHANNELS)}",
        )
    if len(body.steps) > _MAX_STEPS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"a rule may have at most {_MAX_STEPS} steps",
        )
    for i, step in enumerate(body.steps):
        _validate_step(body.trigger_type, step, i)


def _validate_step(trigger_type: str, step: StepIn, index: int) -> None:
    prefix = f"steps[{index}]"
    conditions = _step_conditions(step)
    if len(conditions) > MAX_CONDITIONS_PER_STEP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix} may have at most {MAX_CONDITIONS_PER_STEP} conditions",
        )
    allowed_fields = CONDITION_FIELDS.get(trigger_type, ())
    for c_index, condition in enumerate(conditions):
        if condition.field not in allowed_fields:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{prefix}.conditions[{c_index}].field for {trigger_type} "
                    f"must be one of {sorted(allowed_fields)}"
                ),
            )
        if condition.operator not in OPERATORS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{prefix}.conditions[{c_index}].operator must be one of {sorted(OPERATORS)}",
            )
    if step.delay_seconds is not None and not (0 < step.delay_seconds <= _MAX_DELAY_SECONDS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}.delay_seconds must be between 1 and {_MAX_DELAY_SECONDS}",
        )
    if step.action_type not in _VALID_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}.action_type must be one of {sorted(_VALID_ACTIONS)}",
        )
    send_to = (step.action_config or {}).get("send_to")
    if send_to not in (None, "customer", "payer"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}.action_config.send_to must be customer or payer",
        )
    if send_to == "payer" and step.action_type not in PAYER_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}: {step.action_type} can't be sent to a payer - only {sorted(PAYER_ACTIONS)}",
        )
    if step.action_type == "whatsapp_followup" and not (step.action_config or {}).get("message"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}.action_config.message is required for whatsapp_followup",
        )
    if step.action_type == "instagram_followup" and not (step.action_config or {}).get("message"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}.action_config.message is required for instagram_followup",
        )
    if step.action_type == "instagram_comment_reply" and not (step.action_config or {}).get("message"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}.action_config.message is required for instagram_comment_reply",
        )
    if step.action_type == "add_tag" and not (step.action_config or {}).get("tag"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}.action_config.tag is required for add_tag",
        )
    if step.action_type == "send_flow":
        missing = [k for k in ("flow_id", "body", "screen") if not (step.action_config or {}).get(k)]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{prefix}.action_config.{missing[0]} is required for send_flow",
            )
    if step.action_type == "place_call" and not (step.action_config or {}).get("reason"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}.action_config.reason is required for place_call",
        )
    if step.action_type == "send_sms" and not (step.action_config or {}).get("message"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}.action_config.message is required for send_sms",
        )
    if step.action_type == "send_email":
        missing = [k for k in ("subject", "body") if not (step.action_config or {}).get(k)]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{prefix}.action_config.{missing[0]} is required for send_email",
            )


@router.post("", response_model=PostCallRuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(body: PostCallRuleIn, current_user: CurrentUserDep, db: DbDep) -> PostCallRuleOut:
    _validate(body)
    first = body.steps[0]
    rule = PostCallActionRule(
        business_id=current_user.business,
        name=body.name,
        trigger_type=body.trigger_type,
        # Mirror of step 0 - these columns are NOT NULL and nothing else
        # reads them (see this module's own docstring).
        action_type=first.action_type,
        action_config=first.action_config,
        is_active=body.is_active,
        channel=body.channel,
    )
    db.add(rule)
    await db.flush()

    steps = [
        AutomationStep(
            rule_id=rule.id, position=i,
            condition=_stored_conditions(step),
            delay_seconds=step.delay_seconds,
            action_type=step.action_type, action_config=step.action_config,
        )
        for i, step in enumerate(body.steps)
    ]
    db.add_all(steps)
    await db.commit()
    return _to_out(rule, steps)


async def _owned_rule(rule_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> PostCallActionRule:
    rule = await db.get(PostCallActionRule, rule_id)
    if rule is None or rule.business_id != current_user.business:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    return rule


@router.patch("/{rule_id}", response_model=PostCallRuleOut)
async def update_rule(
    rule_id: uuid.UUID, body: PostCallRuleIn, current_user: CurrentUserDep, db: DbDep
) -> PostCallRuleOut:
    _validate(body)
    rule = await _owned_rule(rule_id, current_user, db)
    first = body.steps[0]
    rule.name = body.name
    rule.trigger_type = body.trigger_type
    rule.action_type = first.action_type
    rule.action_config = first.action_config
    rule.is_active = body.is_active
    rule.channel = body.channel

    existing = (
        await db.execute(
            select(AutomationStep).where(AutomationStep.rule_id == rule.id).order_by(AutomationStep.position)
        )
    ).scalars().all()

    final_steps: list[AutomationStep] = []
    for i, step_in in enumerate(body.steps):
        condition = _stored_conditions(step_in)
        if i < len(existing):
            # Position i keeps its existing row (and id) - an
            # AutomationStepRun already queued against it stays valid;
            # only an edit that actually removes this position should
            # cancel anything.
            step = existing[i]
            step.condition = condition
            step.delay_seconds = step_in.delay_seconds
            step.action_type = step_in.action_type
            step.action_config = step_in.action_config
        else:
            step = AutomationStep(
                rule_id=rule.id, position=i, condition=condition,
                delay_seconds=step_in.delay_seconds,
                action_type=step_in.action_type, action_config=step_in.action_config,
            )
            db.add(step)
        final_steps.append(step)

    # Positions beyond the new, shorter list are gone - cascades away any
    # run still pending against them, which is the correct behaviour for
    # a step the business actually removed.
    for stale in existing[len(body.steps):]:
        await db.delete(stale)

    await db.commit()
    return _to_out(rule, final_steps)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    rule = await _owned_rule(rule_id, current_user, db)
    await db.delete(rule)
    await db.commit()


# ---------------------------------------------------------------------------
# Execution history - "did my rule actually do anything?"
#
# The single most damaging gap in this builder before now: a rule with no
# delayed step left no trace anywhere a business could see, so silence was
# indistinguishable from broken. See AutomationRunLog's own docstring.
# ---------------------------------------------------------------------------

# A page, not a full history - the panel this feeds shows recent activity,
# and a business that wants to audit further back is a signal to build real
# pagination rather than a reason to ship an unbounded query.
_MAX_RUN_ROWS = 200


class RunLogOut(BaseModel):
    id: str
    rule_id: str
    rule_name: str | None = None
    trigger_type: str
    step_position: int
    action_type: str
    # ran / no_action / skipped / queued / failed - see AutomationRunLog.
    status: str
    # Already a plain sentence when the backend wrote it; the UI shows it
    # verbatim rather than translating a code.
    detail: str | None = None
    customer_id: str | None = None
    occurred_at: datetime


class RunHistoryOut(BaseModel):
    # How long history is kept, so the panel can say so rather than leaving
    # an empty list ambiguous between "never ran" and "aged out".
    retention_days: int
    runs: list[RunLogOut]


@router.get("/runs", response_model=RunHistoryOut)
async def list_runs(
    current_user: CurrentUserDep,
    db: DbDep,
    rule_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=_MAX_RUN_ROWS),
) -> RunHistoryOut:
    """
    What this business's automations actually did, newest first. Optionally
    narrowed to one rule.

    A log row whose rule was since deleted does not appear here - the FK
    cascades it away. That is deliberate: history attributed to a rule
    nobody can open is not something a business can act on.
    """
    stmt = select(AutomationRunLog).where(AutomationRunLog.business_id == current_user.business)
    if rule_id is not None:
        # Ownership is already covered by the business_id filter above - a
        # rule_id from another business simply matches nothing.
        stmt = stmt.where(AutomationRunLog.rule_id == rule_id)
    rows = (
        await db.execute(stmt.order_by(desc(AutomationRunLog.created_at)).limit(limit))
    ).scalars().all()

    names: dict[uuid.UUID, str | None] = {}
    if rows:
        rule_rows = (
            await db.execute(
                select(PostCallActionRule.id, PostCallActionRule.name).where(
                    PostCallActionRule.id.in_({r.rule_id for r in rows})
                )
            )
        ).all()
        names = {rid: name for rid, name in rule_rows}

    return RunHistoryOut(
        retention_days=RUN_LOG_RETENTION_DAYS,
        runs=[
            RunLogOut(
                id=str(r.id),
                rule_id=str(r.rule_id),
                rule_name=names.get(r.rule_id),
                trigger_type=r.trigger_type,
                step_position=r.step_position,
                action_type=r.action_type,
                status=r.status,
                detail=r.detail,
                customer_id=str(r.customer_id) if r.customer_id else None,
                occurred_at=r.created_at,
            )
            for r in rows
        ],
    )


# ---------------------------------------------------------------------------
# Test before live
#
# A rule saved here goes live against real customers immediately, and there
# was no way to try one first. This evaluates a rule against a context the
# business supplies and reports what each step would do - reusing
# post_call_actions' own condition evaluator and token resolver, never a
# second copy, so the preview cannot drift from what actually runs.
#
# Deliberately sends nothing, and has no "run it for real on one customer"
# mode: for a tool whose actions message real people, a dry run that can
# accidentally not be dry is worse than no dry run at all.
# ---------------------------------------------------------------------------


class RuleTestIn(BaseModel):
    trigger_type: str
    channel: str | None = None
    steps: list[StepIn] = Field(min_length=1)
    # Keyed to CONDITION_FIELDS[trigger_type] - the same shape the real
    # dispatch site passes. Values the business types in, or that the UI
    # pre-fills from a recent real run of the same trigger.
    context: dict = {}


class StepTestOut(BaseModel):
    position: int
    action_type: str
    # would_run / would_skip / would_wait
    verdict: str
    detail: str | None = None
    # The text as it would actually be sent, with {{tokens}} resolved
    # against the supplied context - the other half of "why did my rule do
    # that", and the part a business can check at a glance.
    preview: str | None = None


class RuleTestOut(BaseModel):
    # What this trigger really carries, so the UI can build the input form
    # from the same allowlist the validator uses.
    available_fields: list[str]
    # Fields a condition needs that the supplied context does not have.
    # Named explicitly because a condition on a field the trigger never
    # carries fails closed at runtime, and is the single most confusing
    # way for a rule to quietly do nothing.
    missing_fields: list[str]
    steps: list[StepTestOut]


# Where each action's human-readable text lives in its action_config, for
# the preview. An action absent here has no text worth previewing (add_tag
# carries a tag name, not a message anyone reads).
_PREVIEW_FIELD = {
    "whatsapp_followup": "message",
    "instagram_followup": "message",
    "instagram_comment_reply": "message",
    "send_sms": "message",
    "send_email": "body",
    "send_flow": "body",
    "create_escalation_task": "reason",
    "place_call": "reason",
}


@router.post("/test", response_model=RuleTestOut)
async def test_rule(body: RuleTestIn, current_user: CurrentUserDep) -> RuleTestOut:
    """
    What this rule would do, given this trigger data. Nothing is sent,
    nothing is written, and the rule need not exist yet - a business can
    check one before ever saving it.
    """
    _validate(
        PostCallRuleIn(trigger_type=body.trigger_type, channel=body.channel, steps=body.steps)
    )

    available = list(CONDITION_FIELDS.get(body.trigger_type, ()))
    missing = sorted(
        {
            condition.field
            for step in body.steps
            for condition in _step_conditions(step)
            if condition.field not in body.context
        }
    )

    results: list[StepTestOut] = []
    for i, step in enumerate(body.steps):
        held, reason = evaluate_condition(_stored_conditions(step), body.context)
        if not held:
            results.append(StepTestOut(
                position=i, action_type=step.action_type,
                verdict="would_skip", detail="condition not met: " + str(reason),
            ))
            continue

        preview_field = _PREVIEW_FIELD.get(step.action_type)
        raw = (step.action_config or {}).get(preview_field) if preview_field else None
        preview = resolve_tokens(raw, body.context) if isinstance(raw, str) else None

        if step.delay_seconds:
            results.append(StepTestOut(
                position=i, action_type=step.action_type, verdict="would_wait",
                # Same wording as the real execution log writes, so a
                # preview and the history it later produces read alike.
                detail=(
                    describe_delay(step.delay_seconds)
                    + ", then run - every step after this one waits with it"
                ),
                preview=preview,
            ))
            continue

        results.append(StepTestOut(
            position=i, action_type=step.action_type, verdict="would_run", preview=preview,
        ))

    return RuleTestOut(available_fields=available, missing_fields=missing, steps=results)

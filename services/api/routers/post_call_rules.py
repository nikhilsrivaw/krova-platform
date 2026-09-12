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

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.care.post_call_actions import CONDITION_FIELDS, OPERATORS
from shared.db.models import AutomationStep, Channel, PostCallActionRule, WebhookEventType

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
    WebhookEventType.flow_completed.value,
    WebhookEventType.appointment_booked.value,
    WebhookEventType.appointment_cancelled.value,
    WebhookEventType.escalation_raised.value,
    WebhookEventType.queue_token_issued.value,
    WebhookEventType.competitor_mentioned.value,
    WebhookEventType.churn_risk_detected.value,
    WebhookEventType.demo_requested.value,
    WebhookEventType.pricing_question_asked.value,
}
_VALID_ACTIONS = {
    "whatsapp_followup", "create_escalation_task", "add_tag", "send_flow",
    "place_call", "send_sms", "send_email",
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
    condition: ConditionOut | None = None
    # None = runs immediately, today's original behaviour for a step with
    # no delay set. See shared/db/models/integrations.py::AutomationStepRun
    # for how a delayed step actually fires later.
    delay_seconds: int | None = None


class StepIn(BaseModel):
    action_type: str
    action_config: dict = {}
    # None (omitted, the default) = the step always runs - see
    # shared/care/post_call_actions.py::CONDITION_FIELDS for the real,
    # per-trigger_type allowlist `field` is checked against.
    condition: ConditionIn | None = None
    # None (omitted, the default) = runs immediately.
    delay_seconds: int | None = None


class PostCallRuleOut(BaseModel):
    id: str
    trigger_type: str
    is_active: bool
    channel: str | None = None
    steps: list[StepOut]


def _to_out(rule: PostCallActionRule, steps: list[AutomationStep]) -> PostCallRuleOut:
    return PostCallRuleOut(
        id=str(rule.id),
        trigger_type=rule.trigger_type,
        is_active=rule.is_active,
        channel=rule.channel,
        steps=[
            StepOut(
                action_type=s.action_type,
                action_config=s.action_config or {},
                condition=ConditionOut(**s.condition) if s.condition else None,
                delay_seconds=s.delay_seconds,
            )
            for s in steps
        ],
    )


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
    if step.condition is not None:
        allowed_fields = CONDITION_FIELDS.get(trigger_type, ())
        if step.condition.field not in allowed_fields:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{prefix}.condition.field for {trigger_type} must be one of {sorted(allowed_fields)}",
            )
        if step.condition.operator not in OPERATORS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{prefix}.condition.operator must be one of {sorted(OPERATORS)}",
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
    if step.action_type == "whatsapp_followup" and not (step.action_config or {}).get("message"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{prefix}.action_config.message is required for whatsapp_followup",
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
            condition=step.condition.model_dump() if step.condition else None,
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
        condition = step_in.condition.model_dump() if step_in.condition else None
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

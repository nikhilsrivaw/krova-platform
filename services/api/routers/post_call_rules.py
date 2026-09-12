"""
Managing PostCallActionRule rows - the trigger-to-action bridge's own CRUD.

See shared/care/post_call_actions.py for the interpreter these rows
drive, and its own docstring for why the table/router keep their
original "post-call" name despite now covering every channel, not only
voice. Deliberately a plain CRUD surface, no execution logic here at
all - a rule only ever runs from wherever apply_rules() is actually
called (see that module's docstring for the current list of dispatch
points).

A rule owns exactly one AutomationStep as of this pass (position 0) -
the engine's own multi-step/chaining phase is later, not this one - but
that step, not the rule's own action_type/action_config, is now what
actually runs (apply_rules reads AutomationStep rows). This router keeps
both in sync: the rule's own fields stay a live mirror of its one step,
so nothing reading rule.action_type/action_config directly (existing
API responses, the frontend) needs to change shape for this pass.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
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


class ConditionOut(BaseModel):
    field: str
    operator: str
    value: object


class PostCallRuleOut(BaseModel):
    id: str
    trigger_type: str
    action_type: str
    action_config: dict
    is_active: bool
    channel: str | None = None
    condition: ConditionOut | None = None


def _to_out(rule: PostCallActionRule, step: AutomationStep | None) -> PostCallRuleOut:
    return PostCallRuleOut(
        id=str(rule.id),
        trigger_type=rule.trigger_type,
        action_type=rule.action_type,
        action_config=rule.action_config or {},
        is_active=rule.is_active,
        channel=rule.channel,
        condition=ConditionOut(**step.condition) if step is not None and step.condition else None,
    )


async def _first_steps(rule_ids: list[uuid.UUID], db: DbDep) -> dict[uuid.UUID, AutomationStep]:
    """Each rule's own position-0 step, keyed by rule_id - a rule owns exactly one as of this pass."""
    if not rule_ids:
        return {}
    result = await db.execute(
        select(AutomationStep).where(AutomationStep.rule_id.in_(rule_ids), AutomationStep.position == 0)
    )
    return {s.rule_id: s for s in result.scalars().all()}


@router.get("", response_model=list[PostCallRuleOut])
async def list_rules(current_user: CurrentUserDep, db: DbDep) -> list[PostCallRuleOut]:
    result = await db.execute(
        select(PostCallActionRule).where(PostCallActionRule.business_id == current_user.business)
    )
    rules = result.scalars().all()
    steps = await _first_steps([r.id for r in rules], db)
    return [_to_out(r, steps.get(r.id)) for r in rules]


class ConditionIn(BaseModel):
    field: str
    operator: str
    value: object


class PostCallRuleIn(BaseModel):
    trigger_type: str
    action_type: str
    action_config: dict = {}
    is_active: bool = True
    # None (omitted) = any channel, matching the model's own default.
    channel: str | None = None
    # None (omitted, the default) = the step always runs - today's only
    # real behaviour. See shared/care/post_call_actions.py::CONDITION_FIELDS
    # for the real, per-trigger_type allowlist `field` is checked against.
    condition: ConditionIn | None = None


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
    if body.condition is not None:
        allowed_fields = CONDITION_FIELDS.get(body.trigger_type, ())
        if body.condition.field not in allowed_fields:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"condition.field for {body.trigger_type} must be one of {sorted(allowed_fields)}",
            )
        if body.condition.operator not in OPERATORS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"condition.operator must be one of {sorted(OPERATORS)}",
            )
    if body.action_type not in _VALID_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"action_type must be one of {sorted(_VALID_ACTIONS)}",
        )
    if body.action_type == "whatsapp_followup" and not (body.action_config or {}).get("message"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="action_config.message is required for whatsapp_followup",
        )
    if body.action_type == "add_tag" and not (body.action_config or {}).get("tag"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="action_config.tag is required for add_tag",
        )
    if body.action_type == "send_flow":
        missing = [k for k in ("flow_id", "body", "screen") if not (body.action_config or {}).get(k)]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"action_config.{missing[0]} is required for send_flow",
            )
    if body.action_type == "place_call" and not (body.action_config or {}).get("reason"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="action_config.reason is required for place_call",
        )
    if body.action_type == "send_sms" and not (body.action_config or {}).get("message"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="action_config.message is required for send_sms",
        )
    if body.action_type == "send_email":
        missing = [k for k in ("subject", "body") if not (body.action_config or {}).get(k)]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"action_config.{missing[0]} is required for send_email",
            )


@router.post("", response_model=PostCallRuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(body: PostCallRuleIn, current_user: CurrentUserDep, db: DbDep) -> PostCallRuleOut:
    _validate(body)
    rule = PostCallActionRule(
        business_id=current_user.business,
        trigger_type=body.trigger_type,
        action_type=body.action_type,
        action_config=body.action_config,
        is_active=body.is_active,
        channel=body.channel,
    )
    db.add(rule)
    await db.flush()
    # The one step apply_rules() actually reads (shared/care/
    # post_call_actions.py) - the rule's own action_type/action_config
    # above stay a live mirror of it, kept for anything still reading the
    # rule directly rather than a schema this pass needs to remove.
    step = AutomationStep(
        rule_id=rule.id, position=0,
        condition=body.condition.model_dump() if body.condition else None,
        action_type=body.action_type, action_config=body.action_config,
    )
    db.add(step)
    await db.commit()
    return _to_out(rule, step)


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
    rule.trigger_type = body.trigger_type
    rule.action_type = body.action_type
    rule.action_config = body.action_config
    rule.is_active = body.is_active
    rule.channel = body.channel

    step = (await _first_steps([rule.id], db)).get(rule.id)
    condition = body.condition.model_dump() if body.condition else None
    if step is None:
        # A rule saved before this pass shipped, or one whose step was
        # somehow lost - repaired here rather than left permanently inert,
        # since apply_rules() only ever reads from AutomationStep now.
        step = AutomationStep(rule_id=rule.id, position=0)
        db.add(step)
    step.condition = condition
    step.action_type = body.action_type
    step.action_config = body.action_config

    await db.commit()
    return _to_out(rule, step)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    rule = await _owned_rule(rule_id, current_user, db)
    await db.delete(rule)
    await db.commit()

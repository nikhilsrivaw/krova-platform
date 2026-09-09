"""
Managing PostCallActionRule rows - the voice-to-action bridge's own CRUD.

See shared/care/post_call_actions.py for the interpreter these rows
drive. Deliberately a plain CRUD surface, no execution logic here at
all - a rule only ever runs from the two dispatch points in
shared/channels/voice/{outbound,relay}.py.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import PostCallActionRule, WebhookEventType

router = APIRouter(prefix="/post-call-rules", tags=["post-call-rules"])

_VALID_TRIGGERS = {
    WebhookEventType.call_completed.value,
    WebhookEventType.call_voicemail.value,
    WebhookEventType.call_no_answer.value,
}
_VALID_ACTIONS = {"whatsapp_followup", "create_escalation_task"}


class PostCallRuleOut(BaseModel):
    id: str
    trigger_type: str
    action_type: str
    action_config: dict
    is_active: bool


def _to_out(rule: PostCallActionRule) -> PostCallRuleOut:
    return PostCallRuleOut(
        id=str(rule.id),
        trigger_type=rule.trigger_type,
        action_type=rule.action_type,
        action_config=rule.action_config or {},
        is_active=rule.is_active,
    )


@router.get("", response_model=list[PostCallRuleOut])
async def list_rules(current_user: CurrentUserDep, db: DbDep) -> list[PostCallRuleOut]:
    result = await db.execute(
        select(PostCallActionRule).where(PostCallActionRule.business_id == current_user.business)
    )
    return [_to_out(r) for r in result.scalars().all()]


class PostCallRuleIn(BaseModel):
    trigger_type: str
    action_type: str
    action_config: dict = {}
    is_active: bool = True


def _validate(body: PostCallRuleIn) -> None:
    if body.trigger_type not in _VALID_TRIGGERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"trigger_type must be one of {sorted(_VALID_TRIGGERS)}",
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


@router.post("", response_model=PostCallRuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(body: PostCallRuleIn, current_user: CurrentUserDep, db: DbDep) -> PostCallRuleOut:
    _validate(body)
    rule = PostCallActionRule(
        business_id=current_user.business,
        trigger_type=body.trigger_type,
        action_type=body.action_type,
        action_config=body.action_config,
        is_active=body.is_active,
    )
    db.add(rule)
    await db.commit()
    return _to_out(rule)


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
    await db.commit()
    return _to_out(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    rule = await _owned_rule(rule_id, current_user, db)
    await db.delete(rule)
    await db.commit()

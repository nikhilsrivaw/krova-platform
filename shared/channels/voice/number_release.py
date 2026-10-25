"""
Giving a voice number back without leaving anything pointing at it.

A number is more than a line: campaigns call from it, automation rules may only fire for calls on it, and it may
be the number every outgoing call uses by default. Before a release the owner is shown all of that
(`dependents`); on release (`settle`):

  campaigns     still in draft, sending or paused move to the number the owner picked - or, with none picked,
                stop pointing at any number (a campaign that was sending is paused, so nothing dials out from
                a number the business no longer has);
  rules         whose condition says "number is this one" are switched off, never left to fire on every number;
  default       if this was the default outgoing number, the oldest remaining number that can call out takes over.

Call history stays: the connection row is only marked disconnected (see the release route).
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.channels.voice import numbers
from shared.db.models import (
    AutomationStep,
    CallCampaign,
    CallCampaignStatus,
    ChannelConnection,
    PostCallActionRule,
)

LIVE_CAMPAIGNS = (CallCampaignStatus.draft, CallCampaignStatus.sending, CallCampaignStatus.paused)
NUMBER_FIELD = "number"


@dataclass
class Dependents:
    campaigns: list[CallCampaign] = field(default_factory=list)
    rules: list[PostCallActionRule] = field(default_factory=list)
    is_default: bool = False
    others: list[ChannelConnection] = field(default_factory=list)


def names_number(condition, connection_id: uuid.UUID) -> bool:
    """Whether a step's stored condition (a dict or a list of them) includes "number equals this one"."""
    items = condition if isinstance(condition, list) else [condition] if isinstance(condition, dict) else []
    return any(
        isinstance(c, dict) and c.get("field") == NUMBER_FIELD and str(c.get("value") or "") == str(connection_id)
        for c in items
    )


async def dependents(db: AsyncSession, business_id: uuid.UUID, connection: ChannelConnection) -> Dependents:
    campaigns = (await db.execute(
        select(CallCampaign).where(
            CallCampaign.business_id == business_id, CallCampaign.connection_id == connection.id,
            CallCampaign.status.in_(LIVE_CAMPAIGNS),
        )
    )).scalars().all()
    steps = (await db.execute(
        select(AutomationStep, PostCallActionRule)
        .join(PostCallActionRule, PostCallActionRule.id == AutomationStep.rule_id)
        .where(PostCallActionRule.business_id == business_id, PostCallActionRule.is_active == True)  # noqa: E712
    )).all()
    rules: dict[uuid.UUID, PostCallActionRule] = {}
    for step, rule in steps:
        if names_number(step.condition, connection.id):
            rules[rule.id] = rule
    active = await numbers.active(db, business_id)
    others = [c for c in active if c.id != connection.id]
    default = numbers.choose_outbound(active)
    return Dependents(
        campaigns=list(campaigns), rules=list(rules.values()),
        is_default=bool(default is not None and default.id == connection.id and len(active) > 1),
        others=others,
    )


async def settle(
    db: AsyncSession, business_id: uuid.UUID, connection: ChannelConnection, *, move_to: ChannelConnection | None,
) -> dict:
    """Move or pause campaigns, switch off rules, hand over the default. Returns counts for the owner."""
    found = await dependents(db, business_id, connection)
    moved = paused = 0
    for campaign in found.campaigns:
        if move_to is not None:
            campaign.connection_id = move_to.id
            moved += 1
        else:
            campaign.connection_id = None
            if campaign.status == CallCampaignStatus.sending:
                campaign.status = CallCampaignStatus.paused
                paused += 1
    for rule in found.rules:
        rule.is_active = False
    new_default = None
    if numbers.is_default_outbound(connection) or found.is_default:
        connection.extra = {**(connection.extra or {}), "default_outbound": False}
        heir = next((c for c in found.others if numbers.allows_outbound(c)), None)
        if heir is not None:
            for other in found.others:
                if numbers.is_default_outbound(other) and other.id != heir.id:
                    other.extra = {**(other.extra or {}), "default_outbound": False}
            heir.extra = {**(heir.extra or {}), "default_outbound": True}
            new_default = heir
    return {
        "campaigns_moved": moved, "campaigns_paused": paused,
        "campaigns_unassigned": len(found.campaigns) - moved, "rules_switched_off": len(found.rules),
        "new_default": numbers.label_of(new_default) if new_default else None,
    }

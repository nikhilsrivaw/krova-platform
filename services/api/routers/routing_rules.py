"""
Teams and routing rules (Team page): which team a conversation or lead goes to. The engine is shared/team/rules.py.

Reading the teams is open to everyone (a team member sees which teams they are in, for the Inbox's "My teams");
creating, changing and ordering teams and rules is for the owner and admins.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from services.api.dependencies import CurrentUserDep, DbDep, OwnerOrAdminDep
from shared.db.models import BusinessMember, Customer, RoutingRule, Team, TeamMember
from shared.team import rules as team_rules

router = APIRouter(prefix="/routing", tags=["routing"])

MAX_TEAMS = 30
MAX_RULES = 50


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    lead_user_id: uuid.UUID | None = None
    reply_target_minutes: int | None = Field(default=None, ge=1, le=1440)
    sharing: str = "round_robin"
    member_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class TeamOut(BaseModel):
    id: str
    name: str
    lead_user_id: str | None
    reply_target_minutes: int | None
    sharing: str
    member_ids: list[str]
    open_chats: int


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    team_id: uuid.UUID
    conditions: dict = Field(default_factory=dict)
    also_if_owned: bool = False
    active: bool = True


class RuleOut(BaseModel):
    id: str
    name: str
    team_id: str
    conditions: dict
    also_if_owned: bool
    active: bool
    position: int


class OrderIn(BaseModel):
    rule_ids: list[uuid.UUID] = Field(max_length=MAX_RULES)


class TestIn(BaseModel):
    text: str = Field(default="", max_length=2000)
    channel: str | None = None
    number: str | None = None
    lead_source: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)


async def _members_of_business(db, business_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.execute(select(BusinessMember.user_id).where(BusinessMember.business_id == business_id))
    return set(rows.scalars().all())


async def _team(db, business_id: uuid.UUID, team_id: uuid.UUID) -> Team:
    team = await db.get(Team, team_id)
    if team is None or team.business_id != business_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
    return team


async def _team_out(db, team: Team) -> TeamOut:
    members = (await db.execute(select(TeamMember.user_id).where(TeamMember.team_id == team.id))).scalars().all()
    open_chats = (await db.execute(select(func.count(Customer.id)).where(
        Customer.team_id == team.id, Customer.resolved_at.is_(None)))).scalar() or 0
    return TeamOut(
        id=str(team.id), name=team.name, lead_user_id=str(team.lead_user_id) if team.lead_user_id else None,
        reply_target_minutes=team.reply_target_minutes, sharing=team.sharing,
        member_ids=[str(m) for m in members], open_chats=int(open_chats),
    )


def _check_team(body: TeamIn, staff: set[uuid.UUID]) -> None:
    if body.sharing not in team_rules.SHARING:
        raise HTTPException(422, "A team shares work round-robin or as a queue.")
    if body.lead_user_id is not None and body.lead_user_id not in staff:
        raise HTTPException(422, "The team lead must be on your team.")
    if any(m not in staff for m in body.member_ids):
        raise HTTPException(422, "Everyone in a team must be on your team (Team page).")


async def _set_members(db, team: Team, member_ids: list[uuid.UUID]) -> None:
    wanted = set(member_ids)
    current = {m.user_id: m for m in (await db.execute(select(TeamMember).where(TeamMember.team_id == team.id))).scalars().all()}
    for user_id, row in current.items():
        if user_id not in wanted:
            await db.delete(row)
    for user_id in wanted - set(current):
        db.add(TeamMember(team_id=team.id, user_id=user_id))


@router.get("/teams", response_model=list[TeamOut])
async def list_teams(current_user: CurrentUserDep, db: DbDep) -> list[TeamOut]:
    teams = (await db.execute(select(Team).where(Team.business_id == current_user.business).order_by(Team.name))).scalars().all()
    return [await _team_out(db, t) for t in teams]


@router.post("/teams", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
async def create_team(body: TeamIn, current_user: OwnerOrAdminDep, db: DbDep) -> TeamOut:
    count = (await db.execute(select(func.count(Team.id)).where(Team.business_id == current_user.business))).scalar() or 0
    if count >= MAX_TEAMS:
        raise HTTPException(422, f"Up to {MAX_TEAMS} teams.")
    name = body.name.strip()
    if (await db.execute(select(Team.id).where(Team.business_id == current_user.business, func.lower(Team.name) == name.lower()))).first():
        raise HTTPException(409, "A team with that name already exists.")
    _check_team(body, await _members_of_business(db, current_user.business))
    team = Team(business_id=current_user.business, name=name, lead_user_id=body.lead_user_id,
                reply_target_minutes=body.reply_target_minutes, sharing=body.sharing)
    db.add(team)
    await db.flush()
    await _set_members(db, team, body.member_ids)
    await db.flush()
    return await _team_out(db, team)


@router.put("/teams/{team_id}", response_model=TeamOut)
async def update_team(team_id: uuid.UUID, body: TeamIn, current_user: OwnerOrAdminDep, db: DbDep) -> TeamOut:
    team = await _team(db, current_user.business, team_id)
    name = body.name.strip()
    clash = (await db.execute(select(Team.id).where(
        Team.business_id == current_user.business, func.lower(Team.name) == name.lower(), Team.id != team.id))).first()
    if clash:
        raise HTTPException(409, "A team with that name already exists.")
    _check_team(body, await _members_of_business(db, current_user.business))
    team.name, team.lead_user_id, team.reply_target_minutes, team.sharing = (
        name, body.lead_user_id, body.reply_target_minutes, body.sharing)
    await _set_members(db, team, body.member_ids)
    await db.flush()
    return await _team_out(db, team)


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(team_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep) -> None:
    """Its rules go with it; its chats keep their owner and simply stop being the team's."""
    team = await _team(db, current_user.business, team_id)
    await db.delete(team)


def _rule_out(rule: RoutingRule) -> RuleOut:
    return RuleOut(id=str(rule.id), name=rule.name, team_id=str(rule.team_id), conditions=rule.conditions or {},
                   also_if_owned=rule.also_if_owned, active=rule.active, position=rule.position)


@router.get("/rules", response_model=list[RuleOut])
async def list_rules(current_user: OwnerOrAdminDep, db: DbDep) -> list[RuleOut]:
    rows = (await db.execute(select(RoutingRule).where(RoutingRule.business_id == current_user.business)
                             .order_by(RoutingRule.position, RoutingRule.created_at))).scalars().all()
    return [_rule_out(r) for r in rows]


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleIn, current_user: OwnerOrAdminDep, db: DbDep) -> RuleOut:
    await _team(db, current_user.business, body.team_id)
    count = (await db.execute(select(func.count(RoutingRule.id)).where(RoutingRule.business_id == current_user.business))).scalar() or 0
    if count >= MAX_RULES:
        raise HTTPException(422, f"Up to {MAX_RULES} rules.")
    rule = RoutingRule(business_id=current_user.business, name=body.name.strip(), team_id=body.team_id,
                       conditions=team_rules.clean_conditions(body.conditions), also_if_owned=body.also_if_owned,
                       active=body.active, position=int(count))
    db.add(rule)
    await db.flush()
    return _rule_out(rule)


@router.put("/rules/{rule_id}", response_model=RuleOut)
async def update_rule(rule_id: uuid.UUID, body: RuleIn, current_user: OwnerOrAdminDep, db: DbDep) -> RuleOut:
    rule = await db.get(RoutingRule, rule_id)
    if rule is None or rule.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    await _team(db, current_user.business, body.team_id)
    rule.name, rule.team_id, rule.conditions = body.name.strip(), body.team_id, team_rules.clean_conditions(body.conditions)
    rule.also_if_owned, rule.active = body.also_if_owned, body.active
    await db.flush()
    return _rule_out(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep) -> None:
    await db.execute(delete(RoutingRule).where(RoutingRule.id == rule_id, RoutingRule.business_id == current_user.business))


@router.put("/rules-order", response_model=list[RuleOut])
async def order_rules(body: OrderIn, current_user: OwnerOrAdminDep, db: DbDep) -> list[RuleOut]:
    """The whole order, top first - the first rule that matches wins."""
    rows = {r.id: r for r in (await db.execute(select(RoutingRule).where(
        RoutingRule.business_id == current_user.business))).scalars().all()}
    if set(body.rule_ids) != set(rows):
        raise HTTPException(422, "Send every rule once, in the new order.")
    for i, rule_id in enumerate(body.rule_ids):
        rows[rule_id].position = i
    await db.flush()
    return [_rule_out(rows[r]) for r in body.rule_ids]


@router.post("/test")
async def test_rules(body: TestIn, current_user: OwnerOrAdminDep, db: DbDep) -> dict:
    """Which rule a message like this would hit, without sending or moving anything."""
    rows = (await db.execute(select(RoutingRule).where(
        RoutingRule.business_id == current_user.business, RoutingRule.active == True)  # noqa: E712
        .order_by(RoutingRule.position, RoutingRule.created_at))).scalars().all()
    from shared.team import language as lang

    language = lang.detect(body.text)
    for rule in rows:
        if team_rules.matches(rule.conditions or {}, text=body.text, channel=body.channel, connection_id=body.number,
                              lead_source=body.lead_source, tags=set(body.tags), language=language):
            team = await db.get(Team, rule.team_id)
            return {"rule_id": str(rule.id), "rule": rule.name, "team": team.name if team else None, "language": language}
    return {"rule_id": None, "rule": None, "team": None, "language": language}

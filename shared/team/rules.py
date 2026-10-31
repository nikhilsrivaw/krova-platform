"""
Routing rules: send a new conversation (or a new lead) to the right team.

A business orders its rules on the Team page; the first one that matches wins. Conditions are plain and free - words in
the message, the language the customer wrote in (shared/team/language.py), the channel, which of the business's
numbers it came on, which lead source it came from, a tag on the customer. Any condition left empty is not checked; a rule with none at all catches everything (a default team).

Inside the team:
  round_robin  the chat goes straight to the team member who was handed one longest ago, skipping anyone Away;
  queue        the chat waits in the team, unassigned, and every member is told - whoever replies first owns it.
Nobody available -> the chat waits in the team and the team lead is told.

Runs on an inbound message for a chat with no owner, and for an owned chat only through a rule marked
"also_if_owned" (a complaint from a sales person's customer). A chat already in the rule's team is left alone, so a
team's chat is not re-routed on every message. When no rule matches, the business-wide behaviour applies as before
(round-robin or first reply - shared/team/routing.py), except for a chat already waiting in a team's queue.

AI first, a person when one is needed. When the AI replies on its own (Act or Conditional autonomy, and always on calls
and the website chat), a message only marks which team the chat belongs to; a person is handed it later by
hand_to_person() - when the AI escalates, when its reply waits for someone's OK, or when the customer has waited past
the reply target. In Draft and Observe every reply needs a person, so a person is handed the chat at once, as before.
A rule marked "person_now" (e.g. complaints) always hands the chat to a person at once.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import BusinessMember, Customer, CustomerTag, RoutingRule, TagStatus, Team, TeamMember, User
from shared.team import conflict, language as lang
from shared.utils.logging import get_logger

logger = get_logger(__name__)

CONDITION_KEYS = ("words", "languages", "channels", "numbers", "lead_sources", "tags")
CHANNELS = ("whatsapp", "instagram", "voice", "web")
SHARING = ("round_robin", "queue")
# Not a condition: a rule switch kept in the same JSON (no column) - hand the chat to a person at once.
PERSON_NOW = "person_now"


def ai_first(autonomy: str | None, channel: str | None) -> bool:
    """Whether the AI answers before any person: it sends its own replies, or the channel is answered live by it."""
    return channel in ("voice", "web") or (autonomy or "").lower() in ("act", "conditional")


def clean_conditions(raw: dict | None) -> dict:
    """Only known keys, as lists of short trimmed strings, empty ones dropped."""
    out: dict[str, list[str]] = {}
    for key in CONDITION_KEYS:
        values = (raw or {}).get(key) or []
        if isinstance(values, str):
            values = [values]
        cleaned = list(dict.fromkeys(str(v).strip()[:60] for v in values if str(v).strip()))[:40]
        if key == "channels":
            cleaned = [v for v in cleaned if v in CHANNELS]
        if key == "languages":
            cleaned = [v for v in cleaned if v in lang.LANGUAGES]
        if cleaned:
            out[key] = cleaned
    if (raw or {}).get(PERSON_NOW) is True:
        out[PERSON_NOW] = True
    return out


def matches(
    conditions: dict, *, text: str | None, channel: str | None, connection_id: uuid.UUID | str | None,
    lead_source: str | None, tags: set[str], language: str | None = None,
) -> bool:
    """Whether a rule's conditions all hold. Pure. Words match anywhere in the message, ignoring case."""
    c = clean_conditions(conditions)
    if "words" in c:
        haystack = f" {(text or '').lower()} "
        if not any(w.lower() in haystack for w in c["words"]):
            return False
    if "languages" in c and (language or lang.detect(text) or "") not in c["languages"]:
        return False
    if "channels" in c and (channel or "") not in c["channels"]:
        return False
    if "numbers" in c and str(connection_id or "") not in c["numbers"]:
        return False
    if "lead_sources" in c and (lead_source or "").lower() not in {s.lower() for s in c["lead_sources"]}:
        return False
    if "tags" in c and not ({t.lower() for t in tags} & {t.lower() for t in c["tags"]}):
        return False
    return True


async def _tags(db: AsyncSession, customer_id: uuid.UUID) -> set[str]:
    rows = await db.execute(select(CustomerTag.label).where(
        CustomerTag.customer_id == customer_id, CustomerTag.status == TagStatus.confirmed))
    return {str(t) for t in rows.scalars().all() if t}


async def route(
    db: AsyncSession, *, business_id: uuid.UUID, customer: Customer, text: str | None, channel: str | None,
    connection_id: uuid.UUID | None = None, lead_source: str | None = None, what: str = "chat",
    assign_people: bool = True,
) -> bool:
    """
    Apply the first matching rule. True when the conversation is now a team's (routed now, or already there), so the
    caller must not hand it out business-wide as well. Without assign_people the chat only joins the team (AI first);
    a rule marked person_now still hands it to a person.
    """
    rules = (await db.execute(
        select(RoutingRule).where(RoutingRule.business_id == business_id, RoutingRule.active == True)  # noqa: E712
        .order_by(RoutingRule.position, RoutingRule.created_at)
    )).scalars().all()
    if not rules:
        return customer.team_id is not None and customer.assigned_to_user_id is None
    tags: set[str] | None = None
    # A call's language comes from speech (saved on the customer); a message's from what it says.
    heard = lang.from_voice(getattr(customer, "preferred_language", None))
    language = (heard or lang.detect(text)) if channel == "voice" else (lang.detect(text) or heard)
    for rule in rules:
        if customer.assigned_to_user_id is not None and not rule.also_if_owned:
            continue
        if "tags" in (rule.conditions or {}) and tags is None:
            tags = await _tags(db, customer.id)
        if not matches(rule.conditions or {}, text=text, channel=channel, connection_id=connection_id,
                       lead_source=lead_source, tags=tags or set(), language=language):
            continue
        if customer.team_id == rule.team_id:
            return True  # already this team's - nothing to move
        team = await db.get(Team, rule.team_id)
        if team is None:
            continue
        await send_to_team(db, business_id=business_id, customer=customer, team=team, what=what,
                           assign=assign_people or (rule.conditions or {}).get(PERSON_NOW) is True)
        logger.info("routed business=%s customer=%s rule=%s team=%s", business_id, customer.id, rule.id, team.id)
        return True
    # No rule: a chat already waiting in a team's queue stays there.
    return customer.team_id is not None and customer.assigned_to_user_id is None


async def available_members(db: AsyncSession, team: Team) -> list[TeamMember]:
    """Team members who can take a chat now (not Away, account active), next-in-turn first."""
    rows = await db.execute(
        select(TeamMember)
        .join(BusinessMember, (BusinessMember.user_id == TeamMember.user_id) & (BusinessMember.business_id == team.business_id))
        .join(User, User.id == TeamMember.user_id)
        .where(TeamMember.team_id == team.id, BusinessMember.available == True, User.is_active == True)  # noqa: E712
        .order_by(TeamMember.last_routed_at.asc().nullsfirst(), TeamMember.id)
    )
    return list(rows.scalars().all())


async def send_to_team(
    db: AsyncSession, *, business_id: uuid.UUID, customer: Customer, team: Team, what: str = "chat",
    assign: bool = True, reason: str | None = None,
) -> None:
    """
    Put the chat in the team. With assign, hand it to a person now, the team's way; without, it only joins the team
    and the AI keeps answering until hand_to_person(). `reason` (why a person is needed) goes in the push.
    """
    previous = customer.assigned_to_user_id
    now = datetime.now(timezone.utc)
    customer.team_id = team.id
    name = customer.display_name or "A customer"
    who = f"{name} - {reason}" if reason else name
    url = f"/app/inbox/{customer.id}"
    label = "lead" if what == "lead" else "chat"
    if not assign:
        customer.assigned_to_user_id, customer.assigned_at = None, None
    elif team.sharing == "round_robin":
        free = await available_members(db, team)
        if free:
            member = free[0]
            member.last_routed_at = now
            customer.assigned_to_user_id, customer.assigned_at = member.user_id, now
            await conflict.notify(db, business_id=business_id, user_id=member.user_id,
                                  title=f"New {label} for you - {team.name}", body=who, url=url, kind="new_chat")
        else:
            customer.assigned_to_user_id, customer.assigned_at = None, None
            await conflict.notify(db, business_id=business_id, user_id=team.lead_user_id,
                                  title=f"Nobody free in {team.name}", body=f"{who} - nobody in the team is available",
                                  url=url, kind="new_chat")
    else:
        customer.assigned_to_user_id, customer.assigned_at = None, None
        members = (await db.execute(select(TeamMember.user_id).where(TeamMember.team_id == team.id))).scalars().all()
        for user_id in members:
            await conflict.notify(db, business_id=business_id, user_id=user_id,
                                  title=f"New {label} in {team.name}", body=f"{who} - reply first to take it", url=url,
                                  kind="new_chat")
    if previous is not None and previous != customer.assigned_to_user_id:
        await conflict.notify(db, business_id=business_id, user_id=previous,
                              title=f"A chat moved to {team.name}", body=f"{name} now goes to the {team.name} team", url=url,
                              kind="new_chat")


async def hand_to_person(db: AsyncSession, *, business_id: uuid.UUID, customer_id: uuid.UUID, reason: str) -> uuid.UUID | None:
    """
    A person is needed now - the AI escalated, its reply waits for an OK, or the customer has waited too long. A chat
    somebody already has stays theirs (the caller tells them). Otherwise the chat's team hands it out its own way; a chat
    in no team goes round-robin to the next available agent (shared/team/routing.py), unless the business assigns chats
    by hand. Returns who has the chat now, or None.
    """
    from shared.team import routing

    customer = await db.get(Customer, customer_id)
    if customer is None or customer.business_id != business_id or customer.is_private:
        return None
    if customer.assigned_to_user_id is not None:
        return customer.assigned_to_user_id
    team = await db.get(Team, customer.team_id) if customer.team_id else None
    if team is not None:
        await send_to_team(db, business_id=business_id, customer=customer, team=team, reason=reason)
        return customer.assigned_to_user_id
    return await routing.route_new_chat(
        db, business_id=business_id, customer_id=customer.id, title="A customer needs you",
        body=f"{customer.display_name or 'A customer'} - {reason}",
    )

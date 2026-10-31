"""
Reply-time reminders: a customer's latest message has sat unanswered past the
business's first-reply target (Business.settings["team"]["sla_minutes"]).

Two steps, each sent once per waiting message:
  1. at the target: a chat nobody owns is first handed to a person (its team, else round-robin - shared/team/rules.py
     hand_to_person); the chat's owner is pinged (everyone, if still nobody), and the team lead too when the chat
     belongs to a team (shared/team/rules.py);
  2. at twice the target: the owner and admins are pinged too.

A chat in a team uses the team's own reply target when it has one, else the business's.

A chat counts as waiting when its newest message is inbound on a text channel
(WhatsApp / Instagram) and is under a day old - older than that the 24-hour
window is closed and a reminder would only be noise. Replying, by anyone or by
the AI, ends the wait because the newest message is then outbound.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.care import post_call_actions
from shared.db.models import Business, BusinessMember, BusinessRole, Channel, Customer, Direction, Message, Team
from shared.integrations import web_push
from shared.team import rules as team_rules, settings as team_settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

MAX_AGE = timedelta(hours=23)
TEXT_CHANNELS = (Channel.whatsapp, Channel.instagram)


def stage_for(waited: timedelta, target_minutes: int, alerted_at, escalated_at, inbound_at) -> str | None:
    """'owner', 'supervisors' or None - pure, so the timing rule is testable."""
    target = timedelta(minutes=target_minutes)
    if waited >= 2 * target and (escalated_at is None or escalated_at < inbound_at):
        return "supervisors"
    if waited >= target and (alerted_at is None or alerted_at < inbound_at):
        return "owner"
    return None


async def _supervisors(db: AsyncSession, business_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await db.execute(
        select(BusinessMember.user_id).where(
            BusinessMember.business_id == business_id,
            BusinessMember.role.in_((BusinessRole.owner, BusinessRole.admin)),
        )
    )
    return list(rows.scalars().all())


async def check_reply_times(db: AsyncSession) -> int:
    """One sweep over every business that set a target. Returns how many reminders went out."""
    now = datetime.now(timezone.utc)
    sent = 0
    businesses = (await db.execute(select(Business).where(Business.is_active == True))).scalars().all()  # noqa: E712
    for business in businesses:
        business_target = team_settings.read(business)["sla_minutes"]
        teams = {t.id: t for t in (await db.execute(select(Team).where(Team.business_id == business.id))).scalars().all()}
        if not business_target and not any(t.reply_target_minutes for t in teams.values()):
            continue
        # The newest message of each recently-active chat.
        customers = (await db.execute(
            select(Customer).where(
                Customer.business_id == business.id,
                Customer.last_contact_at.is_not(None),
                Customer.last_contact_at > now - MAX_AGE,
            )
        )).scalars().all()
        for customer in customers:
            latest = (await db.execute(
                select(Message).where(Message.customer_id == customer.id)
                .order_by(Message.occurred_at.desc()).limit(1)
            )).scalars().first()
            if latest is None or latest.direction != Direction.inbound or latest.channel not in TEXT_CHANNELS:
                continue
            team = teams.get(customer.team_id) if customer.team_id else None
            target = (team.reply_target_minutes if team and team.reply_target_minutes else None) or business_target
            if not target:
                continue
            inbound_at = latest.occurred_at if latest.occurred_at.tzinfo else latest.occurred_at.replace(tzinfo=timezone.utc)
            stage = stage_for(
                now - inbound_at, target,
                customer.sla_alerted_at, customer.sla_escalated_at, inbound_at,
            )
            if stage is None:
                continue
            name = customer.display_name or "A customer"
            payload = {"title": "Customer waiting", "body": f"{name} has waited {target}+ min for a reply",
                       "url": f"/app/inbox/{customer.id}"}
            try:
                if stage == "owner":
                    customer.sla_alerted_at = now
                    # The business's own automations hear about it too (first alert only).
                    try:
                        await post_call_actions.apply_rules(
                            db, business_id=business.id, trigger_type="reply.overdue",
                            customer_id=customer.id, channel=latest.channel.value,
                            context={
                                "minutes_waiting": int((now - inbound_at).total_seconds() // 60),
                                "assigned": customer.assigned_to_user_id is not None,
                            },
                        )
                    except Exception:
                        logger.exception("reply.overdue automations failed business=%s", business.id)
                    # AI first left it with nobody (shared/team/rules.py) - a person takes it now.
                    if customer.assigned_to_user_id is None:
                        try:
                            async with db.begin_nested():
                                got = await team_rules.hand_to_person(
                                    db, business_id=business.id, customer_id=customer.id,
                                    reason=f"waited {target}+ min for a reply")
                            if got:
                                customer.assigned_to_user_id = got
                        except Exception:
                            logger.exception("reply-time hand-off failed business=%s", business.id)
                    if team is not None:
                        payload["body"] += f" ({team.name} team)"
                        people = {u for u in (customer.assigned_to_user_id, team.lead_user_id) if u}
                        if people:
                            await web_push.send_to_users(
                                db, business_id=business.id, user_ids=list(people), payload=payload, kind="slow_reply")
                        else:
                            await web_push.send_to_business(db, business_id=business.id, payload=payload, kind="slow_reply")
                    elif customer.assigned_to_user_id:
                        await web_push.send_to_users(
                            db, business_id=business.id, user_ids=[customer.assigned_to_user_id], payload=payload,
                            kind="slow_reply")
                    else:
                        await web_push.send_to_business(db, business_id=business.id, payload=payload, kind="slow_reply")
                else:
                    customer.sla_alerted_at = customer.sla_escalated_at = now
                    payload["title"] = "Customer still waiting"
                    ids = set(await _supervisors(db, business.id))
                    if customer.assigned_to_user_id:
                        ids.add(customer.assigned_to_user_id)
                    if team is not None and team.lead_user_id:
                        ids.add(team.lead_user_id)
                    await web_push.send_to_users(
                        db, business_id=business.id, user_ids=list(ids), payload=payload, kind="slow_reply")
                sent += 1
            except Exception:
                logger.exception("reply-time reminder failed business=%s", business.id)
    return sent

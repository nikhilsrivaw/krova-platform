"""
What each person on the team has done - the owner's view of the activity log.

Owner and admin only: this is one person's record of another's work, and it
includes refused attempts and where they signed in from.

Two reads over the same table (shared/db/models/activity.py):
  GET /team/activity          the feed, newest first, filterable, paged
  GET /team/activity/summary  one line per person: what they have been doing
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import and_, desc, func, or_, select

from services.api.dependencies import DbDep, OwnerOrAdminDep
from shared.audit import activity
from shared.db.models import ActivityLog, BusinessMember, User

router = APIRouter(prefix="/team/activity", tags=["team"])


class ActivityOut(BaseModel):
    id: str
    user_id: str | None
    user_label: str
    role: str | None
    action: str
    kind: str  # work | config | security
    summary: str
    outcome: str  # ok | denied | failed
    detail: dict
    ip: str | None
    user_agent: str | None
    occurred_at: str


class ActivityPage(BaseModel):
    items: list[ActivityOut]
    # Pass back as `before` for the next page; None when there is no more.
    next_before: str | None


def _out(row: ActivityLog) -> ActivityOut:
    return ActivityOut(
        id=str(row.id), user_id=str(row.user_id) if row.user_id else None,
        user_label=row.user_label, role=row.role, action=row.action,
        kind=activity.kind_of(row.action), summary=row.summary, outcome=row.outcome,
        detail=row.detail or {}, ip=row.ip, user_agent=row.user_agent,
        occurred_at=row.occurred_at.isoformat(),
    )


@router.get("", response_model=ActivityPage)
async def feed(
    current_user: OwnerOrAdminDep,
    db: DbDep,
    user_id: uuid.UUID | None = Query(default=None),
    action: str | None = Query(default=None),
    kind: str | None = Query(default=None, pattern="^(work|config|security)$"),
    outcome: str | None = Query(default=None, pattern="^(ok|denied|failed)$"),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    before: str | None = Query(default=None, description="Cursor from the previous page"),
    limit: int = Query(default=50, ge=1, le=200),
) -> ActivityPage:
    conditions = [ActivityLog.business_id == current_user.business]
    if user_id:
        conditions.append(ActivityLog.user_id == user_id)
    if action:
        conditions.append(ActivityLog.action == action)
    if kind:
        conditions.append(ActivityLog.action.in_(activity.actions_of_kind(kind)))
    if outcome:
        conditions.append(ActivityLog.outcome == outcome)
    if since:
        conditions.append(ActivityLog.occurred_at >= since)
    if until:
        conditions.append(ActivityLog.occurred_at <= until)
    if before:
        # "<time>|<id>": rows older than that, ties broken by id so a page
        # boundary never skips or repeats a row written in the same instant.
        stamp, _, row_id = before.partition("|")
        cutoff = datetime.fromisoformat(stamp)
        conditions.append(or_(
            ActivityLog.occurred_at < cutoff,
            and_(ActivityLog.occurred_at == cutoff, ActivityLog.id < uuid.UUID(row_id)),
        ))

    rows = (
        await db.execute(
            select(ActivityLog)
            .where(*conditions)
            .order_by(desc(ActivityLog.occurred_at), desc(ActivityLog.id))
            .limit(limit + 1)
        )
    ).scalars().all()

    more = len(rows) > limit
    rows = rows[:limit]
    next_before = f"{rows[-1].occurred_at.isoformat()}|{rows[-1].id}" if more and rows else None
    return ActivityPage(items=[_out(r) for r in rows], next_before=next_before)


class MemberSummary(BaseModel):
    user_id: str | None
    name: str
    role: str | None
    last_active_at: str | None
    messages_sent: int
    drafts_approved: int
    drafts_rejected: int
    escalations_handled: int
    conversations_assigned: int
    config_changes: int
    data_exports: int
    sign_ins: int
    refused_attempts: int


class SummaryOut(BaseModel):
    days: int
    members: list[MemberSummary]


# Which action keys feed which column. Anything else still shows in the feed.
_COLUMNS = {
    "messages_sent": {"message_sent", "carousel_sent", "flow_sent"},
    "drafts_approved": {"draft_approved"},
    "drafts_rejected": {"draft_rejected"},
    "escalations_handled": {"escalation_updated"},
    "conversations_assigned": {"conversation_assigned"},
    "data_exports": {"data_export"},
    "sign_ins": {"login"},
}


def summarise(rows: list[tuple], people: dict[uuid.UUID, tuple[str, str | None]]) -> list[MemberSummary]:
    """
    `rows` are (user_id, action, outcome, count, last_at) groups; `people` maps
    user id -> (name, role) for everyone currently on the team. Anyone on the
    team appears, even with nothing done; someone who has since left the team
    (their rows kept, user_id gone) is not guessed at.
    """
    table: dict[uuid.UUID, dict] = {
        uid: {
            "name": name, "role": role, "last": None,
            **{col: 0 for col in _COLUMNS}, "config_changes": 0, "refused_attempts": 0,
        }
        for uid, (name, role) in people.items()
    }
    for user_id, action, outcome, count, last_at in rows:
        entry = table.get(user_id)
        if entry is None:
            continue
        if last_at and (entry["last"] is None or last_at > entry["last"]):
            entry["last"] = last_at
        if outcome == "denied":
            entry["refused_attempts"] += count
            continue
        if outcome != "ok":
            continue
        for col, actions in _COLUMNS.items():
            if action in actions:
                entry[col] += count
        if activity.kind_of(action) == activity.CONFIG:
            entry["config_changes"] += count

    return [
        MemberSummary(
            user_id=str(uid), name=e["name"], role=e["role"],
            last_active_at=e["last"].isoformat() if e["last"] else None,
            **{col: e[col] for col in (*_COLUMNS, "config_changes", "refused_attempts")},
        )
        for uid, e in sorted(table.items(), key=lambda kv: (kv[1]["last"] is None, -(kv[1]["last"].timestamp() if kv[1]["last"] else 0)))
    ]


@router.get("/summary", response_model=SummaryOut)
async def summary(
    current_user: OwnerOrAdminDep,
    db: DbDep,
    days: int = Query(default=7, ge=1, le=90),
) -> SummaryOut:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    members = (
        await db.execute(
            select(BusinessMember, User)
            .join(User, User.id == BusinessMember.user_id)
            .where(BusinessMember.business_id == current_user.business, User.is_active == True)  # noqa: E712
        )
    ).all()
    people = {
        member.user_id: (user.full_name or user.email or user.phone or "Unknown",
                         member.role.value if hasattr(member.role, "value") else str(member.role))
        for member, user in members
    }

    rows = (
        await db.execute(
            select(
                ActivityLog.user_id, ActivityLog.action, ActivityLog.outcome,
                func.count(), func.max(ActivityLog.occurred_at),
            )
            .where(ActivityLog.business_id == current_user.business, ActivityLog.occurred_at >= since)
            .group_by(ActivityLog.user_id, ActivityLog.action, ActivityLog.outcome)
        )
    ).all()
    return SummaryOut(days=days, members=summarise([tuple(r) for r in rows], people))

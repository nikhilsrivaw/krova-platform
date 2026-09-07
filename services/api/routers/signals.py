"""
Where a founder actually reads what shared/ai/signals.py found: bugs,
feature requests, complaints, churn risk, praise - Insight rows that existed
in the schema with nowhere to be read until this.

The file-github-issue action (software-startup vertical) is the one
consequential, external-write endpoint here - staff-triggered on purpose,
never fired by the agent itself. See shared/integrations/github.py's own
docstring for why.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import decrypt
from shared.db.models import Commitment, CommitmentDirection, CommitmentKind, CommitmentStatus, GitHubConnection, Insight
from shared.integrations import github as github_module
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


class SignalOut(BaseModel):
    id: str
    customer_id: str | None
    kind: str
    title: str
    body: str | None
    severity: str
    created_at: datetime
    dismissed_at: datetime | None


def _out(i: Insight) -> SignalOut:
    return SignalOut(
        id=str(i.id), customer_id=str(i.customer_id) if i.customer_id else None,
        kind=i.kind, title=i.title, body=i.body, severity=i.severity,
        created_at=i.created_at, dismissed_at=i.dismissed_at,
    )


@router.get("", response_model=list[SignalOut])
async def list_signals(
    current_user: CurrentUserDep,
    db: DbDep,
    kind: str | None = None,
    severity: str | None = None,
    include_dismissed: bool = Query(default=False),
) -> list[SignalOut]:
    query = select(Insight).where(Insight.business_id == current_user.business)
    if kind:
        query = query.where(Insight.kind == kind)
    if severity:
        query = query.where(Insight.severity == severity)
    if not include_dismissed:
        query = query.where(Insight.dismissed_at.is_(None))
    query = query.order_by(Insight.created_at.desc())

    rows = await db.execute(query)
    return [_out(i) for i in rows.scalars().all()]


@router.post("/{signal_id}/dismiss", response_model=SignalOut)
async def dismiss_signal(signal_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> SignalOut:
    signal = await db.get(Insight, signal_id)
    if signal is None or signal.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signal not found")

    signal.dismissed_at = datetime.now(timezone.utc)
    await db.flush()
    return _out(signal)


class FiledIssueOut(BaseModel):
    signal: SignalOut
    github_issue_url: str
    commitment_id: str


@router.post("/{signal_id}/file-github-issue", response_model=FiledIssueOut)
async def file_github_issue(signal_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> FiledIssueOut:
    """
    Turn a bug-kind Insight into a real GitHub issue, and a linked
    Commitment(kind=bug_fix) that tracks it through to close (see
    services/api/routers/webhooks.py's GitHub receiver). Staff-triggered,
    never automatic - the agent proposes by creating the Insight in the
    first place; a person decides whether it's worth a real issue.
    """
    signal = await db.get(Insight, signal_id)
    if signal is None or signal.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signal not found")
    if signal.kind != "bug":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Only a bug signal can be filed as a GitHub issue")
    if signal.customer_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This signal has no customer to notify once fixed")

    connection = (
        await db.execute(select(GitHubConnection).where(GitHubConnection.business_id == current_user.business))
    ).scalar_one_or_none()
    if connection is None or connection.status.value != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Connect a GitHub repo in Settings first")

    try:
        issue = await github_module.create_issue(
            access_token=decrypt(connection.access_token),
            repo_owner=connection.repo_owner,
            repo_name=connection.repo_name,
            title=signal.title,
            # Never invented text - the cited evidence is the body, same
            # as everything else this Insight already shows a founder.
            body=signal.body or "(no additional detail captured)",
        )
    except github_module.GitHubError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    commitment = Commitment(
        business_id=current_user.business,
        customer_id=signal.customer_id,
        direction=CommitmentDirection.we_owe,
        kind=CommitmentKind.bug_fix,
        description=signal.title,
        status=CommitmentStatus.open,
        source_message_ids=signal.source_message_ids,
        github_issue_url=issue.html_url,
    )
    db.add(commitment)
    signal.dismissed_at = datetime.now(timezone.utc)
    await db.flush()

    logger.info(
        "github issue filed business=%s signal=%s issue=%s commitment=%s",
        current_user.business, signal.id, issue.html_url, commitment.id,
    )
    return FiledIssueOut(signal=_out(signal), github_issue_url=issue.html_url, commitment_id=str(commitment.id))

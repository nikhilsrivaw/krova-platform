"""
Teams of staff, and the rules that send a conversation to one.

A business makes teams - Complaints, Sales, Accounts, a branch - and ordered routing rules: "when a new conversation
matches these conditions, send it to this team". The first rule that matches wins; inside the team the chat goes to
the next available member (round-robin) or waits in the team's queue for anyone in it to pick up. See
shared/team/rules.py and docs/new/team-routing-research.md.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin


class Team(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "teams"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    # Told first when the team's reply target slips, and when a chat reaches the team with nobody available.
    lead_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Minutes; None = the business's own target (Team page).
    reply_target_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "round_robin": straight to the next available member. "queue": the chat waits in the team for anyone to take.
    sharing: Mapped[str] = mapped_column(String(20), nullable=False, default="round_robin")

    __table_args__ = (
        UniqueConstraint("business_id", "name", name="uq_team_name"),
        Index("idx_teams_business", "business_id"),
    )


class TeamMember(UUIDMixin, Base):
    __tablename__ = "team_members"

    team_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # When this person was last handed a chat by this team - who is next in its round-robin.
    last_routed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_member"),)


class RoutingRule(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "routing_rules"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # All that are set must hold: {"words": [...], "channels": [...], "numbers": [connection ids],
    # "lead_sources": [...], "tags": [...]}. Within one list, any one value matches.
    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    team_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    # Move the chat even when someone already has it (a complaint from a sales person's customer).
    also_if_owned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("idx_routing_rules_business", "business_id", "position"),)


class TeamRequest(UUIDMixin, TimestampMixin, Base):
    """
    A customer asked to talk to the team - on a call, WhatsApp, Instagram or the website chat - or a missed call is
    owed a call back. It waits on the "Talk to team" page for whoever is free; that person takes it, talks to the
    customer (call, chat) with the whole story in the side panel, and closes it with a note that stays on the customer.
    See shared/team/requests.py.
    """
    __tablename__ = "team_requests"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    # Where they asked: "voice", "whatsapp", "instagram", "web".
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # What they want, in their own words (several asks while it is open are kept together).
    what: Mapped[str] = mapped_column(String(2000), nullable=False)
    # "customer" (they asked), "missed_call" (nobody picked up), "staff" (added by hand).
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="customer")
    # "open" -> "taken" -> "done" (or "cancelled").
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    # The chat's team when it has one - its members are told first.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    taken_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # When the "still waiting" reminder went out - once per request.
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_team_requests_business_status", "business_id", "status"),
        Index("idx_team_requests_customer", "customer_id"),
    )

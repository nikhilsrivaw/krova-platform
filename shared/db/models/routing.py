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


class FeedbackRequest(UUIDMixin, TimestampMixin, Base):
    """
    Asking one customer how one visit, token, order or chat went - on WhatsApp (three buttons), by call (a short survey
    call), or WhatsApp first and a call if they do not answer - and their answer. See shared/care/feedback.py.
    """
    __tablename__ = "feedback_requests"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    # What it is about: "visit" (a booking), "token" (a walk-in), "order" (delivered), "chat" (resolved) - and the
    # id of that thing, so one is never asked about twice.
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    ref: Mapped[str] = mapped_column(String(64), nullable=False)
    # In words, for the message and the page: "visit on 2 Nov", "order #1042".
    about: Mapped[str] = mapped_column(String(200), nullable=False)
    # "whatsapp", "voice" or "both" (WhatsApp, then a call if no answer by call_at).
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    # "due" -> "sent" / "calling" -> "answered"; or "no_reply" / "skipped" (with `note` saying why).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="due")
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    call_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 1 to 5. WhatsApp's buttons give 5 / 3 / 1; a call gives what the customer said.
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    answered_via: Mapped[str | None] = mapped_column(String(10), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # After "Okay" or "Not good" on WhatsApp the next message they type is kept as the comment.
    awaiting_comment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Rounds: how many times they have been asked so far, and when the next round (or giving up) is due.
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "auto" (KROVA saw it finish), "upload" / "paste" / "form" (added by hand), "api".
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="auto")
    # When the thing itself happened - the trip, the visit - for a request added by hand.
    happened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("business_id", "trigger", "ref", name="uq_feedback_request_ref"),
        Index("idx_feedback_requests_next", "status", "next_at"),
        Index("idx_feedback_requests_due", "status", "send_at"),
        Index("idx_feedback_requests_business", "business_id", "created_at"),
        Index("idx_feedback_requests_customer", "customer_id"),
    )

"""
Who uses Krova, and who they talk to.

Two different kinds of person live here and they must not be confused:

  users      - people who log into Krova (the business owner and their staff)
  customers  - the people those businesses talk to, across every channel

A customer is one human. The handles they reach you by - a phone number, an
email address, an Instagram ID - live in customer_identities, one row each.
That is what lets someone who WhatsApps on Monday and phones on Wednesday be
recognised as the same person, which is the whole basis of cross-channel
memory. Nullable columns on customers could never express it: a person can
have two phone numbers, and two people can share one.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class BusinessRole(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    agent = "agent"


class IdentityKind(str, enum.Enum):
    """How a customer can be addressed. Also the join key across channels."""

    phone = "phone"          # E.164. Shared by WhatsApp and voice - the free win.
    email = "email"          # lowercased
    instagram = "instagram"  # Instagram-scoped user id
    whatsapp = "whatsapp"    # wa_id, when it differs from the phone number


# ── People who log in ────────────────────────────────────────────────────────

class User(UUIDMixin, TimestampMixin, Base):
    """
    A person who signs into Krova.

    Auth is ours: we hash the password and issue the token. There is no
    external identity provider and no external id to reconcile.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    memberships: Mapped[list["BusinessMember"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(UUIDMixin, Base):
    """
    One row per issued refresh token, so sessions can actually be revoked.

    We store a hash, never the token: a leaked database should not hand
    someone a working session.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (Index("idx_refresh_tokens_user", "user_id"),)


# ── The tenant ───────────────────────────────────────────────────────────────

class Business(UUIDMixin, TimestampMixin, Base):
    """
    The tenant. `business_id` is the only tenancy key in this system - the API,
    the workers and the voice service all scope by it, with no second notion of
    a tenant anywhere.
    """

    __tablename__ = "businesses"

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Which vertical template seeded this business: clinic, restaurant, salon,
    # general. Drives prompts, flows, template set and dashboard defaults, and
    # is data rather than code so a new vertical is a config file.
    vertical: Mapped[str] = mapped_column(String(50), nullable=False, default="general")

    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Kolkata"
    )
    plan: Mapped[str] = mapped_column(String(20), nullable=False, default="trial")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # How much the agent may do without a human. Deliberately explicit: the
    # public promise is human-in-the-loop, so autonomy is a stored setting we
    # can audit, not a branch someone can quietly flip in code.
    autonomy: Mapped[str] = mapped_column(
        String(20), nullable=False, default="observe"  # observe | draft | act
    )

    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    members: Mapped[list["BusinessMember"]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )


class BusinessMember(UUIDMixin, TimestampMixin, Base):
    """Which users can act for which business, and in what role."""

    __tablename__ = "business_members"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[BusinessRole] = mapped_column(
        EnumType(BusinessRole, 20), nullable=False, default=BusinessRole.owner
    )

    business: Mapped["Business"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("business_id", "user_id", name="uq_business_member"),
        Index("idx_business_members_user", "user_id"),
    )


# ── The people businesses talk to ────────────────────────────────────────────

class Customer(UUIDMixin, TimestampMixin, Base):
    """
    One human, however many channels they use.

    Carries no phone or email of its own - those are identities, and putting
    them here would cap each customer at one of each.
    """

    __tablename__ = "customers"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )

    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_contact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set by the owner. The agent must never touch a customer marked private -
    # this is what makes "that thread is personal" enforceable rather than a
    # promise, and it is why we can read a mixed Instagram inbox at all.
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    identities: Mapped[list["CustomerIdentity"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_customers_business", "business_id"),
        Index("idx_customers_business_last_contact", "business_id", "last_contact_at"),
    )


class CustomerIdentity(UUIDMixin, Base):
    """
    One handle a customer can be reached by.

    The unique constraint on (business_id, kind, value) is what makes lookup
    from an inbound webhook a single indexed read, and what stops the same
    number becoming two customers under a race.

    `confidence` exists because merging is not always certain. A phone number
    arriving on a WhatsApp webhook is exact. An email address inferred from a
    call transcript is a guess - it gets a low score and waits for a human to
    confirm rather than silently merging two people's histories.
    """

    __tablename__ = "customer_identities"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )

    kind: Mapped[IdentityKind] = mapped_column(EnumType(IdentityKind, 20), nullable=False)
    # Normalised before storage: E.164 for phones, lowercased for email.
    value: Mapped[str] = mapped_column(String(320), nullable=False)

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # True once a human confirmed it, or once it arrived from a channel that
    # proves it (a WhatsApp webhook proves the phone number).
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="identities")

    __table_args__ = (
        UniqueConstraint("business_id", "kind", "value", name="uq_identity_per_business"),
        Index("idx_identities_customer", "customer_id"),
    )

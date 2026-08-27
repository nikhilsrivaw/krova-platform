"""
Connected channels, and everything said through them.

Two ideas carry this file.

First, a channel connection is a row, not a bag of keys on the business.
Storing a WhatsApp token as businesses.metadata["whatsapp_access_token"] works
until a business connects two numbers, or an Instagram account as well, or you
need to know which credential expires on Thursday. A table makes expiry,
status and re-authorisation ordinary queries instead of JSON archaeology.

Second, every channel writes into one messages table - WhatsApp, Instagram,
email and voice alike. A phone call's turns are messages with channel=voice.
That single decision is what makes commitment extraction, the customer
timeline and cross-channel memory work on voice with no extra code, rather
than being written a second time for calls.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class Channel(str, enum.Enum):
    whatsapp = "whatsapp"
    instagram = "instagram"
    email = "email"
    voice = "voice"


class ConnectionStatus(str, enum.Enum):
    active = "active"
    # Credentials expired or were revoked. The owner must reconnect, and we
    # should be telling them before their customers notice.
    needs_reauth = "needs_reauth"
    disconnected = "disconnected"


class Direction(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class ChannelConnection(UUIDMixin, TimestampMixin, Base):
    """
    One connected account on one channel.

    Token expiry is a first-class column because of how it fails: WhatsApp
    business tokens last 60 days, and a business that onboarded in March goes
    silent in May with no error anywhere. Every client fails on their own
    anniversary, so it never looks like a systemic bug. A column here is what
    the refresh job reads.
    """

    __tablename__ = "channel_connections"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[Channel] = mapped_column(EnumType(Channel, 20), nullable=False)

    # WABA id, Instagram user id, mailbox address, or phone number id -
    # whatever this channel calls the account we were given access to.
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The address customers actually see: +91 98765 43210, @salon, hello@shop.in
    external_handle: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Encrypted at rest. Never logged, never returned by the API.
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    token_refresh_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[ConnectionStatus] = mapped_column(
        EnumType(ConnectionStatus, 20), nullable=False, default=ConnectionStatus.active
    )

    # WhatsApp: whether subscribed_apps succeeded and the number is registered.
    # Both fail quietly - an unsubscribed WABA simply delivers nothing - so we
    # record them rather than assume them.
    webhook_subscribed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    number_registered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Only ever read forward from here. Email is the exception: it is the one
    # channel with real history, and backfilling it is what lets a new customer
    # see months of forgotten commitments in their first few minutes.
    connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    backfilled_through: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "business_id", "channel", "external_account_id", name="uq_channel_account"
        ),
        Index("idx_connections_business", "business_id"),
        # The refresh job's query: which credentials expire soon.
        Index("idx_connections_expiry", "status", "token_expires_at"),
    )


class Message(UUIDMixin, Base):
    """
    One thing that was said, on any channel, in either direction.

    external_id is unique per business so a webhook Meta retries three times
    creates one row. Meta does retry, and a duplicate here means replying to
    the same customer twice.
    """

    __tablename__ = "messages"

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
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("channel_connections.id", ondelete="SET NULL"),
        nullable=True,
    )

    channel: Mapped[Channel] = mapped_column(EnumType(Channel, 20), nullable=False)
    direction: Mapped[Direction] = mapped_column(EnumType(Direction, 10), nullable=False)

    # The channel's own id for this message. Idempotency depends on it.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)  # email
    media: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # When it was actually said, per the channel - not when we stored it.
    # Backfilled email is months older than its row, and ordering a customer's
    # timeline by created_at would put their whole history in the wrong place.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Whether the cold path has read this yet.
    analysed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Kept deliberately. When a channel changes its payload shape, or we want
    # to run a better extractor over old conversations, replay is only possible
    # if the original is still here.
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Who actually sent this, when a person did. Null for every inbound
    # message, for anything the AI sent unreviewed, and for a bulk campaign
    # send - only set when a specific team member is the reason this exists,
    # which is what makes per-agent response-time analytics honest rather
    # than attributing automated sends to whoever happened to be logged in.
    sent_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("business_id", "external_id", name="uq_message_external_id"),
        Index("idx_messages_timeline", "business_id", "customer_id", "occurred_at"),
        Index("idx_messages_unanalysed", "business_id", "analysed_at"),
        Index("idx_messages_channel", "business_id", "channel", "occurred_at"),
        Index("idx_messages_sent_by", "business_id", "sent_by_user_id"),
    )


class Call(UUIDMixin, Base):
    """
    The call-specific half of a voice conversation.

    What was said lives in messages, like every other channel. This holds only
    what is true of calls and nothing else: duration, cost, why it ended.
    """

    __tablename__ = "calls"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("channel_connections.id", ondelete="SET NULL"),
        nullable=True,
    )

    direction: Mapped[Direction] = mapped_column(EnumType(Direction, 10), nullable=False)
    # Plivo call uuid, or the WhatsApp call id.
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # plivo | whatsapp - the same agent serves both, at very different costs.
    transport: Mapped[str] = mapped_column(String(20), nullable=False, default="plivo")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hangup_cause: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Integer paise, never floats. Money that rounds is money that argues.
    cost_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # True when the agent could not help. The reason is what compounds: twenty
    # of these show the owner exactly which gap to close.
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("business_id", "external_id", name="uq_call_external_id"),
        Index("idx_calls_business_started", "business_id", "started_at"),
    )


class VoiceProvisioningStatus(str, enum.Enum):
    subaccount_created = "subaccount_created"
    compliance_submitted = "compliance_submitted"
    compliance_approved = "compliance_approved"
    compliance_rejected = "compliance_rejected"


class VoiceProvisioning(UUIDMixin, TimestampMixin, Base):
    """
    A business's slice of Plivo - subaccount and KYC - before any number.

    Precedes ChannelConnection on purpose: a number cannot be bought for a
    business until its subaccount exists and Plivo has approved its
    compliance application, so this tracks state that has nowhere else to
    live. ChannelConnection implies an already-working number; everything
    here happens before that is true.

    Reseller compliance is per end-customer business, not inherited from
    Krova's own - confirmed directly with Plivo after an early test looked
    like it inherited the parent's KYC only because no requirement id was
    given and Plivo silently defaulted to the most recent accepted one.
    """

    __tablename__ = "voice_provisioning"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # The subaccount identifier is not secret (like a WABA id); the token is
    # (like a password), so only the token is encrypted at rest.
    subaccount_auth_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subaccount_auth_token: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[VoiceProvisioningStatus] = mapped_column(
        EnumType(VoiceProvisioningStatus, 30),
        nullable=False,
        default=VoiceProvisioningStatus.subaccount_created,
    )

    end_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    compliance_requirement_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    compliance_application_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Plivo's own status vocabulary (pending-review, approved, rejected, ...)
    # - kept as their raw string rather than mapped onto our enum, since it
    # is theirs to change without us guessing at every value in advance.
    compliance_raw_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    compliance_rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # [{"document_id": ..., "document_type_id": ..., "alias": ...}, ...]
    documents: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (Index("idx_voice_provisioning_business", "business_id"),)

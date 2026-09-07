"""
Outbound integrations: things Krova pushes data out to, rather than a
channel a customer talks through.

Two ideas, mirrored from shared/db/models/channel.py's own reasoning for
why ChannelConnection is a table and not JSON-on-Business - but living in
their own file, not that one, because neither is a messaging channel tied
to a Message row the way WhatsApp/Instagram/email are.

CalendarConnection is one business's link to their own Google Calendar -
KROVA never reads it, only writes appointment/token events into it.
OutboundWebhook is a business's own endpoint (their Zapier catch-hook,
their CRM, their own server) that gets a signed POST whenever a booking
event happens, so any tool a business already uses can subscribe without
Krova building a bespoke integration for each one.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.models.channel import ConnectionStatus
from shared.db.types import EnumType


class CalendarConnection(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "calendar_connections"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # Free text, not an enum - "google" today, no migration needed to add a
    # second provider later.
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="google")
    external_calendar_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Encrypted at rest, same convention as ChannelConnection.
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[ConnectionStatus] = mapped_column(
        EnumType(ConnectionStatus, 20), nullable=False, default=ConnectionStatus.active
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("business_id", "provider", name="uq_calendar_connection_provider"),
        Index("idx_calendar_connections_business", "business_id"),
    )


class GitHubConnection(UUIDMixin, TimestampMixin, Base):
    """
    A business's own GitHub repo, for the software-startup vertical's
    closed bug-lifecycle loop (shared/integrations/github.py). Not a
    customer-facing channel - nobody chats with GitHub - this plays the
    same "backend system of record KROVA listens to for the truth" role
    Shiprocket already plays for D2C delivery status.

    v1 is one PAT, one repo per business - the same "start with what's
    confirmed simple" reasoning Shiprocket's login-based auth used over
    a bigger OAuth app. A real GitHub App/multi-repo flow is a bigger,
    separate effort, not built here.
    """

    __tablename__ = "github_connections"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    repo_owner: Mapped[str] = mapped_column(String(255), nullable=False)
    repo_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Encrypted at rest, same convention as ChannelConnection.access_token.
    # Write direction: creating an issue (shared/integrations/github.py).
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    # Receive direction: verifying the issues-closed webhook
    # (services/api/routers/webhooks.py). The business creates this
    # webhook themselves in their own repo's settings (GitHub has no
    # self-serve "generate a secret for me" flow the way Shopify's app
    # install does) and pastes the secret they chose here - same
    # StoreConnection.webhook_secret precedent, entered rather than
    # generated.
    webhook_secret: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[ConnectionStatus] = mapped_column(
        EnumType(ConnectionStatus, 20), nullable=False, default=ConnectionStatus.active
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("business_id", name="uq_github_connection_per_business"),
        Index("idx_github_connections_business", "business_id"),
    )


class EmailSendConnection(UUIDMixin, TimestampMixin, Base):
    """
    A business's own verified send-from address, routed through Krova's
    one shared Postmark account (settings.postmark_server_token) rather
    than a per-business OAuth mailbox connection - see shared/
    integrations/postmark.py's own module docstring for why: Postmark
    does per-customer reputation isolation natively, which is what a
    multi-tenant platform actually needs, and a sender-signature (one
    confirmed address, no DNS changes) is the honest v1 versus a full
    domain/DKIM setup.
    """

    __tablename__ = "email_send_connections"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Postmark's own id for this sender signature - what verified-status
    # polling and every send call key off, not the raw email string.
    postmark_signature_id: Mapped[str] = mapped_column(String(50), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("business_id", name="uq_email_send_connection_per_business"),
        Index("idx_email_send_connections_business", "business_id"),
    )


class StripeConnection(UUIDMixin, TimestampMixin, Base):
    """
    A business's own Stripe webhook endpoint secret - for the billing-
    dunning half of the software-startup vertical (shared/integrations/
    stripe_client.py). Receive-only: this never calls Stripe's own API,
    only verifies an inbound webhook, so there's no API key to store here
    yet - same "receive-only, no API token needed yet" reasoning
    StoreConnection's own docstring already used for Shopify.

    The business creates the webhook endpoint themselves in their own
    Stripe Dashboard, pointed at Krova's own per-business URL
    (/webhooks/stripe/{webhook_token} - unlike Shopify's shop-domain
    header or GitHub's repository name, a Stripe webhook payload carries
    no reliable per-tenant identifier at all, so the URL itself is the
    lookup key, generated server-side same as OutboundWebhook.secret -
    a direct, O(1) lookup rather than GitHub's parse-then-verify two-step
    or trying every connection's secret in turn), and pastes the signing
    secret Stripe shows them for that endpoint - entered, not generated,
    the same shape as GitHubConnection.webhook_secret.
    """

    __tablename__ = "stripe_connections"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # Generated server-side (secrets.token_urlsafe), never chosen by the
    # caller - what makes the webhook URL itself the per-business lookup.
    webhook_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Encrypted at rest, same convention as every other secret in this file.
    webhook_secret: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[ConnectionStatus] = mapped_column(
        EnumType(ConnectionStatus, 20), nullable=False, default=ConnectionStatus.active
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("business_id", name="uq_stripe_connection_per_business"),
        Index("idx_stripe_connections_business", "business_id"),
        Index("idx_stripe_connections_token", "webhook_token"),
    )


class WebhookEventType(str, enum.Enum):
    appointment_booked = "appointment.booked"
    appointment_cancelled = "appointment.cancelled"
    queue_token_issued = "queue_token.issued"
    escalation_raised = "escalation.raised"
    # software-startup vertical - fired the moment a competitor_mention
    # signal is extracted (services/workers/analyse.py), not batched -
    # research found the first ~83 seconds after a competitor comes up
    # decide the deal, so this is deliberately real-time, not a nightly
    # sweep like every other Insight-kind check in this codebase.
    competitor_mentioned = "competitor.mentioned"


class OutboundWebhook(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "outbound_webhooks"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Generated server-side (secrets.token_urlsafe) when the endpoint is
    # created, never chosen by the caller - the whole point is a business
    # can prove a payload came from Krova.
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Free text: "ok", or an HTTP status/error string - shown to the owner
    # so a dead Zapier hook is visible, not silently going dark.
    last_delivery_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # "raw" (default) sends the plain event payload unchanged - today's
    # exact behavior. "slack"/"teams" wrap the same payload into the
    # {"text": "..."} shape those platforms' own incoming webhooks expect
    # - see services/workers/webhook_delivery.py for where this branches.
    format: Mapped[str] = mapped_column(String(10), nullable=False, default="raw")

    __table_args__ = (Index("idx_outbound_webhooks_business", "business_id"),)


class ApiKey(UUIDMixin, TimestampMixin, Base):
    """
    A business's own systems calling Krova directly - see
    services/api/routers/public_api.py - not the embeddable widget
    (WebWidgetConfig.site_key is
    public-by-design and domain-scoped; this is a bearer credential that
    must never appear in client-side code and has no domain to check).

    Only a hash is stored - a raw API key only ever needs comparing on the
    request path, never reading back, so unlike ChannelConnection's OAuth
    tokens (reversible, via shared.auth.encryption, because they get used
    again against Meta/Google), a one-way hash is the simpler, correct
    choice here. The raw key is shown exactly once, at creation - same
    convention already established for OutboundWebhook.secret.
    """

    __tablename__ = "api_keys"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # First few characters of the raw key, stored plaintext - lets the
    # settings UI show "krova_live_a1b2c3.." without ever being able to
    # re-display the real key. Same convention Stripe/GitHub use.
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Same fixed-window shape already proven on WebWidgetConfig.
    rate_limit_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rate_limit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("idx_api_keys_business", "business_id"),)

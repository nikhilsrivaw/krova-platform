"""
The embeddable website chat widget - a business's own config, and each
anonymous visitor's live conversation state.

Two tables, deliberately not one, mirroring the same split
Business.kiosk_token (durable per-tenant config) vs QueueEntry (ephemeral
per-visit state) already uses:

WebWidgetConfig is what a business sets up once - which domain the widget
is allowed to run on, and the opaque key their embed snippet uses to prove
which business it belongs to. A business with two sites gets two rows, not
a list column, so the CORS lookup this exists for stays one indexed
equality check.

WebSession is a single visitor's conversation. It starts anonymous
(customer_id null) - Tier 1 knowledge Q&A needs no identity at all, since
there is no IdentityKind for "anonymous browser session," only real
phone/email/instagram/whatsapp. It is only ever promoted to a real
Customer, via the same shared/identity/resolver.py every other channel
uses, the moment a visitor actually gives contact info for a Tier 2 action
(a booking) - never assumed, never invented.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin


class WebWidgetConfig(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "web_widget_configs"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # Opaque, public, safe to ship in a client-side <script> snippet - same
    # convention as Business.kiosk_token. Not a secret; the allowed_domain
    # check below is the actual safeguard, matching how every real widget
    # product in this space (Inkeep, Chatbase, Retell) treats their own
    # public embed keys.
    site_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # The one domain this key may be embedded on - a business with two
    # sites gets two rows rather than a list column, so the CORS lookup
    # this exists for is a single indexed equality check, not a scan.
    allowed_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # A crude but real sliding-window rate limit, business-wide (across
    # every visitor session): a scripted attacker hitting this endpoint
    # runs up this business's own Claude API bill, and research found the
    # widget had zero protection against that. Approximate by design - a
    # rate limiter does not need SAVEPOINT-grade correctness the way a
    # booking does, and this stays a single-row read/write, no new table.
    rate_limit_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rate_limit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_web_widget_configs_business", "business_id"),
        UniqueConstraint("business_id", "allowed_domain", name="uq_web_widget_domain_per_business"),
    )


class WebSession(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "web_sessions"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # Opaque, held by the widget's own JS across messages in one visit -
    # never a cookie (this endpoint is deliberately credential-less, see
    # the widget CORS dependency's own docstring for why).
    session_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Null until a visitor gives real contact info for a Tier 2 action -
    # see the module docstring. Once set, this is a real Customer like any
    # other channel's, with the exact same identity-resolution guarantees.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    # Only ever used while customer_id is still null - once a real Customer
    # exists, shared/ai/context.py's own build() reads history from the
    # Message table like every other channel, and this stops growing.
    # Kapa.ai's "Thread" model is the direct precedent (a conversation the
    # server holds, referenced by session_token across turns) for exactly
    # this pre-identity window.
    transcript: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # A hard cap on how long one conversation can run - real industry
    # practice found is to bound conversation depth, not just request
    # rate, since token cost scales with turns, not messages. Past the
    # cap, the router forces an honest "someone will follow up" reply
    # without even calling the model - a real cost-control measure, not
    # just UX.
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Null until the visitor has seen and accepted the widget's consent
    # notice - DPDP Act research found this needs to happen *before* the
    # conversation starts, not just before contact info is collected -
    # unlike WhatsApp, where a customer messaging first is itself a
    # consent-bearing act inside an app they already agreed to terms on.
    consent_given_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_web_sessions_business", "business_id"),
    )

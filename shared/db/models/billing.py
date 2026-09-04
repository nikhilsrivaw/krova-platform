"""
Metering: every unit of Krova's own cost, tagged to the business it belongs to.

Not Meta's or Plivo's billing - both bill the business (WhatsApp) or Krova's
own parent account (Plivo) directly, and neither lets a reseller set custom
rates or invoice their end-customers on their behalf, confirmed directly with
both. Whatever Krova charges a business for its own processing - AI replies,
message volume, voice minutes - has to be metered and priced entirely here.

One append-only row per billable unit, written alongside (not instead of) the
per-feature cost_paise columns that already exist on Call, MessageDraft, and
similar rows - those stay as fast, denormalized reads for their own screens;
this table is what a monthly rollup or an invoice actually sums.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, UUIDMixin
from shared.db.types import EnumType


class UsageEventType(str, enum.Enum):
    # One inbound message ingested, any channel - the "webhook volume" a
    # business is billed on, mirroring how AiSensy/Interakt/Gupshup meter
    # conversations rather than leaving message volume uncapped.
    message_processed = "message_processed"

    # One Claude call that decided how to answer a message - text-channel
    # (queued-for-approval) and voice (spoken live) both count here; which
    # channel it was is on the row already, so a rollup can split them out
    # without needing a second event type.
    ai_reply_generated = "ai_reply_generated"
    ai_commitment_extraction = "ai_commitment_extraction"
    ai_signal_extraction = "ai_signal_extraction"
    ai_profile_compression = "ai_profile_compression"
    # One structured outcome/sentiment/topic/summary read on a finished
    # call - see shared/ai/call_summary.py.
    ai_call_analysis = "ai_call_analysis"

    # Voice-specific, one row per cost component per call, matching the
    # breakdown already computed in relay.py rather than collapsing them
    # into one number - a business asking "why did this call cost what it
    # did" needs the STT/TTS/telephony split, not just a total.
    voice_call_minutes = "voice_call_minutes"
    voice_stt_seconds = "voice_stt_seconds"
    voice_tts_characters = "voice_tts_characters"


class UsageEvent(UUIDMixin, Base):
    """
    One billable unit. Append-only - nothing here is ever updated, only
    inserted, so a monthly rollup run twice against the same window always
    reproduces the same total.
    """

    __tablename__ = "usage_events"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )

    event_type: Mapped[UsageEventType] = mapped_column(
        EnumType(UsageEventType, 40), nullable=False
    )
    # whatsapp | instagram | email | voice - which channel this unit of
    # usage happened on, independent of event_type (an ai_reply_generated
    # row can be voice or whatsapp; this is what lets a rollup answer
    # "how much did voice cost this business" without joining anything else).
    channel: Mapped[str] = mapped_column(String(20), nullable=False)

    # message | second | character | minute - what quantity counts, so a
    # rollup can apply the right per-unit rate without guessing from
    # event_type alone.
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)

    # What Krova itself paid upstream (Anthropic/Sarvam/Plivo) for this unit -
    # the floor any price Krova charges the business must clear to have a
    # positive margin. Never what the business is charged; that is computed
    # separately by whatever pricing plan is applied to a rollup of these rows.
    krova_cost_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Loose reference to the row this event is about (a Message, Call,
    # Commitment id) - not a real foreign key, since the referenced table
    # varies by event_type and this is an audit trail, not a join target.
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The rollup query's own access pattern: sum this business's events
        # within a billing window.
        Index("idx_usage_events_business_occurred", "business_id", "occurred_at"),
        Index("idx_usage_events_business_type", "business_id", "event_type", "occurred_at"),
    )

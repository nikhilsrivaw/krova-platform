"""
Outbound call campaigns.

The voice-channel counterpart to campaigns.py's WhatsApp broadcasts - same
"the audience is a question about the ledger" idea (reuses Audience from
campaign.py unchanged, since it was already channel-agnostic: it resolves
customers plus their own commitment figures, with no WhatsApp assumption
baked in), but nothing else about a WhatsApp Campaign applies to a phone
call. No template, no variable_mapping, no carousel - a call has an
`objective` instead, a brief the AI drafts an opening line from, grounded
in the same real business/customer data every other prompt in this
codebase already reads, never a fixed script.

Kept as its own tables rather than added onto Campaign: Campaign's own
fields (template_name, variable_mapping, carousel_cards) and its router
are WhatsApp-specific throughout, and bending them around a second,
unrelated domain risks the tested WhatsApp path more than a new table
does.

Real-world note, not enforced here: automated outbound calling in India
needs a business phone number in the right TRAI-registered series (140
for promotional, 160 for BFSI-only transactional/service calls) and DLT
registration - neither is a searchable/instant-buy Plivo product the way
a regular local number is (confirmed directly: nothing listed under 160
in Plivo's own number search), and 160-series doesn't apply to most of
KROVA's actual customer base (not BFSI). This module places calls on
whatever voice number IS connected; getting a compliant number connected
is a real-world provisioning step outside this module's job - see the
project memory note on Plivo's reseller number-provisioning model for the
planned "request required" flow that will eventually cover this.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.models.campaign import Audience
from shared.db.types import EnumType


class CallCampaignStatus(str, enum.Enum):
    draft = "draft"
    sending = "sending"      # jobs enqueued, worker actively dialing
    sent = "sent"             # every recipient attempted
    paused = "paused"
    cancelled = "cancelled"
    failed = "failed"


class CallCampaignRecipientStatus(str, enum.Enum):
    pending = "pending"
    calling = "calling"       # a job has claimed this recipient and is dialing
    completed = "completed"   # a human answered - the conversation itself lives in Call/Message
    voicemail = "voicemail"   # machine_detection=hangup caught an answering machine
    failed = "failed"
    skipped = "skipped"       # no phone number on file, customer marked private, etc.


class CallCampaign(UUIDMixin, TimestampMixin, Base):
    """One outbound calling run against a segment."""

    __tablename__ = "call_campaigns"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    audience: Mapped[Audience] = mapped_column(EnumType(Audience, 24), nullable=False)
    audience_params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # What the call should accomplish, in plain language - "Remind them
    # about the outstanding balance and offer to help them pay" - not a
    # script. shared/ai/outbound_opener.py drafts the actual opening line
    # from this plus the same real business/customer context every other
    # prompt here reads, then the call proceeds through the same agent
    # loop as any inbound conversation.
    objective: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[CallCampaignStatus] = mapped_column(
        EnumType(CallCampaignStatus, 20), nullable=False, default=CallCampaignStatus.draft
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Calls actually placed, in the same sense campaigns.py's sent_count
    # means "sent" - not calls that were answered (see
    # CallCampaignRecipient.status for that finer-grained outcome).
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Null (the default) preserves this class's exact original behaviour -
    # a free-objective call. Set means the call is structured instead: the
    # agent works through CallScript.questions in order rather than
    # improvising from `objective`, and a CallScriptResponse is written
    # once the call ends. See CallScript's own docstring.
    call_script_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("call_scripts.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("idx_call_campaigns_business", "business_id", "status", "created_at"),
    )


class CallCampaignRecipient(UUIDMixin, Base):
    """One customer in one outbound call campaign."""

    __tablename__ = "call_campaign_recipients"

    call_campaign_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("call_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )

    status: Mapped[CallCampaignRecipientStatus] = mapped_column(
        EnumType(CallCampaignRecipientStatus, 20),
        nullable=False,
        default=CallCampaignRecipientStatus.pending,
    )
    # Why it didn't complete - "no phone number on file", a PlivoError
    # message, "daily limit reached" - same discipline as
    # CampaignRecipient.reason.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    call_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_call_campaign_recipients", "call_campaign_id", "status"),
    )


class CallScript(UUIDMixin, TimestampMixin, Base):
    """
    An ordered list of questions an outbound call works through - the
    "lead qualification" / "feedback survey" shape neither a free-
    objective CallCampaign (improvised, one AI-drafted opener, then a
    normal open-ended conversation) nor any other flow in this codebase
    covers today.

    A call using one still goes through the exact same voice pipeline
    (barge-in, Sarvam STT/TTS, Call row) - only the system prompt and the
    post-call analysis differ (see shared/ai/agent.py's
    SCRIPTED_SYSTEM_STREAM and shared/ai/call_script_extract.py). The
    live agent works through `questions` conversationally, one at a time,
    reasoning over the call's own history to know what has and hasn't
    been covered yet - not a rigid state machine, the same "let the model
    reason over what it can see" shape context.py's OwnerContext already
    uses successfully.
    """

    __tablename__ = "call_scripts"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # "lead_qualification" scores the call at the end (fit for what this
    # business sells); "survey" only captures answers, no score - see
    # call_script_extract.py's own purpose-branch.
    purpose: Mapped[str] = mapped_column(String(30), nullable=False, default="survey")
    # Ordered list of plain-language questions, e.g.
    # ["What's your budget for this?", "When are you looking to start?"].
    questions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        Index("idx_call_scripts_business", "business_id"),
    )


class CallScriptResponse(UUIDMixin, Base):
    """
    One completed CallScript run - the structured result of a single
    call, extracted from its transcript once it ends (never invented;
    see call_script_extract.py's own citation discipline). A question
    the transcript never actually covers is missing from `answers`
    entirely, not filled in with a guess.
    """

    __tablename__ = "call_script_responses"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    call_script_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("call_scripts.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL"), nullable=True
    )
    # {question: answer} - only questions the transcript actually
    # answered, in the script's own wording as the key so a question
    # edited later doesn't silently orphan old answers under a stale id.
    answers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Only meaningful for purpose="lead_qualification" - null for a
    # "survey" script, never a fabricated number for one that has no
    # scoring rubric at all.
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_call_script_responses_script", "call_script_id", "created_at"),
    )

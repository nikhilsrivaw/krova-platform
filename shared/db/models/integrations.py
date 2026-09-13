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
from sqlalchemy.dialects.postgresql import JSONB
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
    # Voice roadmap round 5 (shared/care/post_call_actions.py) - the two
    # places a call's outcome actually exists in this codebase (confirmed,
    # not unified): call_completed fires from relay.py's _analyze_call
    # once Call.outcome is set (an answered call); call_voicemail and
    # call_no_answer fire from outbound.py's outbound_hangup, the only
    # place either outcome is ever recorded (no Call row exists for an
    # unanswered/voicemail outbound attempt at all - see
    # CallCampaignRecipientStatus instead).
    call_completed = "call.completed"
    call_voicemail = "call.voicemail"
    call_no_answer = "call.no_answer"
    # Fired from shared/channels/ingest.py::ingest() - the one function
    # every channel (WhatsApp, Instagram, email) already funnels inbound
    # messages through, so this one event type covers all of them, not
    # just WhatsApp. Outbound sends don't fire this - a business's own
    # system already knows what it sent.
    message_received = "message.received"
    # Fired from services/api/routers/webhooks.py's inbound message loop
    # when a WhatsApp Flow submission arrives (media kind "flow_reply",
    # see shared/channels/whatsapp/webhook.py) - the moment a customer
    # finishes a structured form (booking, order tracking, ...), not when
    # it was merely opened.
    flow_completed = "flow.completed"
    # Same real-time reasoning as competitor_mentioned above, and the same
    # dispatch site (services/workers/analyse.py, product_feedback-gated -
    # today only the "startup" vertical declares that capability, same
    # gate competitor_mentioned already runs under). All three are real,
    # already-extracted Insight kinds (shared/ai/signals.py) that simply
    # had no automation trigger wired to them before this.
    churn_risk_detected = "churn_risk.detected"
    demo_requested = "demo.requested"
    pricing_question_asked = "pricing_question.asked"


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


class PostCallActionRule(UUIDMixin, TimestampMixin, Base):
    """
    The voice-to-action bridge - "when a call ends this way, do that" -
    business-configurable, unlike everything else conditional in this
    codebase (recall.py, escalation_failsafe.py, ...), which is a fixed
    Python function per condition. See shared/care/post_call_actions.py,
    which interprets these rows.

    trigger_type is a WebhookEventType value (call.completed/
    call.voicemail/call.no_answer) - not FK-constrained to that enum
    (nothing in this schema is, per OutboundWebhook.event_types' own
    precedent), just validated at the API layer.

    action_type is one of a fixed, generic action menu (see
    services/api/routers/post_call_rules.py::_VALID_ACTIONS) - deliberately
    not an open-ended action language, and deliberately never a
    per-vertical action (no "create_case" just for law firms, etc.). A
    business gets the same action vocabulary regardless of what it sells;
    what differs per business is which of these it wires up, and to what -
    see send_flow, whose actual form content is a WhatsAppFlow the
    business authors itself, not something Krova hardcodes per vertical.
    """

    __tablename__ = "post_call_action_rules"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    # The business's own label for this rule ("New patient no-show
    # recovery") - optional, purely descriptive, never interpreted. Without
    # it a rule only ever exists as "trigger X -> action Y", which is
    # Krova's language, not the business's own.
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # None (the default) means "any channel" - the rule fires regardless of
    # where the trigger came from, today's original behaviour, unchanged.
    # A real gap otherwise: message.received fires identically for a
    # WhatsApp message, an Instagram DM, and every single utterance on a
    # live voice call (all three funnel through shared/channels/ingest.py),
    # so a rule authored with WhatsApp in mind would also fire mid-call on
    # every "hello" the caller says, sending a WhatsApp Flow to someone
    # who's on the phone right now. One of Channel's own values
    # (whatsapp/instagram/email/voice/web) - not FK-constrained, same
    # reasoning as trigger_type/action_type below.
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # whatsapp_followup: {"message": "<text the approved template speaks>"}.
    # create_escalation_task: {"reason": "<text shown on the Escalation row>"}.
    action_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("idx_post_call_rules_lookup", "business_id", "trigger_type", "is_active"),
    )


class AutomationStep(UUIDMixin, TimestampMixin, Base):
    """
    One step of a PostCallActionRule's action sequence.

    Phase 1 of the engine described in this session's own research pass:
    every rule today still holds exactly one step (backfilled 1:1 from the
    rule's own action_type/action_config below by this table's own
    migration - see its docstring), so nothing about how a rule actually
    runs changes yet. What this shape buys, for later phases: an optional
    delay before the step fires, an optional condition gating whether it
    fires at all, and more than one step per rule, in order - none of
    which shared/care/post_call_actions.py reads yet.

    A child table rather than fields added to PostCallActionRule itself,
    on purpose: a rule stays "when this trigger fires, on this channel"
    (unchanged, still the thing business_id/trigger_type/channel describe);
    a step is "do this, maybe after a wait, maybe only if a condition
    holds" - one rule can eventually hold several, ordered by `position`.
    """

    __tablename__ = "automation_steps"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("post_call_action_rules.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Seconds to wait after the trigger (or, for step 2+, after the
    # previous step) before this step runs. None (the default, and every
    # step's value today) means "run immediately" - the only real
    # behaviour that exists until a later phase actually schedules a
    # delayed step.
    delay_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # {"field": "commitment.amount_paise", "operator": "greater_than", "value": 50000}
    # None (the default, and every step's value today) means "always run" -
    # no gate. `field` is meant to come from a small, fixed allowlist per
    # trigger_type (validated at the API layer, same as trigger_type/
    # action_type themselves) once a later phase actually evaluates this -
    # never an open query language over arbitrary columns.
    condition: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_automation_steps_rule", "rule_id", "position"),
    )


class AutomationStepRun(UUIDMixin, TimestampMixin, Base):
    """
    A rule's step chain, paused at a delayed step, queued to resume later.

    Runs as a periodic sweep (services/api/scheduler.py), the same "scan
    due work, stamp a one-shot marker, act once" shape as every other
    delayed action in this codebase (Escalation.escalated_further_at,
    Order.cod_call_placed_at - see shared/care/escalation_failsafe.py and
    cod_call_failsafe.py) rather than a new job-queue concept - `due_at`/
    `executed_at` play exactly that role here.

    Phase 4 (true multi-step chains) generalized this from "one delayed
    step" to "the rest of the chain from here": `remaining_steps` snapshots
    every step from the delayed one onward (each as its own action_type/
    action_config/condition/delay_seconds dict) at the moment the trigger
    fired - not the live AutomationStep rows - so editing a rule after this
    row exists can't silently change what an already-queued chain does.
    `context` is the trigger's own original data, snapshotted the same way,
    so a later step's condition can still be evaluated correctly once the
    chain resumes. remaining_steps[0] is always the step whose delay is
    what made this row due; shared/care/post_call_actions.py::run_due_steps
    runs it unconditionally (its condition already passed once, before it
    was queued) and then continues evaluating remaining_steps[1:] in order,
    queuing a fresh row (rule_id unchanged, a shorter remaining_steps) the
    next time it hits a step with its own delay.

    Deleting the rule (ondelete=CASCADE) cancels a still-pending run - the
    one case that should change what was queued: nothing a deleted
    automation queued should keep firing after it's gone.
    """

    __tablename__ = "automation_step_runs"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("post_call_action_rules.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    call_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # The trigger's own real data (shared/care/post_call_actions.py::
    # CONDITION_FIELDS) - needed so a step further down the chain, resumed
    # after this row fires, can still have its own condition evaluated
    # against the original trigger, not just whatever's true when the
    # sweep happens to run.
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # [{"action_type": ..., "action_config": ..., "condition": ..., "delay_seconds": ...}, ...]
    # in chain order, starting with the step this row is waiting on.
    remaining_steps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # None = still pending. Stamped the moment the sweep picks this row up,
    # before the action itself runs - same "mark it done first, so a crash
    # mid-action never causes a retry-forever loop" discipline as every
    # other one-shot dedupe column in this codebase.
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_automation_step_runs_due", "due_at", "executed_at"),
    )

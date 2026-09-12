"""
The trigger-to-action bridge - interprets PostCallActionRule rows.

No rules engine, or anything like it, exists anywhere else in this
codebase - every other conditional action here is a hardcoded Python
function wired as one fixed scheduler job (cod_call_failsafe.py's
WhatsApp-unanswered -> call -> escalate chain is the closest analog).
This is genuinely new infrastructure: a business picks a trigger and an
action themselves, rather than a developer hardcoding the pairing.

Named and tabled for its original scope (started as the voice-to-action
bridge, "when a call ends this way, do that") but the mechanism was
already generic - trigger_type is an arbitrary string, never FK-
constrained to call.* specifically. Now called from every place a
WebhookEventType already fires with a resolvable customer (ingest.py's
message.received, booking.py's appointment events, the flow-completion
path in webhooks.py, ...), not only the two call-outcome hook points.
The table keeps its original name (shared/db/models/integrations.py's
PostCallActionRule) rather than a rename migration for a naming-only
gain - see services/api/routers/post_call_rules.py's own _VALID_TRIGGERS
for the current, cross-channel list.

v1 shipped two action types; a business-configurable graph/canvas UI is
explicitly a later round (Nikhil's own direction) - this stays a flat
trigger -> action list, deliberately not an open-ended action language
(arbitrary webhooks, arbitrary email templates). Adding a new action type
is a new `elif`, not a schema change - action_config is already free-form
JSON per rule.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Customer, PostCallActionRule
from shared.utils.logging import get_logger

logger = get_logger(__name__)


async def apply_rules(
    db: AsyncSession, *, business_id: uuid.UUID, trigger_type: str, customer_id: uuid.UUID | None,
    call_id: uuid.UUID | None = None, channel: str | None = None,
) -> int:
    """
    Run every active rule matching this trigger for this business. Returns
    how many actions ran.

    `channel` is which real channel this trigger fired from (voice,
    whatsapp, instagram, email, web) - every dispatch site now has a real
    value to pass, since it always knows. A rule with no `channel` set
    fires regardless (today's original, unfiltered behaviour); a rule that
    picked a specific channel is skipped when this trigger came from
    somewhere else. Without this, message.received - which fires
    identically for a WhatsApp message, an Instagram DM, and every single
    utterance on a live voice call - could fire a WhatsApp-authored rule
    mid-phone-call.
    """
    if customer_id is None:
        # Every action type today needs a customer (a WhatsApp send, an
        # Escalation row tied to who the call was about) - nothing to do
        # for a call with no resolved customer at all.
        return 0

    result = await db.execute(
        select(PostCallActionRule).where(
            PostCallActionRule.business_id == business_id,
            PostCallActionRule.trigger_type == trigger_type,
            PostCallActionRule.is_active.is_(True),
        )
    )
    rules = [
        r for r in result.scalars().all()
        if not r.channel or r.channel == channel
    ]
    if not rules:
        return 0

    customer = await db.get(Customer, customer_id)
    if customer is None:
        return 0

    from shared.db.models import Business

    business = await db.get(Business, business_id)
    if business is None:
        return 0

    ran = 0
    for rule in rules:
        try:
            if rule.action_type == "whatsapp_followup":
                from shared.scheduling import notify

                message = (rule.action_config or {}).get("message") or ""
                if not message:
                    logger.warning("post_call_action_rule=%s has no message configured, skipping", rule.id)
                    continue
                if "{{summary}}" in message:
                    message = (await _resolve_summary_token(message, call_id, db)).strip()
                    if not message:
                        logger.warning(
                            "post_call_action_rule=%s left empty after resolving {{summary}}, skipping", rule.id,
                        )
                        continue
                if await notify.send_post_call_followup(db, business=business, customer=customer, message=message):
                    ran += 1

            elif rule.action_type == "create_escalation_task":
                from shared.ai import agent as agent_module

                reason = (rule.action_config or {}).get("reason") or f"Post-call follow-up needed ({trigger_type})"
                # via_automation=True: this escalation was itself raised by
                # a rule, so it must not re-enter apply_rules for
                # escalation.raised - a rule mapping that trigger back to
                # create_escalation_task would otherwise cascade forever.
                #
                # channel or "voice": real value when apply_rules' own
                # caller knows it (every dispatch site does, as of this
                # session's channel-filter work) - "voice" only as a last
                # resort for a call site that predates that and still
                # passes none, not a claim this always came from voice.
                await agent_module.notify_escalation(
                    business_id, reason=reason, customer_id=customer_id, channel=channel or "voice", db=db,
                    via_automation=True,
                )
                ran += 1

            elif rule.action_type == "add_tag":
                if await _add_tag(business_id, customer_id, rule.action_config or {}, db):
                    ran += 1

            elif rule.action_type == "send_flow":
                if await _send_flow(business_id, customer_id, rule.action_config or {}, db):
                    ran += 1

            elif rule.action_type == "place_call":
                if await _place_call(business_id, customer_id, rule.action_config or {}, db):
                    ran += 1

            elif rule.action_type == "send_sms":
                if await _send_sms(business_id, customer_id, rule.action_config or {}, db):
                    ran += 1

            elif rule.action_type == "send_email":
                if await _send_email(business_id, customer_id, rule.action_config or {}, db):
                    ran += 1

            else:
                logger.warning("post_call_action_rule=%s has unrecognised action_type=%s", rule.id, rule.action_type)
        except Exception:
            logger.exception("post_call_action_rule=%s failed to apply", rule.id)

    return ran


async def _resolve_summary_token(message: str, call_id: uuid.UUID | None, db: AsyncSession) -> str:
    """
    Substitutes the AI-generated call summary (Call.summary, written by
    shared/ai/call_summary.py::summarize() before this trigger ever fires -
    see _analyze_call in relay.py) into a {{summary}} token. No call_id
    (the voicemail/no_answer dispatch site in outbound.py never has a Call
    row - see its own comment) or no summary yet resolves to blank rather
    than sending the literal token to a customer.
    """
    summary = None
    if call_id is not None:
        from shared.db.models import Call

        call_row = await db.get(Call, call_id)
        summary = call_row.summary if call_row is not None else None

    return message.replace("{{summary}}", summary or "")


async def _place_call(business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession) -> bool:
    """
    Ring the customer - reuses shared/channels/voice/outbound.py's own
    place_adhoc_call verbatim (the same function commitment_deadline_calls.py
    already uses for proactive reminders), rather than a second connection/
    subaccount lookup for the same call. That function already logs its
    own skip reasons (no phone on file, no voice number connected) and
    returns False rather than raising for any of them.
    """
    reason = str(config.get("reason") or "").strip()
    if not reason:
        logger.warning("place_call rule for business=%s has no reason configured, skipping", business_id)
        return False

    from shared.channels.voice import outbound

    return await outbound.place_adhoc_call(business_id, customer_id, reason, db)


async def _send_sms(business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession) -> bool:
    """
    Same connection/credential resolution as shared/care/
    escalation_failsafe.py's own SMS send, verbatim - ChannelConnection.
    access_token decrypted as the subaccount auth_token, connection.extra
    ["subaccount_auth_id"] as the auth_id, connection.external_account_id
    as the from_number - not a second lookup path for the same three
    fields. Sends to the customer's own phone identity, unlike the
    failsafe (which sends to the business's staff_phone_number instead).
    """
    message = str(config.get("message") or "").strip()
    if not message:
        logger.warning("send_sms rule for business=%s has no message configured, skipping", business_id)
        return False

    from shared.auth.encryption import decrypt
    from shared.channels.voice import plivo_client
    from shared.db.models import Channel, ChannelConnection, ConnectionStatus, CustomerIdentity, IdentityKind

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.voice,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        logger.info("send_sms rule skipped business=%s: no voice number connected", business_id)
        return False

    auth_id = (connection.extra or {}).get("subaccount_auth_id")
    if not auth_id:
        logger.warning("send_sms rule skipped business=%s: voice connection missing subaccount id", business_id)
        return False

    to_number = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.phone,
            )
        )
    ).scalars().first()
    if to_number is None:
        logger.info("send_sms rule skipped business=%s customer=%s: no phone on file", business_id, customer_id)
        return False

    try:
        await plivo_client.send_sms(
            auth_id=auth_id, auth_token=decrypt(connection.access_token),
            from_number=connection.external_account_id, to_number=to_number, text=message,
        )
    except plivo_client.PlivoError:
        logger.exception("send_sms rule failed business=%s customer=%s", business_id, customer_id)
        return False
    return True


async def _send_email(business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession) -> bool:
    """
    Same "verified EmailSendConnection + customer's own email identity"
    shape shared/scheduling/notify.py's own onboarding/expansion nudges
    already use for their email fallback, not a second lookup path for
    the same two things.
    """
    subject = str(config.get("subject") or "").strip()
    body = str(config.get("body") or "").strip()
    if not subject or not body:
        logger.warning("send_email rule for business=%s is missing subject/body, skipping", business_id)
        return False

    from shared.db.models import CustomerIdentity, EmailSendConnection, IdentityKind
    from shared.integrations import postmark

    connection = (
        await db.execute(
            select(EmailSendConnection).where(
                EmailSendConnection.business_id == business_id,
                EmailSendConnection.verified.is_(True),
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        logger.info("send_email rule skipped business=%s: no verified email connection", business_id)
        return False

    to_email = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.email,
            )
        )
    ).scalars().first()
    if to_email is None:
        logger.info("send_email rule skipped business=%s customer=%s: no email on file", business_id, customer_id)
        return False

    try:
        await postmark.send_email(from_email=connection.from_email, to=to_email, subject=subject, text_body=body)
    except postmark.PostmarkError:
        logger.exception("send_email rule failed business=%s customer=%s", business_id, customer_id)
        return False
    return True


async def _add_tag(business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession) -> bool:
    """Same shape as crm.py's own add_tag endpoint, minus a human user - a
    rule's own reasoning fills where that endpoint records who typed it."""
    from shared.db.models import CustomerTag, TagStatus

    label = str(config.get("tag") or "").strip().lower()
    if not label:
        logger.warning("add_tag rule for business=%s has no tag configured, skipping", business_id)
        return False

    existing = (
        await db.execute(
            select(CustomerTag).where(CustomerTag.customer_id == customer_id, CustomerTag.label == label)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.status = TagStatus.confirmed
        return True

    db.add(
        CustomerTag(
            business_id=business_id,
            customer_id=customer_id,
            label=label,
            status=TagStatus.confirmed,
            reasoning="Added automatically by an automation rule",
        )
    )
    return True


async def _send_flow(business_id: uuid.UUID, customer_id: uuid.UUID, config: dict, db: AsyncSession) -> bool:
    """
    Same mechanics as services/api/routers/flows.py's own send_flow
    endpoint - resolve the flow, the customer's phone, the service window,
    the WhatsApp connection, send, log. Duplicated rather than imported
    because that endpoint is current_user-scoped (a human clicking Send);
    this path has no user, only a business_id and a rule that fired.

    Only ever sends a *published* flow - never draft/test mode, since
    nobody is standing by as an app tester for a rule that fires
    unattended.
    """
    import datetime as _dt

    from shared.auth.encryption import decrypt
    from shared.channels import ingest
    from shared.channels.whatsapp.client import WhatsAppClient, WhatsAppError, within_service_window
    from shared.db.models import (
        Channel,
        ChannelConnection,
        ConnectionStatus,
        CustomerIdentity,
        Direction,
        FlowSendLog,
        FlowStatus,
        IdentityKind,
        Message,
        WhatsAppFlow,
    )

    flow_id_raw = config.get("flow_id")
    body = str(config.get("body") or "").strip()
    screen = str(config.get("screen") or "").strip()
    cta = str(config.get("cta") or "Open").strip()
    if not (flow_id_raw and body and screen):
        logger.warning("send_flow rule for business=%s is missing flow_id/body/screen, skipping", business_id)
        return False
    try:
        flow_id = uuid.UUID(str(flow_id_raw))
    except ValueError:
        logger.warning("send_flow rule for business=%s has an invalid flow_id, skipping", business_id)
        return False

    flow = await db.get(WhatsAppFlow, flow_id)
    if flow is None or flow.business_id != business_id or flow.status != FlowStatus.published:
        logger.warning("send_flow rule for business=%s points at a missing/unpublished flow, skipping", business_id)
        return False

    identity = (
        await db.execute(
            select(CustomerIdentity.value).where(
                CustomerIdentity.customer_id == customer_id, CustomerIdentity.kind == IdentityKind.phone,
            )
        )
    ).scalars().first()
    if identity is None:
        return False

    last_inbound = (
        await db.execute(
            select(Message.occurred_at)
            .where(Message.customer_id == customer_id, Message.direction == Direction.inbound)
            .order_by(Message.occurred_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if not within_service_window(last_inbound):
        return False

    connection = (
        await db.execute(
            select(ChannelConnection).where(
                ChannelConnection.business_id == business_id,
                ChannelConnection.channel == Channel.whatsapp,
                ChannelConnection.status == ConnectionStatus.active,
            )
        )
    ).scalars().first()
    if connection is None or not connection.access_token:
        return False

    client = WhatsAppClient(decrypt(connection.access_token), connection.external_account_id)
    flow_token = str(uuid.uuid4())
    try:
        result = await client.send_flow_message(
            identity, body, flow_id=flow.meta_flow_id, flow_token=flow_token,
            flow_cta=cta, screen=screen, data=config.get("data") or {}, draft=False,
        )
    except WhatsAppError:
        logger.exception("send_flow rule for business=%s failed to send", business_id)
        return False

    db.add(FlowSendLog(business_id=business_id, flow_id=flow.id, customer_id=customer_id, flow_token=flow_token))
    await ingest.ingest(
        business_id=business_id,
        channel=Channel.whatsapp,
        direction=Direction.outbound,
        identity_kind=IdentityKind.phone,
        identity_value=identity,
        external_id=result.external_id,
        text=body,
        occurred_at=_dt.datetime.now(_dt.timezone.utc),
        connection_id=connection.id,
        raw={"flow_id": flow.meta_flow_id, "screen": screen, "flow_token": flow_token, "automation": True},
        media={"kind": "flow_open", "flow_name": flow.name},
        enqueue_analysis=False,
        db=db,
    )
    return True

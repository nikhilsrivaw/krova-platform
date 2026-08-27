"""
Channel webhooks.

The most timing-sensitive code in the platform. Meta expects a 200 quickly;
a webhook that is slow or errors gets retried, and one that keeps failing gets
disabled - at which point every business on the platform silently stops
receiving messages.

So the shape is fixed: verify the signature, hand the body to a background
task, return 200. No database work, no AI, nothing that can block, before the
response goes out.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from shared.channels import ingest
from shared.auth.encryption import decrypt
from shared.channels.shopify import signature as shopify_signature
from shared.channels.shopify import webhook as shopify_webhook
from shared.channels.whatsapp import conversions, media, signature, webhook
from shared.db.models import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Customer,
    Direction,
    IdentityKind,
    Order,
    OrderStatus,
    StoreConnection,
)
from shared.db.session import AsyncSessionLocal
from shared.identity import resolver as identity_resolver
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/whatsapp", response_class=PlainTextResponse)
async def verify_whatsapp_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
) -> Response:
    """
    Meta's subscription handshake.

    Sent once when the callback URL is saved. Meta expects hub.challenge
    echoed back verbatim, but only after hub.verify_token matches the token
    configured in the dashboard - that is what proves we own this endpoint.
    """
    if not signature.verify_subscription(hub_mode, hub_verify_token):
        logger.warning("webhook verification rejected (mode=%r)", hub_mode)
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    logger.info("webhook subscription verified")
    return PlainTextResponse(content=hub_challenge or "")


@router.post("/whatsapp")
async def receive_whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
) -> Response:
    """
    Inbound messages and delivery statuses.

    Signature is checked against the raw bytes, before any parsing - parse and
    re-serialise and the MAC no longer matches what was signed.
    """
    raw_body = await request.body()

    try:
        signature.verify(raw_body, x_hub_signature_256)
    except signature.InvalidSignature:
        # 403 with no body. An unsigned or wrongly signed request is either a
        # misconfiguration or someone probing; neither deserves detail.
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    background_tasks.add_task(_process_whatsapp, raw_body)
    return Response(status_code=status.HTTP_200_OK)


async def _process_whatsapp(raw_body: bytes) -> None:
    """
    Store what arrived. Runs after the response has already been sent.

    Never raises: this runs detached, so an exception here would vanish into
    the task runner. Everything is logged instead.
    """
    import json

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.error("webhook body was not JSON (%d bytes)", len(raw_body))
        return

    parsed = webhook.parse(payload)
    if not parsed:
        return

    async with AsyncSessionLocal() as db:
        try:
            for message in parsed.messages:
                connection = await ingest.find_connection(
                    Channel.whatsapp, message.phone_number_id, db
                )
                if connection is None:
                    # A number we do not have connected. Happens if a business
                    # disconnects while Meta still delivers, or if someone
                    # points another app's webhook at us.
                    logger.warning(
                        "message for unknown number %s (waba=%s) - dropped",
                        message.phone_number_id,
                        message.waba_id,
                    )
                    continue

                text = message.text
                media_info = dict(message.media or {})

                # A photographed invoice is the most information-dense message
                # in a conversation and the one we would otherwise understand
                # least. Read it now: Meta's download URL lasts five minutes
                # and the media id only seven days.
                if media_info.get("id") and connection.access_token:
                    read_text, details = await media.read(
                        media_info["id"],
                        decrypt(connection.access_token),
                        phone_number_id=connection.external_account_id,
                    )
                    media_info.update(details)
                    if read_text:
                        # A caption plus what the image says beats either alone.
                        text = f"{text}\n\n{read_text}" if text else read_text

                result = await ingest.ingest(
                    business_id=connection.business_id,
                    channel=Channel.whatsapp,
                    direction=Direction.inbound,
                    identity_kind=IdentityKind.phone,
                    identity_value=message.from_phone,
                    external_id=message.external_id,
                    text=text,
                    occurred_at=message.occurred_at,
                    display_name=message.profile_name,
                    media=media_info,
                    raw=message.raw,
                    connection_id=connection.id,
                    referral=message.referral,
                    db=db,
                )
                if result.created:
                    logger.info(
                        "inbound whatsapp business=%s customer=%s",
                        connection.business_id,
                        result.customer.id if result.customer else None,
                    )
                    if media_info.get("kind") == "order" and result.customer is not None:
                        await _record_native_order(
                            connection.business_id, result.customer.id,
                            media_info.get("order") or {}, message.external_id, message.occurred_at, db,
                        )

            for update in parsed.statuses:
                await _apply_status(update, db)

            for other in parsed.other:
                field = other.get("field")
                if field == "message_template_status_update":
                    await _apply_template_status(other.get("value") or {}, db)
                else:
                    # account_update, quality changes. Logged rather than
                    # silently discarded, so we notice the first time one
                    # arrives rather than discovering it months later.
                    logger.info(
                        "unhandled webhook field %r for waba=%s",
                        field,
                        other.get("waba_id"),
                    )

            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("failed to process whatsapp webhook")


async def _apply_status(update: webhook.StatusUpdate, db) -> None:
    """
    Record what happened to a message we sent.

    Failures matter more than deliveries: a failed send means a customer never
    got an answer, and nothing else in the system would ever notice.
    """
    if update.status == "failed":
        logger.warning(
            "message %s to %s FAILED: %s",
            update.external_id,
            update.recipient_phone,
            update.errors,
        )


async def _record_native_order(
    business_id: uuid.UUID, customer_id: uuid.UUID, order_data: dict,
    message_id: str, occurred_at, db,
) -> None:
    """
    A customer submitted a cart from the business's own Meta catalog,
    entirely inside WhatsApp - reuse the same Order table Shopify orders
    land in (source_platform="whatsapp" instead of "shopify") rather than a
    second table only for chat-native orders. Everywhere that already reads
    Order (the orders API, shared/ai/context.py's WISMO answers) picks this
    up with zero extra code.

    Status starts at "pending" and stays there until a human moves it -
    unlike Shopify, Meta gives no separate payment/fulfillment webhook for
    a chat-native order, so nothing here can honestly claim it's paid.
    """
    parsed = webhook.parse_native_order(order_data)
    order = Order(
        business_id=business_id,
        customer_id=customer_id,
        source_platform="whatsapp",
        external_order_id=message_id,  # the wamid - unique per business already
        status=OrderStatus.pending,
        items=[
            {"product_retailer_id": i.product_retailer_id, "quantity": i.quantity, "price_paise": i.price_paise}
            for i in parsed.items
        ],
        total_paise=parsed.total_paise,
        placed_at=occurred_at,
        raw_payload=order_data,
    )
    db.add(order)
    await db.flush()
    logger.info(
        "whatsapp native order recorded business=%s customer=%s items=%d",
        business_id, customer_id, len(parsed.items),
    )


# Meta's event values, mapped to what we store. Anything unrecognised leaves
# the template alone rather than guessing - a wrong status here would either
# hide a usable template or offer an unusable one.
_TEMPLATE_EVENTS = {
    "APPROVED": "APPROVED",
    "REJECTED": "REJECTED",
    "PAUSED": "PAUSED",
    "DISABLED": "DISABLED",
    "FLAGGED": "FLAGGED",
    "ARCHIVED": "ARCHIVED",
    "DELETED": "DELETED",
    "PENDING": "PENDING",
    "PENDING_DELETION": "PENDING",
    "REINSTATED": "APPROVED",
    "UNARCHIVED": "APPROVED",
    "IN_APPEAL": "PENDING",
}


async def _apply_template_status(value: dict, db) -> None:
    """
    Record Meta's verdict on a template.

    This is the other half of template creation. Submitting returns PENDING
    and nothing else; the answer arrives here, up to 24 hours later, long
    after the request that created it has finished. Without this a client
    would watch a template sit at "pending review" forever even after Meta
    approved it.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from shared.db.models import MessageTemplate

    event = (value.get("event") or "").upper()
    external_id = str(value.get("message_template_id") or "")
    name = value.get("message_template_name")
    language = value.get("message_template_language")

    mapped = _TEMPLATE_EVENTS.get(event)
    if mapped is None:
        logger.info("unrecognised template event %r for %r", event, name)
        return

    # Prefer Meta's id; fall back to name+language, which is how a template
    # created in WhatsApp Manager reaches us before we have ever seen its id.
    template = None
    if external_id:
        result = await db.execute(
            select(MessageTemplate).where(MessageTemplate.external_id == external_id)
        )
        template = result.scalars().first()
    if template is None and name:
        conditions = [MessageTemplate.name == name]
        if language:
            conditions.append(MessageTemplate.language == language)
        result = await db.execute(select(MessageTemplate).where(*conditions))
        template = result.scalars().first()

    if template is None:
        logger.info("template status for %r (%s) - not one of ours", name, event)
        return

    template.status = mapped
    template.reviewed_at = datetime.now(timezone.utc)
    if external_id and not template.external_id:
        template.external_id = external_id

    if mapped == "REJECTED":
        reason = value.get("reason") or (value.get("rejection_info") or {}).get(
            "rejection_reason"
        )
        template.rejection_reason = str(reason) if reason else None
        logger.warning("template %r rejected: %s", template.name, reason)
    else:
        template.rejection_reason = None
        logger.info("template %r -> %s", template.name, mapped)


# ── Shopify (Order Sync) ─────────────────────────────────────────────────
#
# One endpoint for every connected store, on every business - Shopify's
# X-Shopify-Shop-Domain header is how a single URL serves every tenant, the
# same trick the WhatsApp endpoint plays with phone_number_id. The signature
# check differs from Meta's for a real reason: v1 connects a store the way
# a business connects WhatsApp - the owner pastes in a webhook secret they
# generated in their own Shopify Admin (Settings > Notifications), not an
# OAuth app install. That means the signing key is per-store, so the store
# must be looked up before the signature can even be checked - unlike Meta,
# where one app secret verifies every business's webhook. Everything else
# keeps the same discipline: raw bytes verified before parsing, work handed
# to a background task, 200 returned fast either way (a store that gets a
# non-200 assumes total failure and can disable the webhook after enough
# retries, same as Meta).

@router.post("/shopify/orders")
async def receive_shopify_order_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_shopify_hmac_sha256: str | None = Header(default=None),
    x_shopify_shop_domain: str | None = Header(default=None),
) -> Response:
    if not x_shopify_shop_domain:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    raw_body = await request.body()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(StoreConnection).where(
                StoreConnection.platform == "shopify",
                StoreConnection.store_identifier == x_shopify_shop_domain,
                StoreConnection.active == True,  # noqa: E712
            )
        )
        connection = result.scalars().first()

        if connection is None:
            # Not a misconfiguration worth 500-ing over: a store disconnected
            # while Shopify still has the webhook registered. Ack and drop,
            # same reasoning as an unknown WhatsApp phone_number_id.
            logger.warning("shopify webhook for unconnected store %s", x_shopify_shop_domain)
            return Response(status_code=status.HTTP_200_OK)

        try:
            shopify_signature.verify(
                raw_body, x_shopify_hmac_sha256, decrypt(connection.webhook_secret)
            )
        except shopify_signature.InvalidSignature:
            return Response(status_code=status.HTTP_403_FORBIDDEN)

        business_id = connection.business_id

    background_tasks.add_task(_process_shopify_order, raw_body, business_id)
    return Response(status_code=status.HTTP_200_OK)


async def _process_shopify_order(raw_body: bytes, business_id: uuid.UUID) -> None:
    """
    Upsert one order. Runs after the response has already been sent - never
    raises, everything is logged instead, same contract as _process_whatsapp.
    """
    import json

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.error("shopify webhook body was not JSON (%d bytes)", len(raw_body))
        return

    parsed = shopify_webhook.parse_order(payload)
    if parsed is None:
        return

    async with AsyncSessionLocal() as db:
        try:
            existing = await db.execute(
                select(Order).where(
                    Order.business_id == business_id,
                    Order.source_platform == "shopify",
                    Order.external_order_id == parsed.external_order_id,
                )
            )
            order = existing.scalars().first()
            was_already_paid = order is not None and order.status.value == "paid"

            # Resolve identity only when there is no customer on this order
            # yet - never on every webhook. Shopify's later webhooks
            # (fulfilled, cancelled) do not always repeat every field the
            # first one had; if update webhooks re-resolved identity from
            # whatever subset they happen to carry, a phone-resolved
            # customer on orders/create could get silently replaced by a
            # brand-new, email-resolved customer on orders/cancelled -
            # splitting one person's order history across two Customer rows.
            # Once an order is attached to a customer, that attachment is
            # the order's identity, full stop.
            customer_id = order.customer_id if order else None
            if customer_id is None:
                if parsed.customer_phone:
                    resolution = await identity_resolver.resolve(
                        business_id, IdentityKind.phone, parsed.customer_phone, db,
                    )
                    customer_id = resolution.customer.id
                elif parsed.customer_email:
                    resolution = await identity_resolver.resolve(
                        business_id, IdentityKind.email, parsed.customer_email, db,
                    )
                    customer_id = resolution.customer.id

            if order is None:
                order = Order(
                    business_id=business_id,
                    customer_id=customer_id,
                    source_platform="shopify",
                    external_order_id=parsed.external_order_id,
                    placed_at=parsed.placed_at,
                )
                db.add(order)
            elif order.customer_id is None and customer_id is not None:
                order.customer_id = customer_id

            order.order_number = parsed.order_number
            order.status = parsed.status
            order.items = parsed.items
            order.total_paise = parsed.total_paise
            order.tracking_number = parsed.tracking_number
            order.carrier = parsed.carrier
            order.raw_payload = parsed.raw

            just_became_paid = parsed.status == "paid" and not was_already_paid
            fire_conversion_for = (customer_id, order.total_paise) if just_became_paid else None

            await db.commit()
            logger.info(
                "shopify order %s business=%s status=%s",
                parsed.external_order_id, business_id, parsed.status,
            )
        except Exception:
            await db.rollback()
            logger.exception("failed to process shopify order webhook")
            return

    # Outside the transaction, and only after it actually committed - never
    # count a conversion for a database write that did not happen. A
    # failure here is logged and swallowed: Meta not receiving one ad
    # attribution event must never turn into retrying (and re-billing) an
    # otherwise-successful order webhook.
    if fire_conversion_for is not None:
        await _fire_purchase_conversion(business_id, *fire_conversion_for)


async def _fire_purchase_conversion(
    business_id: uuid.UUID, customer_id: uuid.UUID | None, total_paise: int | None
) -> None:
    if customer_id is None:
        return
    async with AsyncSessionLocal() as db:
        customer = await db.get(Customer, customer_id)
        if customer is None or not customer.ctwa_clid:
            return
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
            return
        dataset_id = (connection.extra or {}).get("dataset_id")
        try:
            result = await conversions.send_conversion_for_customer(
                dataset_id=dataset_id,
                access_token=decrypt(connection.access_token),
                customer_ctwa_clid=customer.ctwa_clid,
                event_name="Purchase",
                value_paise=total_paise,
            )
            if result.sent:
                logger.info("purchase conversion sent business=%s customer=%s", business_id, customer_id)
        except conversions.ConversionEventError:
            logger.warning("purchase conversion failed business=%s customer=%s", business_id, customer_id)

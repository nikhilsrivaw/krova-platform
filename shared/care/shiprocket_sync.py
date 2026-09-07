"""
Filling the one real courier-status gap shared/channels/shopify/
webhook.py's own docstring already names: Shopify's order payload never
carries out_for_delivery/delivered/NDR facts, because Shopify doesn't
have them - Shiprocket does, as the aggregator small/mid Indian D2C
sellers actually use.

Poll-based, not webhook-based - confirmed against Shiprocket's own docs
(apidocs.shiprocket.in) and a real third-party SDK quoting live endpoint
paths, not guessed: auth is POST /v1/external/auth/login (email+password
-> a bearer token valid 240 hours), NDR is its own dedicated resource
(GET /v1/external/ndr/all), not folded into generic order status. A
webhook-push design was the original plan, but Shiprocket's own webhook
signature/payload wire format could not be confirmed from any real
source - shipping against a guessed signature scheme is exactly the kind
of "looks built, doesn't actually work" gap this batch is explicitly
trying to avoid. Polling with confirmed endpoints is what actually works
today; a webhook receiver is a legitimate v2 once that spec is read
directly from a live account.

One more honest gap, stated plainly rather than hidden: the NDR list
endpoint's own response field names for AWB / delivery-failure-reason
were not confirmed from documentation - only that the endpoint itself is
real. This module tries the field names Shiprocket's own other confirmed
endpoints suggest (awb_code, matching the /track/awb/{awb_code} URL
param), keeps the full raw entry either way, and logs loudly the first
time a poll returns something it cannot map - so a real mismatch surfaces
immediately in production logs instead of silently doing nothing.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.encryption import decrypt, encrypt
from shared.db.models import Business, Customer, Order, OrderStatus, ShippingConnection
from shared.utils.logging import get_logger

logger = get_logger(__name__)

_BASE_URL = "https://apiv2.shiprocket.in/v1/external"
_TIMEOUT = 25.0
# Refresh once within this much of the confirmed 240-hour (10-day)
# expiry - same headroom reasoning as every other token-refresh job in
# this codebase (shared/channels/whatsapp/token_refresh.py's own
# REFRESH_WINDOW).
_REFRESH_WINDOW = timedelta(hours=24)
_TOKEN_LIFETIME = timedelta(hours=240)

# Candidate field names for the AWB on one NDR entry - Shiprocket's own
# confirmed endpoint (/track/awb/{awb_code}) strongly suggests "awb_code"
# is the real key, but this stays defensive rather than assuming a
# single name is certainly right.
_AWB_KEYS = ("awb_code", "awb", "awb_number")


class ShiprocketError(Exception):
    """A Shiprocket API call failed. Safe to log, not to show a customer."""


async def _login(email: str, password: str) -> str:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{_BASE_URL}/auth/login", json={"email": email, "password": password}
        )
    if response.status_code != 200:
        raise ShiprocketError(f"Shiprocket login rejected: {response.status_code}")
    token = response.json().get("token")
    if not token:
        raise ShiprocketError("Shiprocket login returned no token")
    return token


async def _valid_token(db: AsyncSession, connection: ShippingConnection) -> str | None:
    """Re-authenticates when the cached token is missing or near its
    confirmed 240-hour expiry. Never raises - a failed login here must
    not crash the whole sweep for every other connected business."""
    now = datetime.now(timezone.utc)
    needs_login = (
        not connection.access_token
        or connection.token_expires_at is None
        or connection.token_expires_at - now <= _REFRESH_WINDOW
    )
    if not needs_login:
        return decrypt(connection.access_token)

    try:
        token = await _login(connection.email, decrypt(connection.password))
    except ShiprocketError as exc:
        logger.error("shiprocket login failed business=%s: %s", connection.business_id, exc)
        return None

    connection.access_token = encrypt(token)
    connection.token_expires_at = now + _TOKEN_LIFETIME
    await db.flush()
    return token


async def _get(token: str, path: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(
                f"{_BASE_URL}{path}", headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as exc:
        logger.error("shiprocket request failed %s: %s", path, exc)
        return None
    if response.status_code != 200:
        logger.error("shiprocket request rejected %s: %s %s", path, response.status_code, response.text[:300])
        return None
    return response.json()


def _awb_from_entry(entry: dict) -> str | None:
    for key in _AWB_KEYS:
        value = entry.get(key)
        if value:
            return str(value)
    return None


async def sync_business(db: AsyncSession, connection: ShippingConnection) -> int:
    """
    Poll NDR for one business's Shiprocket account, match entries back to
    Order rows by tracking_number (AWB), stamp ndr_at on the ones not
    already flagged, fire the reschedule WhatsApp message. Returns how
    many new NDR events were processed.
    """
    token = await _valid_token(db, connection)
    if token is None:
        return 0

    payload = await _get(token, "/ndr/all")
    if payload is None:
        return 0

    entries = payload.get("data") or payload.get("ndr_orders") or []
    if not isinstance(entries, list):
        logger.warning("shiprocket NDR response shape unexpected business=%s: %r", connection.business_id, type(entries))
        return 0

    processed = 0
    for entry in entries:
        awb = _awb_from_entry(entry)
        if not awb:
            logger.warning("shiprocket NDR entry had no recognisable AWB field business=%s keys=%s", connection.business_id, list(entry.keys()))
            continue

        order = (
            await db.execute(
                select(Order).where(
                    Order.business_id == connection.business_id,
                    Order.tracking_number == awb,
                    Order.ndr_at.is_(None),
                )
            )
        ).scalars().first()
        if order is None:
            continue  # already flagged, or not one of ours - either way, nothing new to do

        order.ndr_at = datetime.now(timezone.utc)
        processed += 1

        if order.customer_id is None:
            continue
        business = await db.get(Business, connection.business_id)
        customer = await db.get(Customer, order.customer_id)
        if business is None or customer is None:
            continue

        from shared.scheduling import notify
        try:
            sent = await notify.send_ndr_reschedule_request(
                db, business=business, customer=customer,
                order_number=order.order_number or str(order.id),
            )
            if sent:
                order.ndr_reschedule_sent_at = datetime.now(timezone.utc)
        except Exception:
            logger.exception("NDR reschedule send failed order=%s", order.id)

    return processed


async def sync_all(db: AsyncSession) -> int:
    """Every active ShippingConnection, one poll each. A single business's
    Shiprocket failure (bad credentials, API outage) must never block
    every other connected business's own sweep."""
    result = await db.execute(
        select(ShippingConnection).where(
            ShippingConnection.platform == "shiprocket", ShippingConnection.active.is_(True)
        )
    )
    connections = list(result.scalars().all())
    if not connections:
        return 0

    total = 0
    for connection in connections:
        try:
            total += await sync_business(db, connection)
        except Exception:
            logger.exception("shiprocket sync failed business=%s", connection.business_id)

    return total

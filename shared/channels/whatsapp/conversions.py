"""
Closing the Click-to-WhatsApp ad attribution loop.

Capturing ctwa_clid (see shared/channels/ingest.py) only gets a business
half the value - the other half is telling Meta a real conversion happened,
so their ad system can actually optimise toward customers who convert
rather than customers who merely reply. Per Meta's Automatic Events API for
business messaging: POST to a Dataset - a Business Manager asset the
business owns, not something Krova can supply the way app_id/config_id are
supplied for Embedded Signup. Each connected business configures their own,
the same shape as a Shopify webhook secret.

Uses the business's own stored WhatsApp access token (the business
integration system user token from Embedded Signup) rather than a second
credential - that token already carries the permissions this needs, and
storing a third secret per business for the same relationship would be
exactly the kind of credential sprawl this schema has avoided everywhere
else.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class ConversionEventError(Exception):
    """Meta rejected the event. Never raised for 'no dataset configured' -
    that is a normal, common state, not a failure."""


@dataclass(slots=True)
class ConversionResult:
    sent: bool
    events_received: int | None = None
    reason: str | None = None


async def send_conversion_event(
    *,
    dataset_id: str,
    access_token: str,
    ctwa_clid: str,
    event_name: str,
    value_paise: int | None = None,
    currency: str = "INR",
) -> ConversionResult:
    """
    Tell Meta a real conversion happened for this ad click.

    value_paise is converted to whole currency units for Meta (it wants
    123.45, not paise) - the one place in this codebase that leaves the
    integer-paise convention, because the receiving API requires it.
    """
    custom_data: dict = {}
    if value_paise is not None:
        custom_data = {"currency": currency, "value": round(value_paise / 100, 2)}

    body = {
        "data": [
            {
                "event_name": event_name,
                "event_time": int(datetime.now(timezone.utc).timestamp()),
                "action_source": "business_messaging",
                "messaging_channel": "whatsapp",
                "user_data": {"ctwa_clid": ctwa_clid},
                **({"custom_data": custom_data} if custom_data else {}),
                "messaging_outcome_data": {"outcome_type": "automatic_events"},
            }
        ],
        "partner_agent": "krova",
    }

    url = f"{settings.graph_base_url}/{dataset_id}/events"
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(url, json=body, headers={"Authorization": f"Bearer {access_token}"})

    if res.status_code != 200:
        logger.warning(
            "conversion event rejected for dataset=%s event=%s: %s",
            dataset_id, event_name, res.text[:300],
        )
        raise ConversionEventError(f"Meta rejected the conversion event ({res.status_code})")

    received = res.json().get("events_received")
    logger.info(
        "conversion event sent dataset=%s event=%s ctwa_clid=%s...%s",
        dataset_id, event_name, ctwa_clid[:12], ctwa_clid[-6:],
    )
    return ConversionResult(sent=True, events_received=received)


async def send_conversion_for_customer(
    *,
    dataset_id: str | None,
    access_token: str,
    customer_ctwa_clid: str | None,
    event_name: str,
    value_paise: int | None = None,
) -> ConversionResult:
    """
    The everyday entry point: a customer, an event, done.

    Both a missing dataset_id (business hasn't configured ad tracking) and
    a missing ctwa_clid (this customer never came from an ad) are the
    normal, common case for most conversions - neither is an error, and
    callers (an order marked paid, an appointment booked) should never have
    to check both conditions themselves before deciding whether to call
    this.
    """
    if not dataset_id:
        return ConversionResult(sent=False, reason="no_dataset_configured")
    if not customer_ctwa_clid:
        return ConversionResult(sent=False, reason="not_from_an_ad")

    return await send_conversion_event(
        dataset_id=dataset_id,
        access_token=access_token,
        ctwa_clid=customer_ctwa_clid,
        event_name=event_name,
        value_paise=value_paise,
    )

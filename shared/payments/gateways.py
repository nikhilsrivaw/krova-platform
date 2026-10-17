"""
A business's own payment gateway, behind one small adapter.

Every gateway needs only three things from KROVA: check the key works, create a link for an amount, and say whether a
link has been paid. Adding Cashfree or PayU later is one more class with these three methods; nothing else changes.

Razorpay (docs read 2026-10-18):
  create  POST https://api.razorpay.com/v1/payment_links    Basic auth key_id:key_secret
          amount in paise (min 100), currency, description, reference_id (<= 40 chars, unique per link),
          customer {name, contact}, notify {sms, email}, expire_by (unix seconds, >= 15 minutes ahead)
          -> id (plink_...), short_url, status
  fetch   GET  https://api.razorpay.com/v1/payment_links/{id}
          -> status: created | partially_paid | paid | expired | cancelled, amount, amount_paid
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

TIMEOUT = 20.0
LINK_LIFETIME = timedelta(days=7)


class GatewayError(Exception):
    """The gateway refused or could not be reached; the message is safe to show the owner."""


@dataclass(slots=True)
class LinkInfo:
    external_id: str
    short_url: str
    status: str
    amount_paise: int
    amount_paid_paise: int
    expires_at: datetime | None


class Razorpay:
    name = "razorpay"
    base = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str) -> None:
        self._auth = (key_id, key_secret)

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                response = await http.request(method, f"{self.base}{path}", auth=self._auth, **kwargs)
        except httpx.HTTPError as exc:
            raise GatewayError("Could not reach Razorpay. Try again in a minute.") from exc
        if response.status_code in (401, 403):
            raise GatewayError("Razorpay did not accept this Key ID and Secret. Check them in Razorpay > Account & Settings > API Keys.")
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("description") or response.text
            except ValueError:
                detail = response.text
            raise GatewayError(f"Razorpay: {str(detail)[:200]}")
        return response.json()

    async def check(self) -> None:
        """A harmless read that fails on a wrong key - nothing is created."""
        await self._call("GET", "/payments", params={"count": 1})

    async def create_link(
        self, *, amount_paise: int, description: str, customer_name: str | None, customer_phone: str | None,
        now: datetime | None = None,
    ) -> LinkInfo:
        if amount_paise < 100:
            raise GatewayError("The amount must be at least Rs 1.")
        now = now or datetime.now(timezone.utc)
        expires = now + LINK_LIFETIME
        body: dict = {
            "amount": amount_paise,
            "currency": "INR",
            "description": (description or "Payment")[:2048],
            # unique per link and within Razorpay's 40 characters
            "reference_id": f"krova-{uuid.uuid4().hex[:30]}",
            # KROVA sends the link on WhatsApp itself
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "expire_by": int(expires.timestamp()),
        }
        customer = {}
        if customer_name:
            customer["name"] = customer_name[:100]
        if customer_phone:
            digits = "".join(ch for ch in customer_phone if ch.isdigit() or ch == "+")
            if 8 <= len(digits) <= 14:
                customer["contact"] = digits
        if customer:
            body["customer"] = customer
        data = await self._call("POST", "/payment_links", json=body)
        return _info(data, fallback_expiry=expires)

    async def fetch_link(self, external_id: str) -> LinkInfo:
        return _info(await self._call("GET", f"/payment_links/{external_id}"))


def _info(data: dict, fallback_expiry: datetime | None = None) -> LinkInfo:
    expire_by = data.get("expire_by")
    return LinkInfo(
        external_id=str(data.get("id") or ""),
        short_url=str(data.get("short_url") or ""),
        status=str(data.get("status") or "created"),
        amount_paise=int(data.get("amount") or 0),
        amount_paid_paise=int(data.get("amount_paid") or 0),
        expires_at=datetime.fromtimestamp(expire_by, tz=timezone.utc) if expire_by else fallback_expiry,
    )


GATEWAYS = {"razorpay": Razorpay}


def gateway_for(name: str, key_id: str, key_secret: str):
    cls = GATEWAYS.get(name)
    if cls is None:
        raise GatewayError(f"{name} is not supported yet.")
    return cls(key_id, key_secret)

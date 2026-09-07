"""
Outbound transactional email via Postmark - one shared, Krova-level
account (settings.postmark_account_token / postmark_server_token, not a
per-business connection), because for a multi-tenant platform Postmark
does per-customer reputation isolation natively: a bounce or spam
complaint against one business's sender signature does not hurt another
business's deliverability. Confirmed via research before AWS SES or
Resend were ruled out - both push that isolation work onto Krova instead.

Two Postmark credentials, genuinely different, both confirmed against
Postmark's own API docs before being used:

  X-Postmark-Account-Token - manages sender signatures (create, check
  verification status). Account-level, not tied to a sending volume.

  X-Postmark-Server-Token - sends mail through Krova's one Postmark
  server, using whichever business's signature is passed as `From`.

v1 uses a sender signature (one confirmed email address, no DNS changes -
Postmark emails a confirmation link to that address) rather than full
domain/DKIM verification - the same "start with what's confirmed simple"
reasoning Shiprocket's login-based auth used over a bigger OAuth app.
"""

from dataclasses import dataclass

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

API_BASE = "https://api.postmarkapp.com"


class PostmarkError(Exception):
    """Postmark rejected the request. The message is safe to show a person."""


@dataclass(slots=True)
class SenderSignature:
    id: str
    confirmed: bool


async def create_signature(from_email: str, *, business_name: str) -> SenderSignature:
    """Register a new sender signature - Postmark emails a confirmation
    link to from_email; it is not usable for sending until that link is
    clicked (see check_signature_verified)."""
    if not settings.postmark_account_token:
        raise PostmarkError("Email sending is not configured on this server")

    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            f"{API_BASE}/senders",
            json={"FromEmail": from_email, "Name": business_name},
            headers={
                "X-Postmark-Account-Token": settings.postmark_account_token,
                "Accept": "application/json",
            },
        )

    if res.status_code != 200:
        logger.error("postmark signature creation failed email=%s status=%s body=%s",
                      from_email, res.status_code, res.text[:500])
        raise PostmarkError(f"Postmark rejected this address ({res.status_code}): {res.text[:300]}")

    data = res.json()
    return SenderSignature(id=str(data["ID"]), confirmed=bool(data.get("Confirmed")))


async def check_signature_verified(signature_id: str) -> bool:
    """Poll whether a business has clicked Postmark's own confirmation link yet."""
    if not settings.postmark_account_token:
        raise PostmarkError("Email sending is not configured on this server")

    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.get(
            f"{API_BASE}/senders/{signature_id}",
            headers={
                "X-Postmark-Account-Token": settings.postmark_account_token,
                "Accept": "application/json",
            },
        )

    if res.status_code != 200:
        logger.warning("postmark signature status check failed id=%s status=%s", signature_id, res.status_code)
        return False

    return bool(res.json().get("Confirmed"))


async def send_email(*, from_email: str, to: str, subject: str, text_body: str) -> None:
    """Send one transactional email. Caller is responsible for confirming
    the sending EmailSendConnection is verified before calling this -
    Postmark itself would refuse an unconfirmed From address anyway, but
    failing that closer to the caller gives a clearer error."""
    if not settings.postmark_server_token:
        raise PostmarkError("Email sending is not configured on this server")

    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await client.post(
            f"{API_BASE}/email",
            json={"From": from_email, "To": to, "Subject": subject, "TextBody": text_body},
            headers={
                "X-Postmark-Server-Token": settings.postmark_server_token,
                "Accept": "application/json",
            },
        )

    if res.status_code != 200:
        logger.error("postmark send failed from=%s to=%s status=%s body=%s",
                      from_email, to, res.status_code, res.text[:500])
        raise PostmarkError(f"Postmark rejected this email ({res.status_code}): {res.text[:300]}")

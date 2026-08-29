"""
Webhook signature verification for Instagram.

Same HMAC-SHA256-over-the-raw-body scheme as WhatsApp's - see
shared/channels/whatsapp/signature.py for the full reasoning.

Two different secrets are accepted here, on purpose, because two different
integration paths can both deliver to this same callback URL: "Instagram
API with Instagram Login" signs with its own separate app secret
(meta_instagram_app_secret), while Instagram messaging via Facebook Login
for Business signs with the main app's secret (meta_app_secret) - the same
one WhatsApp already uses. Trying both rather than picking one keeps this
endpoint working regardless of which path a given webhook came through.
"""

import hashlib
import hmac

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

_PREFIX = "sha256="


class InvalidSignature(Exception):
    """The request did not come from Meta, or was altered on the way."""


def _matches(secret: str, raw_body: bytes, received: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def verify(raw_body: bytes, signature_header: str | None) -> None:
    """Check an Instagram webhook signature against either valid secret. Raises on failure."""
    if not signature_header or not signature_header.startswith(_PREFIX):
        logger.warning("instagram webhook rejected: missing or malformed signature header")
        raise InvalidSignature("Signature missing or malformed")

    received = signature_header[len(_PREFIX) :]

    for secret in (settings.meta_app_secret, settings.meta_instagram_app_secret):
        if secret and _matches(secret, raw_body, received):
            return

    logger.warning("instagram webhook rejected: signature mismatch - possible spoof attempt")
    raise InvalidSignature("Signature does not match")


def verify_subscription(mode: str | None, token: str | None) -> bool:
    """
    Answer Meta's GET verification handshake for the Instagram webhook.

    The verify token itself is one we chose and typed into Meta's dashboard
    ourselves - reusing the same META_WEBHOOK_VERIFY_TOKEN as WhatsApp is
    fine, since it isn't a cryptographic secret, just a shared password for
    this one-time handshake.
    """
    if not settings.meta_webhook_verify_token:
        logger.error("META_WEBHOOK_VERIFY_TOKEN is not set - cannot verify subscription")
        return False
    return mode == "subscribe" and hmac.compare_digest(
        token or "", settings.meta_webhook_verify_token
    )

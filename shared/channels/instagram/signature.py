"""
Webhook signature verification for Instagram.

Same HMAC-SHA256-over-the-raw-body scheme as WhatsApp's - see
shared/channels/whatsapp/signature.py for the full reasoning. The one thing
that differs, and the reason this isn't just a re-export: "Instagram API
with Instagram Login" issues its own separate app id and secret, distinct
from the main Meta app used for WhatsApp. A webhook for Instagram messages
or comments is signed with THAT secret - verifying against
meta_app_secret would make every legitimate Instagram webhook fail.
"""

import hashlib
import hmac

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

_PREFIX = "sha256="


class InvalidSignature(Exception):
    """The request did not come from Meta, or was altered on the way."""


def verify(raw_body: bytes, signature_header: str | None) -> None:
    """Check an Instagram webhook signature. Raises InvalidSignature on any failure."""
    if not settings.meta_instagram_app_secret:
        raise InvalidSignature("META_INSTAGRAM_APP_SECRET is not configured")

    if not signature_header or not signature_header.startswith(_PREFIX):
        logger.warning("instagram webhook rejected: missing or malformed signature header")
        raise InvalidSignature("Signature missing or malformed")

    received = signature_header[len(_PREFIX) :]
    expected = hmac.new(
        settings.meta_instagram_app_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received):
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

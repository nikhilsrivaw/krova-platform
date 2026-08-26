"""
Webhook signature verification.

Meta signs every webhook with an HMAC-SHA256 of the raw body, keyed on the app
secret, in an X-Hub-Signature-256 header. Without this check, anyone who
learns the webhook URL can post whatever they like into a business's
conversation history - fake customer messages, fake commitments, fake amounts
owed. The URL is not a secret; the signature is what proves the sender.

Two details matter and both are easy to get wrong:

  The raw bytes. Verification must run against exactly what arrived. Parse the
  JSON first and re-serialise it and the bytes differ, so the MAC differs, and
  every legitimate webhook fails.

  Constant-time comparison. A plain == returns early on the first differing
  byte, and that timing difference is enough to recover a valid signature one
  byte at a time.
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
    """
    Check a Meta webhook signature. Raises InvalidSignature on any failure.

    Every failure raises the same exception with no detail about which check
    failed - distinguishing "no header" from "bad MAC" in a response tells a
    prober how far they got.
    """
    if not settings.meta_app_secret:
        raise InvalidSignature("META_APP_SECRET is not configured")

    if not signature_header or not signature_header.startswith(_PREFIX):
        logger.warning("webhook rejected: missing or malformed signature header")
        raise InvalidSignature("Signature missing or malformed")

    received = signature_header[len(_PREFIX) :]
    expected = hmac.new(
        settings.meta_app_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received):
        logger.warning("webhook rejected: signature mismatch - possible spoof attempt")
        raise InvalidSignature("Signature does not match")


def verify_subscription(mode: str | None, token: str | None) -> bool:
    """
    Answer Meta's GET verification handshake.

    Sent once when the webhook URL is registered: Meta expects hub.challenge
    echoed back, but only if hub.verify_token matches what we configured.
    """
    if not settings.meta_webhook_verify_token:
        logger.error("META_WEBHOOK_VERIFY_TOKEN is not set - cannot verify subscription")
        return False
    return mode == "subscribe" and hmac.compare_digest(
        token or "", settings.meta_webhook_verify_token
    )

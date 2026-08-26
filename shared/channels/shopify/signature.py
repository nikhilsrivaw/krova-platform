"""
Shopify webhook signature verification.

Same purpose as shared/channels/whatsapp/signature.py, different scheme:
Shopify signs with HMAC-SHA256 over the raw body, base64-encoded (not hex),
in an X-Shopify-Hmac-Sha256 header - and the key is per-store, not one app
secret shared by every business. A store's webhook secret is issued when
that store's webhook is registered and lives on its StoreConnection row,
encrypted at rest like every other credential in this codebase.

The two rules from the WhatsApp module hold here for the same reasons: verify
the exact raw bytes before any JSON parsing, and compare in constant time so
a timing difference never leaks how much of a guessed signature was right.
"""

import base64
import hashlib
import hmac

from shared.utils.logging import get_logger

logger = get_logger(__name__)


class InvalidSignature(Exception):
    """The request did not come from this store, or was altered on the way."""


def verify(raw_body: bytes, signature_header: str | None, webhook_secret: str) -> None:
    """
    Check a Shopify webhook signature against one store's secret.

    Raises InvalidSignature on any failure, with no detail about which check
    failed - same reasoning as the WhatsApp module: distinguishing "no
    header" from "bad MAC" in a response tells a prober how far they got.
    """
    if not signature_header:
        logger.warning("shopify webhook rejected: missing signature header")
        raise InvalidSignature("Signature missing")

    expected = base64.b64encode(
        hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()

    if not hmac.compare_digest(expected, signature_header):
        logger.warning("shopify webhook rejected: signature mismatch - possible spoof attempt")
        raise InvalidSignature("Signature does not match")

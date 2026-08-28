"""
Parsing Meta's "signed_request" - the format the deauthorize callback (and,
historically, the Facebook JS SDK) uses to hand over a payload Meta itself
vouches for.

Shape: "<base64url signature>.<base64url JSON payload>". The signature is an
HMAC-SHA256 of the base64url payload string, keyed on the app secret - same
proof-of-origin idea as the webhook's X-Hub-Signature-256 header, just a
different envelope because this predates that header convention.
"""

import base64
import hashlib
import hmac
import json

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class InvalidSignedRequest(Exception):
    """The signed_request did not come from Meta, or was malformed."""


def _b64url_decode(data: str) -> bytes:
    # base64url omits padding; Python's decoder wants it back.
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def parse(signed_request: str) -> dict:
    """Verify and decode a signed_request, keyed on the Instagram app secret."""
    if not settings.meta_instagram_app_secret:
        raise InvalidSignedRequest("META_INSTAGRAM_APP_SECRET is not configured")

    try:
        encoded_sig, encoded_payload = signed_request.split(".", 1)
    except ValueError as exc:
        raise InvalidSignedRequest("Malformed signed_request") from exc

    expected_sig = hmac.new(
        settings.meta_instagram_app_secret.encode(),
        encoded_payload.encode(),
        hashlib.sha256,
    ).digest()

    try:
        received_sig = _b64url_decode(encoded_sig)
    except Exception as exc:
        raise InvalidSignedRequest("Malformed signature") from exc

    if not hmac.compare_digest(expected_sig, received_sig):
        logger.warning("signed_request rejected: signature mismatch")
        raise InvalidSignedRequest("Signature does not match")

    try:
        payload = json.loads(_b64url_decode(encoded_payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidSignedRequest("Malformed payload") from exc

    if not isinstance(payload, dict):
        raise InvalidSignedRequest("Payload was not a JSON object")

    return payload

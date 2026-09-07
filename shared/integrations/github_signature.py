"""
Webhook signature verification for GitHub.

Same HMAC-SHA256-over-the-raw-body scheme as Instagram's/WhatsApp's -
confirmed against GitHub's own webhook docs, not guessed: the
`X-Hub-Signature-256` header carries `sha256=<hex digest>`, keyed on the
secret the business themselves set when creating the webhook in their
repo's own settings (GitHubConnection.webhook_secret - entered, not
generated, since GitHub has no self-serve "issue me a secret" flow the
way Shopify's app install does).
"""

import hashlib
import hmac

from shared.utils.logging import get_logger

logger = get_logger(__name__)

_PREFIX = "sha256="


class InvalidSignature(Exception):
    """The request did not come from GitHub, or was altered on the way."""


def verify(raw_body: bytes, signature_header: str | None, secret: str) -> None:
    """Check a GitHub webhook signature. Raises on failure."""
    if not signature_header or not signature_header.startswith(_PREFIX):
        logger.warning("github webhook rejected: missing or malformed signature header")
        raise InvalidSignature("Signature missing or malformed")

    received = signature_header[len(_PREFIX):]
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received):
        logger.warning("github webhook rejected: signature mismatch - possible spoof attempt")
        raise InvalidSignature("Signature does not match")

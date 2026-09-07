"""
Public API keys - a business's own backend calling Krova directly (see
services/api/routers/public_api.py), not the embeddable widget.

Only a hash is ever stored (see ApiKey's own docstring for why this is
correct, not merely convenient) - hashing and comparing both happen here
so no call site reimplements the same SHA-256 either way.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from shared.db.models import ApiKey

_PREFIX = "krova_live_"

# Business-wide, same fixed-window shape as shared/channels/web/
# guardrails.py's own rate limiter - generous for a real integration's
# actual traffic, tight enough to block a runaway or malicious caller.
_RATE_LIMIT_WINDOW = timedelta(minutes=1)
_RATE_LIMIT_MAX_REQUESTS = 60


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate() -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix). The raw key is returned
    exactly once - nothing after this call can recover it from storage."""
    raw_key = _PREFIX + secrets.token_urlsafe(32)
    return raw_key, hash_key(raw_key), raw_key[: len(_PREFIX) + 8]


def verify(raw_key: str, key_hash: str) -> bool:
    return hmac.compare_digest(hash_key(raw_key), key_hash)


def check_rate_limit(api_key: ApiKey) -> bool:
    """
    True if this request is allowed, mutating api_key's own counter either
    way - same contract as shared/channels/web/guardrails.py's
    check_rate_limit, copied rather than shared for two call sites, not
    abstracted prematurely.
    """
    now = datetime.now(timezone.utc)
    window_started = api_key.rate_limit_window_started_at

    if window_started is None or now - window_started > _RATE_LIMIT_WINDOW:
        api_key.rate_limit_window_started_at = now
        api_key.rate_limit_count = 1
        return True

    if api_key.rate_limit_count >= _RATE_LIMIT_MAX_REQUESTS:
        return False

    api_key.rate_limit_count += 1
    return True

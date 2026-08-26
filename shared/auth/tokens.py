"""
Access and refresh tokens.

Two tokens with different jobs. The access token is short-lived and stateless -
it is checked by signature alone, with no database round trip, so it stays
cheap enough to verify on every request. The refresh token is long-lived and
stateful: one row per issued token, so sessions can actually be ended.

The refresh token is stored as a SHA-256 hash. A leaked database should not
hand an attacker working sessions. Hashing is enough here (unlike passwords,
which need argon2) because the token is 256 bits of randomness, not something
a human chose - there is no dictionary to run against it.

Rotation: using a refresh token consumes it and issues a new one. If a
consumed token is presented again, it was either replayed or stolen, and the
whole family is revoked.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt

from shared.config.settings import settings

TokenType = Literal["access", "refresh"]


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired, or not ours."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Access tokens ────────────────────────────────────────────────────────────

def create_access_token(
    user_id: uuid.UUID,
    business_id: uuid.UUID | None = None,
    role: str | None = None,
) -> str:
    """
    Issue a short-lived access token.

    business_id and role travel in the token so ordinary requests need no
    membership lookup. The trade-off is that a role change only takes effect
    when the token next refreshes - minutes, not immediately - which is why
    access tokens are short-lived and anything destructive re-checks the
    database rather than trusting the claim.
    """
    now = _now()
    payload = {
        "sub": str(user_id),
        "typ": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        "jti": secrets.token_urlsafe(8),
    }
    if business_id is not None:
        payload["biz"] = str(business_id)
    if role is not None:
        payload["role"] = role

    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """
    Verify an access token and return its claims.

    Raises TokenError on anything wrong. The algorithm is pinned to the one we
    issue with: accepting whatever the token's own header names is how JWT
    algorithm-confusion attacks work.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid") from exc

    if claims.get("typ") != "access":
        # A refresh token presented as an access token must not be honoured.
        raise TokenError("Wrong token type")

    return claims


# ── Refresh tokens ───────────────────────────────────────────────────────────

def generate_refresh_token() -> tuple[str, str]:
    """
    Make a refresh token.

    Returns (token, token_hash). The token goes to the client once and is
    never stored; only the hash is persisted.
    """
    token = secrets.token_urlsafe(48)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def refresh_token_expiry() -> datetime:
    return _now() + timedelta(days=settings.refresh_token_ttl_days)

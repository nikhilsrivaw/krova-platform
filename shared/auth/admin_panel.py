"""
Sign-in for KROVA's own admin panel (/admin). Completely separate from customer accounts: a short list of
staff emails, each with a salted PBKDF2 hash of their password, read from the server's ADMIN_PANEL_USERS.
A successful sign-in returns a token that only the /admin routes accept (header X-Admin-Token).

Plain passwords are never stored in the code or the repo; only the hashes live in the server's .env.
"""

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

import jwt

from shared.config.settings import settings

ITERATIONS = 600_000
TOKEN_HOURS = 12
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60

# email -> timestamps of recent failures. In memory: a restart clears it, which is fine for a lockout.
_failures: dict[str, list[float]] = {}


class AdminAuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """(salt_hex, hash_hex)."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return salt.hex(), digest.hex()


def format_entry(email: str, password: str) -> str:
    salt, digest = hash_password(password)
    return f"{email.strip().lower()}|{salt}|{digest}"


def _users() -> dict[str, tuple[bytes, bytes]]:
    out: dict[str, tuple[bytes, bytes]] = {}
    for entry in settings.admin_panel_users.split(","):
        parts = entry.strip().split("|")
        if len(parts) != 3:
            continue
        email, salt, digest = parts
        try:
            out[email.strip().lower()] = (bytes.fromhex(salt), bytes.fromhex(digest))
        except ValueError:
            continue
    return out


def _recent_failures(email: str, now: float) -> list[float]:
    recent = [t for t in _failures.get(email, []) if now - t < LOCK_SECONDS]
    _failures[email] = recent
    return recent


def login(email: str, password: str) -> str:
    """Check the credentials and return a token, or raise AdminAuthError."""
    users = _users()
    if not users:
        raise AdminAuthError("The admin panel sign-in is not switched on.", 403)
    key = (email or "").strip().lower()
    now = time.time()
    if len(_recent_failures(key, now)) >= MAX_FAILURES:
        raise AdminAuthError("Too many wrong attempts. Try again in 15 minutes.", 429)

    salt, expected = users.get(key, (b"\x00" * 16, b"\x00" * 32))  # unknown email still costs a full hash
    got = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, ITERATIONS)
    if key not in users or not hmac.compare_digest(got, expected):
        _failures.setdefault(key, []).append(now)
        raise AdminAuthError("Wrong email or password.")
    _failures.pop(key, None)

    issued = datetime.now(timezone.utc)
    return jwt.encode(
        {"typ": "admin_panel", "sub": key, "iat": issued, "exp": issued + timedelta(hours=TOKEN_HOURS)},
        settings.jwt_secret, algorithm=settings.jwt_algorithm,
    )


def verify_token(token: str | None) -> str:
    """The signed-in staff email, or AdminAuthError. A customer's login token is not accepted here."""
    if not token:
        raise AdminAuthError("Sign in to the admin panel.")
    try:
        claims = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm], options={"require": ["exp", "sub"]}
        )
    except jwt.InvalidTokenError as exc:
        raise AdminAuthError("Your admin session has expired. Sign in again.") from exc
    email = str(claims.get("sub") or "")
    if claims.get("typ") != "admin_panel" or email not in _users():
        raise AdminAuthError("Sign in to the admin panel.")
    return email

"""
Registration, sign-in, and session lifecycle.

Everything that touches credentials lives here rather than in the router, so
the rules hold no matter which entry point calls them.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.passwords import hash_password, needs_rehash, verify_password
from shared.auth.tokens import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
    refresh_token_expiry,
)
from shared.db.models import (
    Business,
    BusinessDNA,
    BusinessMember,
    BusinessRole,
    RefreshToken,
    User,
)
from shared.verticals import seed_dna
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class AuthError(Exception):
    """Anything that should stop a sign-in or registration."""


class EmailAlreadyRegistered(AuthError):
    pass


class InvalidCredentials(AuthError):
    pass


class AccountDisabled(AuthError):
    pass


@dataclass(slots=True)
class Session:
    access_token: str
    refresh_token: str
    user: User
    business: Business | None


def normalise_email(email: str) -> str:
    return email.strip().lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _issue_session(
    user: User, business: Business | None, role: str | None, db: AsyncSession
) -> Session:
    """Mint an access token and persist a refresh token for this sign-in."""
    access = create_access_token(
        user.id,
        business_id=business.id if business else None,
        role=role,
    )
    refresh, refresh_hash = generate_refresh_token()

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=refresh_token_expiry(),
            created_at=_now(),
        )
    )
    return Session(
        access_token=access, refresh_token=refresh, user=user, business=business
    )


async def register(
    email: str,
    password: str,
    full_name: str | None,
    business_name: str,
    vertical: str,
    db: AsyncSession,
) -> Session:
    """
    Create an account, its first business, and sign the person in.

    Registration creates the business too. A user with no business cannot do
    anything in Krova, and leaving that state reachable means every later
    query has to handle a case that should not exist.
    """
    email = normalise_email(email)

    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise EmailAlreadyRegistered("That email is already registered")

    user = User(
        email=email,
        password_hash=hash_password(password),
        full_name=full_name,
        is_active=True,
    )
    db.add(user)
    await db.flush()  # assign user.id before the rows that reference it

    business = Business(name=business_name.strip(), vertical=vertical)
    db.add(business)
    await db.flush()

    db.add(
        BusinessMember(
            business_id=business.id, user_id=user.id, role=BusinessRole.owner
        )
    )

    # Seed the business's DNA from its vertical template, so the agent knows
    # how this kind of business speaks and what it must never answer before
    # the first conversation happens. Only what a template can honestly know -
    # prices and hours stay empty until the owner fills them in.
    db.add(BusinessDNA(business_id=business.id, **seed_dna(vertical)))

    session = await _issue_session(user, business, BusinessRole.owner.value, db)
    logger.info("registered user=%s business=%s vertical=%s", user.id, business.id, vertical)
    return session


async def authenticate(email: str, password: str, db: AsyncSession) -> Session:
    """
    Verify credentials and start a session.

    A missing user and a wrong password raise the same error with the same
    message: telling an attacker which addresses are registered turns a
    password guess into an account enumeration.
    """
    email = normalise_email(email)
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None:
        # Hash anyway so a missing account does not answer faster than a wrong
        # password - the timing difference is enough to enumerate addresses.
        hash_password("timing-equalisation-placeholder")
        raise InvalidCredentials("Email or password is incorrect")

    if not verify_password(password, user.password_hash):
        raise InvalidCredentials("Email or password is incorrect")

    if not user.is_active:
        raise AccountDisabled("This account has been disabled")

    # A successful sign-in is the only moment we hold the plaintext, so it is
    # the only moment we can quietly upgrade an old hash.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.last_login_at = _now()

    membership = await _primary_membership(user.id, db)
    business, role = membership if membership else (None, None)

    return await _issue_session(user, business, role, db)


async def refresh_session(refresh_token: str, db: AsyncSession) -> Session:
    """
    Exchange a refresh token for a new pair, and consume the old one.

    Rotation with reuse detection: presenting an already-revoked token means
    it was replayed or stolen, so every session for that user is ended rather
    than just refusing the request.
    """
    token_hash = hash_refresh_token(refresh_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    stored = result.scalar_one_or_none()

    if stored is None:
        raise InvalidCredentials("Session is not valid")

    if stored.revoked_at is not None:
        logger.warning(
            "revoked refresh token replayed user=%s - revoking all sessions",
            stored.user_id,
        )
        await revoke_all_sessions(stored.user_id, db)
        # Commit before raising. The caller turns this exception into a 401,
        # and the session dependency rolls back on the way out - which would
        # otherwise undo the very revocation this branch exists to perform,
        # leaving a stolen token working.
        await db.commit()
        raise InvalidCredentials("Session is not valid")

    if stored.expires_at <= _now():
        raise InvalidCredentials("Session has expired")

    stored.revoked_at = _now()

    user = await db.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise InvalidCredentials("Session is not valid")

    membership = await _primary_membership(user.id, db)
    business, role = membership if membership else (None, None)

    return await _issue_session(user, business, role, db)


async def revoke_session(refresh_token: str, db: AsyncSession) -> None:
    """Sign out one session. Silent if the token is already gone."""
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_token(refresh_token)
        )
    )
    stored = result.scalar_one_or_none()
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = _now()


async def revoke_all_sessions(user_id: uuid.UUID, db: AsyncSession) -> int:
    """Sign out everywhere. Used on password change and on token reuse."""
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
    )
    tokens = result.scalars().all()
    now = _now()
    for token in tokens:
        token.revoked_at = now
    return len(tokens)


async def _primary_membership(
    user_id: uuid.UUID, db: AsyncSession
) -> tuple[Business, str] | None:
    """The business this user acts for. Owner wins when they belong to several."""
    result = await db.execute(
        select(Business, BusinessMember.role)
        .join(BusinessMember, BusinessMember.business_id == Business.id)
        .where(BusinessMember.user_id == user_id, Business.is_active == True)  # noqa: E712
        .order_by(BusinessMember.role, Business.created_at)
    )
    row = result.first()
    if row is None:
        return None
    business, role = row
    return business, (role.value if hasattr(role, "value") else str(role))

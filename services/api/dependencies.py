"""
Request dependencies.

Tenant scoping happens here and only here. Every authenticated route receives
a CurrentUser that already carries the business it may act for, so no handler
has to remember to filter by business_id - forgetting once is a cross-customer
data leak, and "remember to filter" is not a security model.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.tokens import TokenError, decode_access_token
from shared.db.models import ApiKey, Business, BusinessMember, User
from shared.db.session import get_db
from shared.integrations import api_keys

# auto_error=False so a missing header produces our 401 with a useful message
# rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class CurrentUser:
    """Who is making this request, and what they may act on."""

    id: uuid.UUID
    email: str
    business_id: uuid.UUID | None
    role: str | None

    @property
    def business(self) -> uuid.UUID:
        """
        The business this request acts on.

        Raises rather than returning None: a route that needs a business and
        is reached without one is a bug, and it should fail loudly at the
        boundary instead of quietly querying across every tenant.
        """
        if self.business_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account is not attached to a business",
            )
        return self.business_id


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to continue",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is invalid"
        ) from exc

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is not active"
        )

    business_id: uuid.UUID | None = None
    role: str | None = claims.get("role")

    if claims.get("biz"):
        claimed = uuid.UUID(claims["biz"])
        # The token asserts a business; the database decides whether it is
        # still true. Membership can be revoked while a token is still valid,
        # and a signed claim is not proof of current access.
        result = await db.execute(
            select(BusinessMember.role)
            .join(Business, Business.id == BusinessMember.business_id)
            .where(
                BusinessMember.business_id == claimed,
                BusinessMember.user_id == user_id,
                Business.is_active == True,  # noqa: E712
            )
        )
        found = result.scalar_one_or_none()
        if found is not None:
            business_id = claimed
            role = found.value if hasattr(found, "value") else str(found)

    request.state.business_id = business_id
    return CurrentUser(
        id=user.id, email=user.email, business_id=business_id, role=role
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
DbDep = Annotated[AsyncSession, Depends(get_db)]


async def get_api_key_business(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Business:
    """
    The API-key equivalent of get_current_user, for services/api/routers/
    public_api.py only - never mixed with JWT auth. A business's own
    backend authenticates with `Authorization: Bearer krova_live_...`,
    hashed and looked up against ApiKey, same shape as widget.py's
    site_key resolution but for a bearer credential instead of a public
    embed identifier.
    """
    if credentials is None or not credentials.credentials.startswith("krova_live_"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "A valid API key is required")

    key_hash = api_keys.hash_key(credentials.credentials)
    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.active.is_(True)))
    api_key = result.scalars().first()
    if api_key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked API key")

    if not api_keys.check_rate_limit(api_key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests - please try again shortly")

    api_key.last_used_at = _now_utc()
    await db.flush()

    business = await db.get(Business, api_key.business_id)
    if business is None or not business.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked API key")
    return business


ApiKeyBusinessDep = Annotated[Business, Depends(get_api_key_business)]

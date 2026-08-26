"""
Request dependencies.

Tenant scoping happens here and only here. Every authenticated route receives
a CurrentUser that already carries the business it may act for, so no handler
has to remember to filter by business_id - forgetting once is a cross-customer
data leak, and "remember to filter" is not a security model.
"""

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.tokens import TokenError, decode_access_token
from shared.db.models import Business, BusinessMember, User
from shared.db.session import get_db

# auto_error=False so a missing header produces our 401 with a useful message
# rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)


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

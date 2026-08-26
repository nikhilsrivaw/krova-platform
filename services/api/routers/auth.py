"""
Sign up, sign in, refresh, sign out.
"""

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth import service
from shared.auth.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordTooWeak,
)
from shared.db.models import Business, User
from shared import verticals

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    full_name: str | None = Field(default=None, max_length=255)
    business_name: str = Field(min_length=1, max_length=255)
    vertical: str = Field(default="general")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SessionResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    business_id: str | None
    business_name: str | None
    vertical: str | None


class MeResponse(BaseModel):
    user_id: str
    email: str
    full_name: str | None
    business_id: str | None
    business_name: str | None
    vertical: str | None
    # What this business's vertical actually declares - e.g. "scheduling",
    # "case_tracking" - the one thing the frontend is allowed to gate a
    # nav item or a page on. Never a second, frontend-side copy of the
    # vertical->capability map: that map already lives in
    # shared/verticals/templates/*.json, and duplicating it here is exactly
    # the kind of drift the template system exists to prevent.
    capabilities: list[str]
    autonomy: str | None
    role: str | None


def _session_response(s: service.Session) -> SessionResponse:
    return SessionResponse(
        access_token=s.access_token,
        refresh_token=s.refresh_token,
        user_id=str(s.user.id),
        email=s.user.email,
        business_id=str(s.business.id) if s.business else None,
        business_name=s.business.name if s.business else None,
        vertical=s.business.vertical if s.business else None,
    )


@router.get("/verticals", tags=["auth"])
async def list_verticals() -> list[dict]:
    """The business types available at signup. Public - it precedes any account."""
    return verticals.available()


@router.post("/register", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: DbDep) -> SessionResponse:
    """
    Create an account and its first business.

    The vertical is chosen here, before anything else, because it seeds the
    business's DNA - prompts, flows, template set, dashboard defaults - so the
    agent is useful before a single conversation exists.
    """
    if body.vertical not in verticals.keys():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Unknown business type. Choose one of: "
                f"{', '.join(sorted(verticals.keys()))}"
            ),
        )

    try:
        session = await service.register(
            email=body.email,
            password=body.password,
            full_name=body.full_name,
            business_name=body.business_name,
            vertical=body.vertical,
            db=db,
        )
    except service.EmailAlreadyRegistered as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PasswordTooWeak as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return _session_response(session)


@router.post("/login", response_model=SessionResponse)
async def login(body: LoginRequest, db: DbDep) -> SessionResponse:
    try:
        session = await service.authenticate(body.email, body.password, db)
    except service.AccountDisabled as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except service.InvalidCredentials as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    return _session_response(session)


@router.post("/refresh", response_model=SessionResponse)
async def refresh(
    db: DbDep,
    refresh_token: Annotated[str, Body(embed=True)],
) -> SessionResponse:
    """
    Trade a refresh token for a new pair.

    The old token is consumed. Presenting a consumed one revokes every session
    for that user - a replayed token means it leaked.
    """
    try:
        session = await service.refresh_session(refresh_token, db)
    except service.InvalidCredentials as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    return _session_response(session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    db: DbDep,
    refresh_token: Annotated[str, Body(embed=True)],
) -> None:
    # Deliberately silent about whether the token existed. Signing out is not
    # a place to confirm whether a token is real.
    await service.revoke_session(refresh_token, db)


@router.get("/me", response_model=MeResponse)
async def me(current_user: CurrentUserDep, db: DbDep) -> MeResponse:
    """
    Everything the app shell needs to render as this person, in one call -
    identity, which business, and how much the agent may do without them -
    rather than a `UserProfile` the frontend assumed existed as its own
    endpoint but never did.
    """
    user = await db.get(User, current_user.id)
    business = (
        await db.get(Business, current_user.business_id)
        if current_user.business_id
        else None
    )
    return MeResponse(
        user_id=str(current_user.id),
        email=current_user.email,
        full_name=user.full_name if user else None,
        business_id=str(current_user.business_id) if current_user.business_id else None,
        business_name=business.name if business else None,
        vertical=business.vertical if business else None,
        capabilities=(
            verticals.get(business.vertical).get("capabilities", []) if business else []
        ),
        autonomy=business.autonomy if business else None,
        role=current_user.role,
    )


class UpdateMeRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    business_name: str | None = Field(default=None, min_length=1, max_length=255)
    vertical: str | None = None


@router.post("/me", response_model=MeResponse)
async def update_me(
    body: UpdateMeRequest, current_user: CurrentUserDep, db: DbDep
) -> MeResponse:
    """
    Onboarding and Settings both land here - a person's name is theirs, the
    business name and vertical belong to the business, so this writes to
    both rows behind one call rather than the caller having to know that.
    """
    if body.vertical is not None and body.vertical not in verticals.keys():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown vertical: {body.vertical}",
        )

    user = await db.get(User, current_user.id)
    if user is not None and body.full_name is not None:
        user.full_name = body.full_name

    business = (
        await db.get(Business, current_user.business_id)
        if current_user.business_id
        else None
    )
    if business is not None:
        if body.business_name is not None:
            business.name = body.business_name
        if body.vertical is not None:
            business.vertical = body.vertical

    await db.commit()

    return MeResponse(
        user_id=str(current_user.id),
        email=current_user.email,
        full_name=user.full_name if user else None,
        business_id=str(current_user.business_id) if current_user.business_id else None,
        business_name=business.name if business else None,
        vertical=business.vertical if business else None,
        autonomy=business.autonomy if business else None,
        role=current_user.role,
    )

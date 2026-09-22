"""
Sign up, sign in, refresh, sign out.
"""

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth import google_oauth, service
from shared.auth.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordTooWeak,
)
from shared.auth.tokens import (
    TokenError,
    create_google_handoff,
    create_google_oauth_state,
    decode_google_handoff,
    decode_google_oauth_state,
)
from shared.config.settings import settings
from shared.db.models import Business, User
from shared import verticals
from shared.verticals import labels

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
    # What this business actually has - e.g. "scheduling", "case_tracking" -
    # the one thing the frontend is allowed to gate a nav item or a page on.
    # Its vertical's declared list adjusted by the business's own overrides
    # (verticals.capabilities_for); the vertical is the default, not the
    # verdict. Never a second, frontend-side copy of the vertical->capability
    # map: that map already lives in shared/verticals/templates/*.json, and
    # duplicating it here is exactly the kind of drift the template system
    # exists to prevent.
    capabilities: list[str]
    autonomy: str | None
    role: str | None
    # What this business calls the parts of its queue, resolved across code
    # defaults, its vertical's template, and its own settings. Sent from here
    # because every page already fetches /auth/me, exactly like capabilities.
    queue_labels: dict
    # Settings.google_review_url made a first-class field on this response
    # rather than a raw settings passthrough - see UpdateMeRequest's own
    # reasoning for why this one key gets a named field instead of an
    # arbitrary-JSONB write endpoint.
    google_review_url: str | None
    # Same reasoning, same pattern - the only way this business can ever
    # turn on shared/care/commitment_deadline_calls.py's proactive voice
    # calls, which previously had no UI path to enable at all.
    proactive_deadline_calls_enabled: bool


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


# ── Google sign-in/sign-up ───────────────────────────────────────────────────
# Three hops: /google/start hands back where to send the browser, Google
# redirects to /google/callback with no Krova session of its own, and that
# redirects the browser again to the frontend with a short-lived handoff code
# - never the real tokens - for /google/exchange to trade in. See
# shared/auth/tokens.py's own module comment for why the handoff exists
# rather than putting access/refresh tokens straight in a URL.

class GoogleStartUrl(BaseModel):
    authorize_url: str


def _google_redirect_uri() -> str:
    if not settings.public_base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PUBLIC_BASE_URL is not configured on this server",
        )
    return f"{settings.public_base_url.rstrip('/')}/api/v1/auth/google/callback"


@router.get("/google/start", response_model=GoogleStartUrl)
async def google_start(
    business_name: str | None = Query(default=None, max_length=255),
    vertical: str | None = Query(default=None),
) -> GoogleStartUrl:
    """
    Called from the login page (no params - existing account only) or the
    signup page (business_name/vertical from the form already on screen,
    carried through Google's own round trip in the state param - see
    create_google_oauth_state).
    """
    if vertical is not None and vertical not in verticals.keys():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown business type",
        )
    state = create_google_oauth_state(business_name, vertical)
    return GoogleStartUrl(
        authorize_url=google_oauth.authorize_url(state, _google_redirect_uri())
    )


@router.get("/google/callback", include_in_schema=False)
async def google_callback(
    db: DbDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    frontend = settings.frontend_base_url.rstrip("/")

    if error or not code or not state:
        return RedirectResponse(f"{frontend}/login?error=google_denied")

    try:
        parsed_state = decode_google_oauth_state(state)
    except TokenError:
        return RedirectResponse(f"{frontend}/login?error=google_expired")

    try:
        tokens = await google_oauth.exchange_code(code, _google_redirect_uri())
        userinfo = await google_oauth.fetch_userinfo(tokens["access_token"])
    except google_oauth.GoogleOAuthError:
        return RedirectResponse(f"{frontend}/login?error=google_failed")

    email = userinfo.get("email")
    if not email or not userinfo.get("email_verified"):
        return RedirectResponse(f"{frontend}/login?error=google_unverified_email")

    full_name = userinfo.get("name")
    business_name = parsed_state["business_name"]
    vertical = parsed_state["vertical"]

    # Every branch below either returns before writing anything or reaches
    # the handoff at the end - nothing here needs a manual commit/rollback,
    # DbDep's own get_db() already commits on a normal return and rolls back
    # on a raised exception (shared/db/session.py).
    try:
        session = await service.login_via_google(email, full_name, db)
    except service.UserNotFound:
        if not business_name:
            return RedirectResponse(f"{frontend}/signup?error=google_no_account")
        try:
            session = await service.register_via_google(
                email=email,
                full_name=full_name,
                business_name=business_name,
                vertical=vertical or "general",
                db=db,
            )
        except service.EmailAlreadyRegistered:
            # Lost a race with a second tab, or the account was created by a
            # password signup between login_via_google's lookup and here -
            # either way, the account exists now, so fall through to it.
            # register()'s own EmailAlreadyRegistered check runs before it
            # writes anything, so there is nothing to undo before retrying.
            session = await service.login_via_google(email, full_name, db)
    except service.AccountDisabled:
        return RedirectResponse(f"{frontend}/login?error=account_disabled")

    handoff = create_google_handoff(session.user.id)
    return RedirectResponse(f"{frontend}/auth/google/complete?code={handoff}")


@router.post("/google/exchange", response_model=SessionResponse)
async def google_exchange(
    db: DbDep,
    code: Annotated[str, Body(embed=True)],
) -> SessionResponse:
    """The frontend's only call after the Google round trip - trades the
    short-lived handoff code from the callback redirect for a real session."""
    try:
        user_id = decode_google_handoff(code)
    except TokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    try:
        session = await service.resume_session(user_id, db)
    except service.InvalidCredentials as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    return _session_response(session)


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
        capabilities=verticals.capabilities_for(business) if business else [],
        queue_labels=labels.queue_labels(business) if business else {},
        autonomy=business.autonomy if business else None,
        role=current_user.role,
        google_review_url=(business.settings or {}).get("google_review_url") if business else None,
        proactive_deadline_calls_enabled=bool(
            (business.settings or {}).get("proactive_deadline_calls_enabled")
        ) if business else False,
    )


class UpdateMeRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    business_name: str | None = Field(default=None, min_length=1, max_length=255)
    vertical: str | None = None
    # A named field rather than a generic settings dict - keeps this
    # endpoint from becoming an arbitrary-JSONB write surface while still
    # reusing Business.settings as the actual storage (same JSONB bag
    # already used for e.g. settings["pipeline_stages"]).
    google_review_url: str | None = Field(default=None, max_length=2000)
    proactive_deadline_calls_enabled: bool | None = None


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
        if body.google_review_url is not None:
            business.settings = {**(business.settings or {}), "google_review_url": body.google_review_url}
        if body.proactive_deadline_calls_enabled is not None:
            business.settings = {
                **(business.settings or {}),
                "proactive_deadline_calls_enabled": body.proactive_deadline_calls_enabled,
            }

    await db.commit()

    return MeResponse(
        user_id=str(current_user.id),
        email=current_user.email,
        full_name=user.full_name if user else None,
        business_id=str(current_user.business_id) if current_user.business_id else None,
        business_name=business.name if business else None,
        vertical=business.vertical if business else None,
        capabilities=verticals.capabilities_for(business) if business else [],
        queue_labels=labels.queue_labels(business) if business else {},
        autonomy=business.autonomy if business else None,
        role=current_user.role,
        google_review_url=(business.settings or {}).get("google_review_url") if business else None,
        proactive_deadline_calls_enabled=bool(
            (business.settings or {}).get("proactive_deadline_calls_enabled")
        ) if business else False,
    )

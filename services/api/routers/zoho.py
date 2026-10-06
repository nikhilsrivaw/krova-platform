"""
Zoho Books connection for a business: connect, check status, sync, disconnect.

The callback is public on purpose: the browser returns from Zoho with no
session, so the business is identified by the signed state we sent out.
A forged or expired state is refused.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import encrypt
from shared.config.settings import settings
from shared.db.models.zoho import ZohoConnection
from shared.integrations import zoho_books
from shared.integrations.zoho_sync import sync_connection
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/zoho", tags=["zoho"])

STATE_TYPE = "zoho_oauth_state"


class ZohoStatusOut(BaseModel):
    configured: bool
    connected: bool
    organization_id: str | None = None
    last_synced_at: datetime | None = None
    last_sync_summary: dict | None = None


class ConnectUrlOut(BaseModel):
    url: str


def _configured() -> bool:
    return bool(settings.zoho_client_id and settings.zoho_client_secret)


def _make_state(business_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"typ": STATE_TYPE, "biz": str(business_id), "iat": now, "exp": now + timedelta(minutes=15)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def _read_state(token: str) -> uuid.UUID:
    try:
        claims = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat"]},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This Zoho connection attempt is invalid or expired") from exc
    if claims.get("typ") != STATE_TYPE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Wrong token type")
    return uuid.UUID(claims["biz"])


async def _connection_for(db, business_id: uuid.UUID) -> ZohoConnection | None:
    return (
        await db.execute(select(ZohoConnection).where(ZohoConnection.business_id == business_id))
    ).scalars().first()


@router.get("/connect", response_model=ConnectUrlOut)
async def zoho_connect(current_user: CurrentUserDep) -> ConnectUrlOut:
    if not _configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Zoho Books is not configured on the server")
    return ConnectUrlOut(url=zoho_books.build_auth_url(_make_state(current_user.business)))


@router.get("/callback")
async def zoho_callback(
    db: DbDep,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    back = f"{settings.app_base_url.rstrip('/')}/settings"
    if error or not code or not state:
        return RedirectResponse(f"{back}?zoho=denied")
    business_id = _read_state(state)

    try:
        refresh_token = await zoho_books.exchange_code(code)
        access = await zoho_books.access_token_from_refresh(refresh_token)
        organizations = await zoho_books.list_organizations(access)
    except zoho_books.ZohoError as exc:
        logger.warning("zoho connect failed business=%s: %s", business_id, exc)
        return RedirectResponse(f"{back}?zoho=error")

    if not organizations:
        return RedirectResponse(f"{back}?zoho=no_organization")
    organization_id = str(organizations[0]["organization_id"])

    connection = await _connection_for(db, business_id)
    if connection is None:
        connection = ZohoConnection(business_id=business_id)
        db.add(connection)
    connection.organization_id = organization_id
    connection.refresh_token = encrypt(refresh_token)
    await db.flush()
    return RedirectResponse(f"{back}?zoho=connected")


@router.get("/status", response_model=ZohoStatusOut)
async def zoho_status(current_user: CurrentUserDep, db: DbDep) -> ZohoStatusOut:
    connection = await _connection_for(db, current_user.business)
    return ZohoStatusOut(
        configured=_configured(),
        connected=connection is not None,
        organization_id=connection.organization_id if connection else None,
        last_synced_at=connection.last_synced_at if connection else None,
        last_sync_summary=connection.last_sync_summary if connection else None,
    )


@router.post("/sync")
async def zoho_sync(current_user: CurrentUserDep, db: DbDep) -> dict:
    connection = await _connection_for(db, current_user.business)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Zoho Books is not connected")
    try:
        return await sync_connection(db, connection)
    except zoho_books.ZohoError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
async def zoho_disconnect(current_user: CurrentUserDep, db: DbDep) -> None:
    connection = await _connection_for(db, current_user.business)
    if connection is not None:
        await db.delete(connection)
        await db.flush()

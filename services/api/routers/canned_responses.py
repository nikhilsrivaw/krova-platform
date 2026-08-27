"""
Saved replies a staff member can reuse verbatim - a refund policy, an
address, an apology for a delay. See shared/db/models/canned_response.py for
why this is deliberately not the same table as MessageDraft.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import CannedResponse
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/canned-responses", tags=["canned-responses"])


class CannedResponseIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4096)


class CannedResponsePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    body: str | None = Field(default=None, min_length=1, max_length=4096)


class CannedResponseOut(BaseModel):
    id: str
    title: str
    body: str
    created_at: str


def _out(c: CannedResponse) -> CannedResponseOut:
    return CannedResponseOut(id=str(c.id), title=c.title, body=c.body, created_at=c.created_at.isoformat())


@router.get("", response_model=list[CannedResponseOut])
async def list_canned_responses(current_user: CurrentUserDep, db: DbDep) -> list[CannedResponseOut]:
    rows = await db.execute(
        select(CannedResponse)
        .where(CannedResponse.business_id == current_user.business)
        .order_by(CannedResponse.title)
    )
    return [_out(c) for c in rows.scalars().all()]


@router.post("", response_model=CannedResponseOut, status_code=status.HTTP_201_CREATED)
async def create_canned_response(
    body: CannedResponseIn, current_user: CurrentUserDep, db: DbDep
) -> CannedResponseOut:
    response = CannedResponse(
        business_id=current_user.business,
        title=body.title,
        body=body.body,
        created_by_user_id=current_user.id,
    )
    db.add(response)
    await db.flush()
    return _out(response)


@router.patch("/{response_id}", response_model=CannedResponseOut)
async def update_canned_response(
    response_id: uuid.UUID, body: CannedResponsePatch, current_user: CurrentUserDep, db: DbDep
) -> CannedResponseOut:
    response = await db.get(CannedResponse, response_id)
    if response is None or response.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Saved reply not found")
    if body.title is not None:
        response.title = body.title
    if body.body is not None:
        response.body = body.body
    await db.flush()
    return _out(response)


@router.delete("/{response_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_canned_response(response_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    response = await db.get(CannedResponse, response_id)
    if response is None or response.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Saved reply not found")
    await db.delete(response)

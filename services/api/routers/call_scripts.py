"""
Managing CallScript rows and reading back CallScriptResponse results -
the lead-qualification / survey calling feature's own CRUD.

Attaching a script to an outbound call happens on call_campaigns.py's own
create endpoint (CallCampaignIn.call_script_id), not here - a script is a
reusable question list, a campaign is one run of calls against it.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import CallScript, CallScriptResponse, Customer

router = APIRouter(prefix="/call-scripts", tags=["call-scripts"])

_VALID_PURPOSES = {"lead_qualification", "survey"}


class CallScriptOut(BaseModel):
    id: str
    name: str
    purpose: str
    questions: list[str]


def _to_out(script: CallScript) -> CallScriptOut:
    return CallScriptOut(
        id=str(script.id), name=script.name, purpose=script.purpose,
        questions=list(script.questions or []),
    )


@router.get("", response_model=list[CallScriptOut])
async def list_scripts(current_user: CurrentUserDep, db: DbDep) -> list[CallScriptOut]:
    result = await db.execute(select(CallScript).where(CallScript.business_id == current_user.business))
    return [_to_out(s) for s in result.scalars().all()]


class CallScriptIn(BaseModel):
    name: str
    purpose: str
    questions: list[str]


def _validate(body: CallScriptIn) -> None:
    if body.purpose not in _VALID_PURPOSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"purpose must be one of {sorted(_VALID_PURPOSES)}",
        )
    questions = [q.strip() for q in body.questions if q.strip()]
    if not questions:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one question is required")
    if not body.name.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name is required")


@router.post("", response_model=CallScriptOut, status_code=status.HTTP_201_CREATED)
async def create_script(body: CallScriptIn, current_user: CurrentUserDep, db: DbDep) -> CallScriptOut:
    _validate(body)
    script = CallScript(
        business_id=current_user.business,
        name=body.name.strip(),
        purpose=body.purpose,
        questions=[q.strip() for q in body.questions if q.strip()],
    )
    db.add(script)
    await db.commit()
    return _to_out(script)


async def _owned_script(script_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> CallScript:
    script = await db.get(CallScript, script_id)
    if script is None or script.business_id != current_user.business:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Script not found")
    return script


@router.patch("/{script_id}", response_model=CallScriptOut)
async def update_script(
    script_id: uuid.UUID, body: CallScriptIn, current_user: CurrentUserDep, db: DbDep
) -> CallScriptOut:
    _validate(body)
    script = await _owned_script(script_id, current_user, db)
    script.name = body.name.strip()
    script.purpose = body.purpose
    script.questions = [q.strip() for q in body.questions if q.strip()]
    await db.commit()
    return _to_out(script)


@router.delete("/{script_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_script(script_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    script = await _owned_script(script_id, current_user, db)
    await db.delete(script)
    await db.commit()


class CallScriptResponseOut(BaseModel):
    id: str
    customer_id: str | None
    customer_name: str | None
    answers: dict[str, str]
    score: int | None
    summary: str | None
    created_at: str


@router.get("/{script_id}/responses", response_model=list[CallScriptResponseOut])
async def list_responses(
    script_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep,
) -> list[CallScriptResponseOut]:
    await _owned_script(script_id, current_user, db)
    rows = (
        await db.execute(
            select(CallScriptResponse, Customer.display_name)
            .join(Customer, Customer.id == CallScriptResponse.customer_id, isouter=True)
            .where(CallScriptResponse.call_script_id == script_id)
            .order_by(CallScriptResponse.created_at.desc())
        )
    ).all()
    return [
        CallScriptResponseOut(
            id=str(r.id),
            customer_id=str(r.customer_id) if r.customer_id else None,
            customer_name=name,
            answers=r.answers or {},
            score=r.score,
            summary=r.summary,
            created_at=r.created_at.isoformat(),
        )
        for r, name in rows
    ]

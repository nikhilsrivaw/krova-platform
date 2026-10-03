"""
Owner commands: preview, confirm, cancel. The app's command bar calls these.
Phase 1 executes set_setting only; the other tools are listed with their phase.
"""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from services.api.dependencies import CurrentUserDep, DbDep
from shared.commands import settings_registry as registry
from shared.commands.service import CommandError, cancel, confirm, create_pending
from shared.commands.tools import CURRENT_PHASE, TOOLS
from shared.db.models import Business, CommandAudit

router = APIRouter(prefix="/commands", tags=["commands"])


class PreviewIn(BaseModel):
    tool: str = Field(min_length=1, max_length=40)
    args: dict[str, Any] = Field(default_factory=dict)


class CommandOut(BaseModel):
    id: str
    tool: str
    status: str
    preview: list[str]
    result: dict[str, Any] | None = None
    error: str | None = None


def _out(row: CommandAudit) -> CommandOut:
    return CommandOut(
        id=str(row.id), tool=row.tool, status=row.status,
        preview=list(row.preview or []), result=row.result, error=row.error,
    )


async def _business(current_user, db) -> Business:
    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business nahi mila")
    return business


@router.get("/tools")
async def list_tools(current_user: CurrentUserDep) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "writes": t.writes,
         "available": t.phase <= CURRENT_PHASE}
        for t in TOOLS.values()
    ]


@router.get("/settings")
async def list_settings(current_user: CurrentUserDep, db: DbDep) -> list[dict[str, Any]]:
    business = await _business(current_user, db)
    return [
        {"key": s.key, "label": s.label, "kind": s.kind,
         "value": registry.current(business.settings, s.key),
         "choices": list(s.choices), "minimum": s.minimum, "maximum": s.maximum}
        for s in registry.REGISTRY.values()
    ]


@router.post("/preview", response_model=CommandOut, status_code=status.HTTP_201_CREATED)
async def preview(body: PreviewIn, current_user: CurrentUserDep, db: DbDep) -> CommandOut:
    business = await _business(current_user, db)
    try:
        row = await create_pending(
            db, business=business, user_id=current_user.id, role=current_user.role,
            tool_name=body.tool, raw_args=body.args,
        )
    except CommandError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return _out(row)


@router.post("/{audit_id}/confirm", response_model=CommandOut)
async def confirm_command(audit_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> CommandOut:
    business = await _business(current_user, db)
    row = await confirm(db, business=business, user_id=current_user.id, role=current_user.role, audit_id=audit_id)
    return _out(row)


@router.post("/{audit_id}/cancel", response_model=CommandOut)
async def cancel_command(audit_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> CommandOut:
    business = await _business(current_user, db)
    row = await cancel(db, business=business, audit_id=audit_id)
    return _out(row)

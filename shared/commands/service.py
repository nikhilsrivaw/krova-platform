"""
Preview, confirm, cancel. Nothing runs without a pending audit row first,
and a confirm only ever moves a row from pending to done or failed once.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.commands import settings_registry as registry
from shared.commands.tools import TOOLS, ToolRefused, check_allowed, parse_input
from shared.db.models import Business, CommandAudit


class CommandError(Exception):
    """Shown to the owner as a plain line."""


def preview_lines(tool_name: str, args: dict[str, Any], business_settings: dict | None) -> list[str]:
    if tool_name == "set_setting":
        key, value = args["key"], args["value"]
        spec = registry.REGISTRY[key]
        before = registry.current(business_settings, key)
        return [f"{spec.label}: {_show(before)} se {_show(value)} kar denge"]
    return [TOOLS[tool_name].description]


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "haan" if value else "nahi"
    if isinstance(value, list):
        return ", ".join(f"{v}h" for v in value)
    return str(value)


async def create_pending(
    db: AsyncSession, *, business: Business, user_id: uuid.UUID, role: str | None,
    tool_name: str, raw_args: dict[str, Any],
) -> CommandAudit:
    tool = TOOLS.get(tool_name)
    if tool is None:
        raise CommandError(f"'{tool_name}' ek valid tool nahi hai")
    try:
        check_allowed(tool, role)
        args = parse_input(tool_name, raw_args).model_dump()
        if tool_name == "set_setting":
            args["value"] = registry.validate(args["key"], args["value"])
    except (ToolRefused, registry.SettingError) as exc:
        raise CommandError(str(exc)) from exc

    row = CommandAudit(
        business_id=business.id, user_id=user_id, tool=tool_name, args=args,
        preview=preview_lines(tool_name, args, business.settings), status="pending",
    )
    db.add(row)
    await db.commit()
    return row


async def _load(db: AsyncSession, business: Business, audit_id: uuid.UUID) -> CommandAudit:
    row = (await db.execute(
        select(CommandAudit).where(CommandAudit.id == audit_id, CommandAudit.business_id == business.id)
    )).scalar_one_or_none()
    if row is None:
        raise CommandError("Ye command nahi mili")
    return row


async def confirm(
    db: AsyncSession, *, business: Business, role: str | None, audit_id: uuid.UUID,
) -> CommandAudit:
    row = await _load(db, business, audit_id)
    if row.status != "pending":
        return row  # already decided: a second confirm changes nothing

    try:
        check_allowed(TOOLS[row.tool], role)
        row.result = await _execute(business, row)
        row.status, row.error = "done", None
    except (ToolRefused, registry.SettingError, CommandError) as exc:
        row.status, row.error = "failed", str(exc)
    row.decided_at = datetime.now(timezone.utc)
    await db.commit()
    return row


async def cancel(db: AsyncSession, *, business: Business, audit_id: uuid.UUID) -> CommandAudit:
    row = await _load(db, business, audit_id)
    if row.status == "pending":
        row.status = "cancelled"
        row.decided_at = datetime.now(timezone.utc)
        await db.commit()
    return row


async def _execute(business: Business, row: CommandAudit) -> dict[str, Any]:
    if row.tool == "set_setting":
        key = row.args["key"]
        value = registry.validate(key, row.args["value"])
        settings = dict(business.settings or {})
        controls = dict(settings.get(registry.CONTROLS_KEY) or {})
        controls[key] = value
        settings[registry.CONTROLS_KEY] = controls
        business.settings = settings
        return {"key": key, "value": value}
    raise ToolRefused(f"'{row.tool}' abhi nahi chalega. Ye agle phase mein aayega.")

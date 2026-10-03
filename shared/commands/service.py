"""
Preview, confirm, cancel. Every command is an audit row. Write tools stop at
pending until the owner confirms, then run once. Read tools run at once, since
nothing changes, and their row is still written.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.commands import executors
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
    if tool_name == "update":
        return [f"Customer ka naam '{args['value']}' kar denge"]
    if tool_name == "create":
        kind = "note" if args["entity"] == "note" else "tag"
        return [f"Is customer par {kind} jod denge: '{(args.get('text') or '')[:80]}'"]
    if tool_name == "block_slot":
        reason = f" ({args['reason']})" if args.get("reason") else ""
        return [f"{args['date']} ko {args['start']} se {args['end']} tak slot band kar denge{reason}"]
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
    await db.flush()

    if not tool.writes:
        await _run(db, business, user_id, row)
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
    db: AsyncSession, *, business: Business, user_id: uuid.UUID, role: str | None, audit_id: uuid.UUID,
) -> CommandAudit:
    row = await _load(db, business, audit_id)
    if row.status != "pending":
        return row  # already decided: a second confirm changes nothing
    try:
        check_allowed(TOOLS[row.tool], role)
    except ToolRefused as exc:
        row.status, row.error = "failed", str(exc)
        row.decided_at = datetime.now(timezone.utc)
        await db.commit()
        return row
    await _run(db, business, user_id, row)
    await db.commit()
    return row


async def cancel(db: AsyncSession, *, business: Business, audit_id: uuid.UUID) -> CommandAudit:
    row = await _load(db, business, audit_id)
    if row.status == "pending":
        row.status = "cancelled"
        row.decided_at = datetime.now(timezone.utc)
        await db.commit()
    return row


async def _run(db: AsyncSession, business: Business, user_id: uuid.UUID, row: CommandAudit) -> None:
    """Run the tool, record the outcome on the row. A refusal is a 'failed' outcome, not an error."""
    try:
        row.result = await _execute(db, business, user_id, row)
        row.status, row.error = "done", None
    except (ToolRefused, registry.SettingError, CommandError) as exc:
        row.status, row.error = "failed", str(exc)
    row.decided_at = datetime.now(timezone.utc)


async def _execute(db: AsyncSession, business: Business, user_id: uuid.UUID, row: CommandAudit) -> dict[str, Any]:
    args = row.args
    if row.tool == "set_setting":
        key = args["key"]
        value = registry.validate(key, args["value"])
        settings = dict(business.settings or {})
        controls = dict(settings.get(registry.CONTROLS_KEY) or {})
        controls[key] = value
        settings[registry.CONTROLS_KEY] = controls
        business.settings = settings
        return {"key": key, "value": value}
    if row.tool == "find":
        return await executors.find(db, business, args)
    if row.tool == "report":
        return await executors.report(db, business, args)
    if row.tool == "create":
        return await executors.create(db, business, user_id, args)
    if row.tool == "update":
        return await executors.update(db, business, args)
    if row.tool == "block_slot":
        return await executors.block_slot(db, business, args)
    raise ToolRefused(f"'{row.tool}' abhi nahi chalega. Ye agle phase mein aayega.")

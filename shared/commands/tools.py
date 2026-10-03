"""
The eight generic tools an owner's command can resolve to. Each has a typed
input, a read/write flag, and the roles allowed to run it. Phase 1 executes
only set_setting; the others are registered so the contract is fixed and
refused with a clear message until their phase lands.
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

WRITE_ROLES = frozenset({"owner", "admin"})


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FindInput(ToolInput):
    entity: Literal["customers", "bookings", "orders", "messages"]
    limit: int = 20


class UpdateInput(ToolInput):
    entity: Literal["customer"]
    record_id: str
    field: str
    value: str


class CreateInput(ToolInput):
    entity: Literal["note", "tag", "booking", "reminder"]
    customer_id: str
    text: str | None = None


class MessageInput(ToolInput):
    audience: str
    body: str
    template: str | None = None


class BlockSlotInput(ToolInput):
    doctor_id: str | None = None
    date: str
    start: str
    end: str
    reason: str | None = None


class SetSettingInput(ToolInput):
    key: str
    value: Any


class RuleInput(ToolInput):
    trigger: str
    condition: dict[str, Any] = {}
    action: str
    action_args: dict[str, Any] = {}


class ReportInput(ToolInput):
    name: Literal["no_show_summary", "todays_bookings", "waitlist", "ledger_summary"]
    period_days: int = 30


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[ToolInput]
    writes: bool
    phase: int  # the build phase in which the tool becomes executable


TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in (
        ToolSpec("find", "Customers, bookings or orders with filters", FindInput, False, 2),
        ToolSpec("update", "Change a customer's name", UpdateInput, True, 2),
        ToolSpec("create", "Add a note or tag to a customer", CreateInput, True, 2),
        ToolSpec("message", "Send a message to an audience (always confirmed)", MessageInput, True, 3),
        ToolSpec("block_slot", "Block a time on a staff member's calendar", BlockSlotInput, True, 2),
        ToolSpec("set_setting", "Change one business setting from the registry", SetSettingInput, True, 1),
        ToolSpec("rule", "Create an automation from plain words", RuleInput, True, 4),
        ToolSpec("report", "Run one approved report", ReportInput, False, 2),
    )
}


class ToolRefused(Exception):
    """The tool cannot run for this caller or in this build phase. The message is shown to the owner."""


def check_allowed(tool: ToolSpec, role: str | None) -> None:
    if tool.writes and role not in WRITE_ROLES:
        raise ToolRefused("Ye kaam sirf owner ya admin kar sakte hain.")
    if tool.phase > CURRENT_PHASE:
        raise ToolRefused(f"'{tool.name}' abhi nahi chalega. Ye agle phase mein aayega.")


CURRENT_PHASE = 2


def parse_input(name: str, raw: dict[str, Any]) -> ToolInput:
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolRefused(f"'{name}' ek valid tool nahi hai")
    try:
        return tool.input_model.model_validate(raw)
    except Exception as exc:  # pydantic error, shown as one plain line
        raise ToolRefused(f"Input galat hai: {str(exc).splitlines()[0]}") from exc

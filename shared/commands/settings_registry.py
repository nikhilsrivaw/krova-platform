"""
The business settings an owner can change by command. Stored under
Business.settings["controls"], so they never collide with settings other
features already keep at the top level. Adding a setting means one entry here.
"""

from dataclasses import dataclass
from typing import Any

CONTROLS_KEY = "controls"


class SettingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    label: str
    kind: str  # "int" | "bool" | "choice" | "int_list"
    default: Any
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()


REGISTRY: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec("cancellation_window_hours", "Cancellation window (hours before)", "int", 2, 0, 72),
        SettingSpec("reminder_before_hours", "Reminders sent (hours before)", "int_list", [24, 2], 1, 168),
        SettingSpec("no_show_grace_minutes", "No-show grace period (minutes)", "int", 15, 0, 120),
        SettingSpec("deposit_required", "Deposit required at booking", "bool", False),
        SettingSpec("deposit_percent", "Deposit percent", "int", 50, 0, 100),
        SettingSpec(
            "service_model", "How the business works", "choice", "timed",
            choices=("timed", "job", "session", "walk_in", "table"),
        ),
        SettingSpec(
            "feedback_route", "What happens after a visit", "choice", "review_link",
            choices=("review_link", "manager_alert"),
        ),
    )
}


def validate(key: str, value: Any) -> Any:
    """Return the normalised value, or raise SettingError with a plain message."""
    spec = REGISTRY.get(key)
    if spec is None:
        raise SettingError(f"'{key}' ek valid setting nahi hai")

    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise SettingError(f"{spec.label}: haan ya nahi (true/false) dijiye")
        return value

    if spec.kind == "choice":
        if value not in spec.choices:
            raise SettingError(f"{spec.label}: inmein se ek chuniye: {', '.join(spec.choices)}")
        return value

    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingError(f"{spec.label}: ek number dijiye")
        _check_range(spec, value)
        return value

    if spec.kind == "int_list":
        if not isinstance(value, list) or not value:
            raise SettingError(f"{spec.label}: numbers ki list dijiye")
        if len(value) > 5:
            raise SettingError(f"{spec.label}: zyada se zyada 5 reminder")
        cleaned = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                raise SettingError(f"{spec.label}: sirf numbers chahiye")
            _check_range(spec, item)
            cleaned.append(item)
        return sorted(set(cleaned), reverse=True)

    raise SettingError(f"{spec.label}: ye setting type abhi support nahi hota")


def _check_range(spec: SettingSpec, value: int) -> None:
    if spec.minimum is not None and value < spec.minimum:
        raise SettingError(f"{spec.label}: kam se kam {spec.minimum} hona chahiye")
    if spec.maximum is not None and value > spec.maximum:
        raise SettingError(f"{spec.label}: zyada se zyada {spec.maximum} ho sakta hai")


def current(business_settings: dict | None, key: str) -> Any:
    """The business's value for this setting, or the default if never set."""
    spec = REGISTRY[key]
    controls = (business_settings or {}).get(CONTROLS_KEY) or {}
    return controls.get(key, spec.default)

import pytest

from shared.commands import settings_registry as registry
from shared.commands.service import preview_lines
from shared.commands.tools import CURRENT_PHASE, TOOLS, ToolRefused, check_allowed, parse_input


def test_registry_has_the_seven_settings():
    assert set(registry.REGISTRY) == {
        "cancellation_window_hours", "reminder_before_hours", "no_show_grace_minutes",
        "deposit_required", "deposit_percent", "service_model", "feedback_route",
    }


def test_int_setting_accepts_range_and_rejects_outside():
    assert registry.validate("cancellation_window_hours", 4) == 4
    with pytest.raises(registry.SettingError):
        registry.validate("cancellation_window_hours", 500)
    with pytest.raises(registry.SettingError):
        registry.validate("cancellation_window_hours", True)  # a bool is not a number here


def test_bool_setting_rejects_strings():
    assert registry.validate("deposit_required", True) is True
    with pytest.raises(registry.SettingError):
        registry.validate("deposit_required", "haan")


def test_choice_setting_rejects_unknown_choice():
    assert registry.validate("service_model", "table") == "table"
    with pytest.raises(registry.SettingError):
        registry.validate("service_model", "bar")


def test_int_list_is_sorted_deduplicated_and_capped():
    assert registry.validate("reminder_before_hours", [2, 24, 24]) == [24, 2]
    with pytest.raises(registry.SettingError):
        registry.validate("reminder_before_hours", [1, 2, 3, 4, 5, 6])


def test_unknown_setting_key_is_rejected():
    with pytest.raises(registry.SettingError):
        registry.validate("secret_flag", 1)


def test_current_returns_default_when_not_set_and_stored_value_when_set():
    assert registry.current(None, "cancellation_window_hours") == 2
    assert registry.current({"controls": {"cancellation_window_hours": 6}}, "cancellation_window_hours") == 6
    assert registry.current({"scheduling": {"x": 1}}, "cancellation_window_hours") == 2


def test_write_tools_refuse_non_owner_roles():
    for role in ("agent", None):
        with pytest.raises(ToolRefused):
            check_allowed(TOOLS["set_setting"], role)
    check_allowed(TOOLS["set_setting"], "owner")
    check_allowed(TOOLS["set_setting"], "admin")


def test_read_tools_are_not_role_gated():
    # Read tools are phase 2 here, so the refusal must be about the phase, never the role.
    for role in ("agent", None):
        with pytest.raises(ToolRefused) as exc:
            check_allowed(TOOLS["report"], role)
        assert "agle phase" in str(exc.value)


def test_phase_gate_refuses_tools_not_yet_built():
    assert CURRENT_PHASE == 1
    with pytest.raises(ToolRefused):
        check_allowed(TOOLS["rule"], "owner")


def test_parse_input_rejects_extra_fields_and_unknown_tool():
    with pytest.raises(ToolRefused):
        parse_input("set_setting", {"key": "deposit_required", "value": True, "extra": 1})
    with pytest.raises(ToolRefused):
        parse_input("delete_everything", {})


def test_preview_for_set_setting_shows_before_and_after():
    lines = preview_lines("set_setting", {"key": "cancellation_window_hours", "value": 6}, None)
    assert lines == ["Cancellation window (hours before): 2 se 6 kar denge"]


def test_preview_for_bool_uses_plain_words():
    lines = preview_lines("set_setting", {"key": "deposit_required", "value": True}, {"controls": {"deposit_required": False}})
    assert lines == ["Deposit required at booking: nahi se haan kar denge"]

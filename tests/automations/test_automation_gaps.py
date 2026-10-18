"""
The engine gaps from docs/new/automation-needs-research.md: outcome triggers, "stop if they replied / booked / paid",
quiet hours and the daily cap, a step's own template, and dates that come round every year.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from services.api.routers import post_call_rules
from shared.care import date_triggers, post_call_actions as pca
from shared.db.models import WebhookEventType

OUTCOMES = [
    "appointment.visited", "appointment.no_show", "queue_token.done", "queue_token.skipped",
    "order.status_changed", "payment.received",
]


@pytest.mark.parametrize("trigger", OUTCOMES)
def test_outcome_triggers_are_real_rule_triggers_with_fields(trigger):
    assert WebhookEventType(trigger)
    assert trigger in post_call_rules._VALID_TRIGGERS
    assert pca.CONDITION_FIELDS[trigger]


def test_first_visit_and_order_status_are_conditions():
    assert "visit_number" in pca.CONDITION_FIELDS["appointment.visited"]
    assert {"status", "previous_status"} <= set(pca.CONDITION_FIELDS["order.status_changed"])


# ── stop if ──────────────────────────────────────────────────────────────────

def test_stop_kinds_read_a_comma_list():
    assert pca.stop_kinds({"stop_if": "replied, booked"}) == ["replied", "booked"]
    assert pca.stop_kinds({}) == [] and pca.stop_kinds(None) == []


def _step(**config):
    return post_call_rules.StepIn(action_type="whatsapp_followup", action_config={"message": "hi", **config})


def test_an_unknown_stop_kind_is_refused():
    post_call_rules._validate_step("lead.received", _step(stop_if="replied,paid"), 0)
    with pytest.raises(Exception):
        post_call_rules._validate_step("lead.received", _step(stop_if="opened"), 0)


def test_a_step_with_its_own_template_needs_no_message():
    step = post_call_rules.StepIn(action_type="whatsapp_followup", action_config={"template_name": "diwali_offer"})
    post_call_rules._validate_step("customer.inactive", step, 0)
    with pytest.raises(Exception):
        post_call_rules._validate_step(
            "customer.inactive", post_call_rules.StepIn(action_type="whatsapp_followup", action_config={}), 0,
        )


def test_no_stop_check_without_a_start_time():
    import asyncio

    step = {"action_config": {"stop_if": "replied"}}
    assert asyncio.run(pca._stopped(None, step, None, {})) is None


# ── quiet hours ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hour, expected", [
    (22, datetime(2026, 10, 20, 9)), (2, datetime(2026, 10, 19, 9)), (9, None), (14, None), (20, None),
])
def test_quiet_hours_across_midnight(hour, expected):
    assert pca.quiet_until(pca.DEFAULT_LIMITS, datetime(2026, 10, 19, hour, 30)) == expected


def test_quiet_hours_off_or_empty():
    assert pca.quiet_until({**pca.DEFAULT_LIMITS, "quiet_hours": False}, datetime(2026, 10, 19, 23)) is None
    assert pca.quiet_until({**pca.DEFAULT_LIMITS, "quiet_start": 9, "quiet_end": 9}, datetime(2026, 10, 19, 23)) is None


def test_limits_default_and_saved():
    assert pca.sending_limits(SimpleNamespace(settings={})) == pca.DEFAULT_LIMITS
    saved = SimpleNamespace(settings={"automation_limits": {"daily_limit": 0, "junk": 1}})
    assert pca.sending_limits(saved)["daily_limit"] == 0 and "junk" not in pca.sending_limits(saved)


def test_team_steps_are_never_held():
    import asyncio

    assert asyncio.run(pca._send_gate(None, None, None, "notify_team", datetime.now())) is None


# ── yearly dates ─────────────────────────────────────────────────────────────

def test_a_one_off_date_is_itself():
    assert date_triggers.occurrence(date(2020, 5, 1), date(2026, 10, 19), repeats_yearly=False) == date(2020, 5, 1)


def test_a_yearly_date_moves_to_the_next_one():
    today = date(2026, 10, 19)
    assert date_triggers.occurrence(date(1990, 11, 2), today, repeats_yearly=True) == date(2026, 11, 2)
    # two months past this year's: next year's
    assert date_triggers.occurrence(date(1990, 8, 1), today, repeats_yearly=True) == date(2027, 8, 1)
    # just past (within the after-window): still this year's, so "the day after" fires
    assert date_triggers.occurrence(date(1990, 10, 10), today, repeats_yearly=True) == date(2026, 10, 10)


def test_29_february_falls_on_28th():
    assert date_triggers.occurrence(date(2000, 2, 29), date(2026, 1, 10), repeats_yearly=True) == date(2026, 2, 28)

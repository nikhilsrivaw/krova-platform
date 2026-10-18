"""
Writing a whole WhatsApp template from a description: what code enforces on the model's draft, the retry, and the
endpoint's allowance and metering.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.api.routers import templates as templates_router
from shared.ai import template_draft as td

GOOD = {
    "name": "Appointment Reminder!",
    "category": "UTILITY",
    "header": "Appointment reminder",
    "body": "Hi {{name}}, your appointment is on {{date}}. Reply YES to confirm.",
    "footer": "",
    "examples": {"name": "Priya", "date": "21 Oct, 4 pm"},
    "buttons": [
        {"type": "QUICK_REPLY", "text": "Confirm"},
        {"type": "URL", "text": "Map", "url": "not a link"},
        {"type": "PHONE_NUMBER", "text": "Call us", "phone_number": "+91 98200 12345"},
    ],
    "note": "Utility, kyunki ye booking ke baare mein hai.",
}


def test_a_good_draft_is_numbered_and_cleaned():
    d = td.clean(GOOD, taken=set())
    assert d.body == "Hi {{1}}, your appointment is on {{2}}. Reply YES to confirm."
    assert d.examples == {"1": "Priya", "2": "21 Oct, 4 pm"}
    assert d.name == "appointment_reminder" and d.category == "UTILITY" and d.language == "en"
    assert [b["type"] for b in d.buttons] == ["QUICK_REPLY", "PHONE_NUMBER"]  # the URL had no real link
    assert d.buttons[1]["phone_number"] == "+919820012345"


def test_a_taken_name_gets_a_suffix():
    assert td.clean(GOOD, taken={"appointment_reminder"}).name == "appointment_reminder_2"


def test_devanagari_is_hindi():
    d = td.clean({**GOOD, "body": "नमस्ते {{1}}, आपकी अपॉइंटमेंट {{2}} को है। धन्यवाद", "examples": {"1": "प्रिया", "2": "21 अक्टूबर"}}, taken=set())
    assert d.language == "hi"


@pytest.mark.parametrize("body, reason", [
    ("{{1}}, your order is ready.", "starts or ends"),
    ("Your order is ready, {{1}}.", "starts or ends"),
    ("Hi {{1}} {{2}}, your order is ready.", "next to each other"),
    ("", "empty"),
])
def test_what_meta_refuses_is_refused(body, reason):
    with pytest.raises(td.DraftError) as err:
        td.clean({**GOOD, "body": body, "examples": {"1": "a", "2": "b"}}, taken=set())
    assert reason in str(err.value)


def test_every_variable_needs_an_example_and_none_in_header():
    with pytest.raises(td.DraftError):
        td.clean({**GOOD, "examples": {"name": "Priya"}}, taken=set())
    with pytest.raises(td.DraftError):
        td.clean({**GOOD, "header": "Hi {{1}}"}, taken=set())


def test_anything_not_utility_is_marketing():
    assert td.clean({**GOOD, "category": "PROMO"}, taken=set()).category == "MARKETING"


def test_json_is_found_inside_text():
    assert td._json('Here you go:\n{"a": 1}\nThanks') == {"a": 1}
    assert td._json("no json") is None


def test_a_bad_first_try_gets_one_more_with_the_reason(monkeypatch):
    calls = []
    answers = [
        json.dumps({**GOOD, "body": "{{1}}, your appointment is tomorrow."}),
        json.dumps(GOOD),
    ]

    async def fake_complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=answers.pop(0), cost_paise=20)

    monkeypatch.setattr(td.client, "complete", fake_complete)
    draft, cost = asyncio.run(td.write(brief="reminder", business_line="Clinic", taken=set()))
    assert draft.name == "appointment_reminder" and cost == 40
    assert "tool" not in calls[0] and "starts or ends" in calls[1]["messages"][-1]["content"]


# ── the endpoint ─────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return SimpleNamespace(all=lambda: ["old_one"])


class FakeDb:
    def __init__(self, used):
        self.used, self.added, self.commits = used, [], 0

    async def execute(self, _q):
        return _Result(self.used)

    async def get(self, model, key):
        return SimpleNamespace(name="Sharma Clinic", vertical="clinic", summary="A family clinic")

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1


def _call(monkeypatch, result, used=0):
    async def fake_write(**kwargs):
        if isinstance(result, Exception):
            raise result
        return result, 25

    monkeypatch.setattr(templates_router.template_draft, "write", fake_write)
    db = FakeDb(used)
    user = SimpleNamespace(business=uuid.uuid4())
    out = asyncio.run(templates_router.draft_template(templates_router.TemplateDraftIn(brief="ek din pehle reminder"), user, db))
    return out, db


def test_the_draft_comes_back_and_is_metered(monkeypatch):
    out, db = _call(monkeypatch, td.clean(GOOD, taken=set()))
    assert out.body.startswith("Hi {{1}}") and out.used == 1 and out.limit == templates_router.TEMPLATE_DRAFTS_MONTHLY
    assert db.added[0].krova_cost_paise == 25 and db.commits == 1


def test_the_monthly_allowance(monkeypatch):
    with pytest.raises(HTTPException) as err:
        _call(monkeypatch, None, used=templates_router.TEMPLATE_DRAFTS_MONTHLY)
    assert err.value.status_code == 429


def test_two_failed_tries_ask_for_more_detail(monkeypatch):
    with pytest.raises(HTTPException) as err:
        _call(monkeypatch, None)
    assert err.value.status_code == 422


def test_model_down_is_a_clear_message(monkeypatch):
    with pytest.raises(HTTPException) as err:
        _call(monkeypatch, RuntimeError("down"))
    assert err.value.status_code == 502

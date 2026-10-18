"""
The reception QR: its exact message gets a token without the AI, in the shift running now, once per patient,
with one reply and the payment link; anything else goes to the AI as usual.
"""

import asyncio
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from shared.db.models import Shift
from shared.scheduling import qr_token

BIZ = uuid.uuid4()


def _business(phrase=None, capability=True):
    settings_ = {"queue": {"qr_phrase": phrase}} if phrase else {}
    return SimpleNamespace(id=BIZ, name="Dr Sharma Clinic", settings=settings_, timezone="Asia/Kolkata",
                           vertical="health_clinic", _cap=capability)


@pytest.mark.parametrize("text", [
    "Mujhe aaj ka token chahiye", "mujhe aaj ka token chahiye!", "  Mujhe  aaj ka TOKEN chahiye. ",
])
def test_the_qr_message_matches_give_or_take_case_and_punctuation(text):
    assert qr_token.matches(text, qr_token.DEFAULT_PHRASE)


@pytest.mark.parametrize("text", [
    "token chahiye kal ka", "Mujhe aaj ka token chahiye aur fees kitni hai?", "hi", "", "token",
])
def test_other_messages_go_to_the_ai(text):
    assert not qr_token.matches(text, qr_token.DEFAULT_PHRASE)


def test_the_clinic_can_set_its_own_phrase():
    assert qr_token.phrase_for(_business("TOKEN")) == "TOKEN"
    assert qr_token.phrase_for(_business()) == qr_token.DEFAULT_PHRASE


@pytest.mark.parametrize("open_shifts,hour,expected", [
    ([Shift.morning], 9, Shift.morning),
    ([Shift.evening], 9, Shift.evening),            # only one open: that one, whatever the clock says
    ([Shift.morning, Shift.evening], 11, Shift.morning),
    ([Shift.morning, Shift.evening], 18, Shift.evening),
    ([Shift.emergency], 11, None),                 # emergency is never handed out from a QR
    ([Shift.emergency, Shift.morning], 18, Shift.morning),
    ([], 11, None),
])
def test_the_shift_running_now_is_chosen(open_shifts, hour, expected):
    assert qr_token.pick_shift(open_shifts, hour) == expected


def test_the_reply_is_in_hinglish_and_english():
    text = qr_token.token_text(business_name="Dr Sharma Clinic", ticket="token", shift_label="Morning", number=14, ahead=5)
    assert "token #14" in text and "aapse pehle 5 log" in text and "5 ahead of you" in text
    assert "aapse pehle koi nahi" in qr_token.token_text(business_name="X", ticket="token", shift_label="M", number=1, ahead=0)


# ── the whole path, with the database parts replaced ─────────────────────────

def _run(monkeypatch, *, text=qr_token.DEFAULT_PHRASE, channel="whatsapp", open_shifts=(Shift.morning,),
         existing=None, capability=True):
    calls = {"replies": [], "issued": [], "links": []}
    entry = SimpleNamespace(id=uuid.uuid4(), business_id=BIZ, queue_number=14, queue_date=date(2026, 10, 18), shift=Shift.morning)

    async def open_shifts_today(db, *, business_id):
        return [SimpleNamespace(shift=s) for s in open_shifts]

    async def issue_token(db, **kwargs):
        calls["issued"].append(kwargs)
        return entry

    async def reply(db, business, customer, text):
        calls["replies"].append(text)
        return True

    async def waiting(db, business_id, customer_id, shift):
        return existing

    async def ahead(db, e):
        return 3

    async def auto_link(db, **kwargs):
        calls["links"].append(kwargs)

    from shared.payments import links as payment_links

    monkeypatch.setattr(qr_token.verticals, "has_capability", lambda b, c: capability)
    monkeypatch.setattr(qr_token.queue_booking, "open_shifts_today", open_shifts_today)
    monkeypatch.setattr(qr_token.queue_booking, "issue_token", issue_token)
    monkeypatch.setattr(qr_token, "_reply", reply)
    monkeypatch.setattr(qr_token, "_waiting_token_today", waiting)
    monkeypatch.setattr(qr_token, "_ahead", ahead)
    monkeypatch.setattr(qr_token, "pick_shift", lambda shifts, hour: shifts[0] if shifts and shifts[0] != Shift.emergency else None)
    monkeypatch.setattr(qr_token.vertical_labels, "queue_labels", lambda b: {"ticket": "token", "shifts": {"morning": "Morning"}})
    monkeypatch.setattr(payment_links, "auto_link_after_booking", auto_link)

    message = SimpleNamespace(id=uuid.uuid4(), content=text, channel=channel)
    handled = asyncio.run(qr_token.handle(message, _business(), SimpleNamespace(id=uuid.uuid4()), None))
    return handled, calls, entry


def test_the_qr_message_gets_a_token_one_reply_and_the_payment_link(monkeypatch):
    handled, calls, entry = _run(monkeypatch)
    assert handled
    issued = calls["issued"][0]
    assert issued["shift"] == Shift.morning and issued["notify_customer"] is False
    assert len(issued["source_message_ids"]) == 1
    assert len(calls["replies"]) == 1 and "#14" in calls["replies"][0] and "3 log" in calls["replies"][0]
    assert calls["links"] == [{"business": calls["links"][0]["business"], "kind": "tokens", "ref_id": entry.id}]


def test_scanning_twice_keeps_the_same_token(monkeypatch):
    existing = SimpleNamespace(id=uuid.uuid4(), business_id=BIZ, queue_number=9, queue_date=date(2026, 10, 18), shift=Shift.morning)
    handled, calls, _ = _run(monkeypatch, existing=existing)
    assert handled and calls["issued"] == [] and "#9" in calls["replies"][0]


def test_no_open_shift_says_so_and_issues_nothing(monkeypatch):
    handled, calls, _ = _run(monkeypatch, open_shifts=())
    assert handled and calls["issued"] == [] and "koi shift khuli nahi" in calls["replies"][0]


@pytest.mark.parametrize("kwargs", [
    {"text": "kitne baje khulte ho?"}, {"channel": "instagram"}, {"capability": False},
])
def test_everything_else_is_left_to_the_ai(monkeypatch, kwargs):
    handled, calls, _ = _run(monkeypatch, **kwargs)
    assert handled is False and calls["replies"] == [] and calls["issued"] == []


def test_the_reply_worker_checks_the_qr_before_the_ai():
    import inspect
    from services.workers import respond
    src = inspect.getsource(respond.draft_for_message)
    assert src.index("qr_token.handle") < src.index("agent_context.build")


def test_counter_mode_tells_the_patient_the_fee_to_pay_at_the_counter(monkeypatch):
    from shared.payments import links as payment_links
    monkeypatch.setattr(payment_links, "auto_settings", lambda b: {"tokens": False, "appointments": False, "default_fee_paise": 50000, "advance_paise": None})
    handled, calls, _ = _run(monkeypatch)
    assert handled and "Rs 500 counter par" in calls["replies"][0]


def test_online_modes_do_not_add_the_counter_line(monkeypatch):
    from shared.payments import links as payment_links
    monkeypatch.setattr(payment_links, "auto_settings", lambda b: {"tokens": True, "appointments": False, "default_fee_paise": 50000, "advance_paise": None})
    handled, calls, _ = _run(monkeypatch)
    assert handled and "counter par" not in calls["replies"][0]

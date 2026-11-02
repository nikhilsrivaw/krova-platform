"""What the AI may change for a customer, the confirm-then-do rule, and the AI not steering back to the past."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from sqlalchemy.dialects import postgresql  # noqa: E402

from shared.ai import actions  # noqa: E402
from shared.ai import agent as agent_module  # noqa: E402
from shared.ai import context as ctx  # noqa: E402

BIZ = uuid.uuid4()
NOW = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)


def _business(**settings):
    return SimpleNamespace(id=BIZ, settings={"ai_actions": settings} if settings else {}, timezone="Asia/Kolkata",
                           vertical="local_service", capabilities=None)


# ── settings ────────────────────────────────────────────────────────────────

def test_by_default_the_ai_keeps_what_it_could_do_and_orders_stay_off():
    b = _business()
    for key in ("book", "cancel_booking", "reschedule_booking", "book_token", "cancel_token"):
        assert all(actions.allowed(b, key, c) for c in actions.CHANNELS)
    assert not actions.allowed(b, "take_order", "whatsapp") and not actions.allowed(b, "cancel_order", "voice")
    assert actions.read(b)["cutoff_hours"] is None


def test_the_owner_chooses_per_action_and_channel():
    b = _business()
    out = actions.write(b, actions={"cancel_booking": ["whatsapp", "fax"], "take_order": ["voice"]}, cutoff_hours=4)
    assert out["actions"]["cancel_booking"] == ["whatsapp"] and out["actions"]["take_order"] == ["voice"]
    assert actions.allowed(b, "cancel_booking", "whatsapp") and not actions.allowed(b, "cancel_booking", "voice")
    assert out["cutoff_hours"] == 4
    with pytest.raises(ValueError):
        actions.write(b, actions={}, cutoff_hours=0)


def test_website_chat_can_only_book_never_change():
    b = _business()
    assert actions.allowed(b, "book", "web") and actions.allowed(b, "book_token", "web")
    assert not any(actions.allowed(b, k, "web") for k in actions.CHANGES)


# ── what the customer said ──────────────────────────────────────────────────

@pytest.mark.parametrize("text, yes", [
    ("haan", True), ("Haan ji kar do", True), ("yes please", True), ("ok", True), ("हाँ", True), ("theek hai", True), ("हाँ जी", True), ("नहीं", False),
    ("haan cancel kar do", True),
    ("nahi", False), ("no wait", False), ("ruko abhi mat karo", False), ("kal ka kya time hai?", False), ("", False),
])
def test_only_a_clear_yes_counts(text, yes):
    assert actions.affirmed(text) is yes


def test_topics_follow_what_the_customer_brings_up_now():
    assert actions.topics(["mera appointment kab hai"]) == {"booking"}
    assert "money" in actions.topics(["fees kitni baaki hai"])
    assert actions.topics(["hello, aapke yahan parking hai?"]) == set()


def test_the_confirmation_is_the_same_words_every_time_and_expires():
    plan = actions.Plan(ok=True, kind="cancel_booking", data={"appointment_id": "x"},
                        summary="cancel your booking on Sun 2 Nov, 5:00 PM")
    assert actions.confirmation(plan, "hinglish") == actions.confirmation(plan, "hinglish")
    assert "kar doon" in actions.confirmation(plan, "hinglish") and "?" in actions.confirmation(plan, "english")
    saved = actions.pending(plan, NOW)
    assert actions.still_open(saved, NOW + timedelta(minutes=29))
    assert not actions.still_open(saved, NOW + timedelta(minutes=31))
    assert not actions.still_open({"kind": "book", "asked_at": NOW.isoformat()}, NOW)


# ── checking a change against the records ───────────────────────────────────

class _Db:
    def __init__(self, first=None, objects=None):
        self.first, self.objects = first, objects or {}

    async def execute(self, stmt, *a, **k):
        stmt.compile(dialect=postgresql.dialect())
        first = self.first
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: first, all=lambda: [first] if first else []))

    async def get(self, model, key):
        return self.objects.get(key)


def _with_scheduling(monkeypatch):
    from shared import verticals

    monkeypatch.setattr(verticals, "has_capability", lambda business, cap: True)


def test_cancelling_a_booking_that_is_not_theirs_is_refused(monkeypatch):
    _with_scheduling(monkeypatch)
    plan = asyncio.run(actions.plan(_Db(first=None), business=_business(), customer=SimpleNamespace(id=uuid.uuid4()),
                                    proposal=actions.Proposal("cancel_booking", booking_at="2026-11-02T17:00:00+05:30"),
                                    now=NOW))
    assert not plan.ok and plan.say and "no upcoming booking" in plan.problem


def test_a_booking_inside_the_cut_off_goes_to_a_person(monkeypatch):
    _with_scheduling(monkeypatch)
    soon = NOW + timedelta(hours=2)
    appointment = SimpleNamespace(id=uuid.uuid4(), starts_at=soon, doctor_id=uuid.uuid4())
    b = _business(actions={}, cutoff_hours=4)
    plan = asyncio.run(actions.plan(_Db(first=appointment), business=b, customer=SimpleNamespace(id=uuid.uuid4()),
                                    proposal=actions.Proposal("cancel_booking", booking_at=soon.isoformat()), now=NOW))
    assert not plan.ok and "cut-off" in plan.problem
    later = NOW + timedelta(days=1)
    appointment.starts_at = later
    plan = asyncio.run(actions.plan(_Db(first=appointment), business=b, customer=SimpleNamespace(id=uuid.uuid4()),
                                    proposal=actions.Proposal("cancel_booking", booking_at=later.isoformat()), now=NOW))
    assert plan.ok and plan.data == {"appointment_id": str(appointment.id)} and "cancel your booking" in plan.summary


def test_a_change_switched_off_is_never_carried_out():
    b = _business(actions={"cancel_booking": []})
    done = asyncio.run(actions.carry_out(_Db(), business=b, customer=SimpleNamespace(id=uuid.uuid4()),
                                         saved={"kind": "cancel_booking", "data": {}}, channel="voice"))
    assert not done.ok and "switched off" in done.problem


def test_a_confirmed_cancel_cancels_the_saved_booking_and_tells_the_team(monkeypatch):
    from shared.db.models import AppointmentStatus
    from shared.scheduling import booking

    customer = SimpleNamespace(id=uuid.uuid4(), display_name="Ravi", assigned_to_user_id=uuid.uuid4())
    appt_id = uuid.uuid4()
    appointment = SimpleNamespace(id=appt_id, customer_id=customer.id, status=AppointmentStatus.confirmed,
                                  starts_at=NOW + timedelta(days=1), doctor_id=uuid.uuid4())
    seen = {}

    async def cancel(db, *, appointment, reason=None):
        seen["reason"] = reason
        return appointment

    async def tell(db, *, business, customer, text):
        seen["team"] = text

    monkeypatch.setattr(booking, "cancel", cancel)
    monkeypatch.setattr(actions, "_tell_team", tell)
    saved = {"kind": "cancel_booking", "data": {"appointment_id": str(appt_id)}, "summary": "cancel your booking"}
    done = asyncio.run(actions.carry_out(_Db(objects={appt_id: appointment}), business=_business(), customer=customer,
                                         saved=saved, channel="voice", now=NOW))
    assert done.ok and "cancelled" in done.say and "a call" in seen["reason"] and "done on a call" in seen["team"]


# ── the WhatsApp / Instagram worker ─────────────────────────────────────────

def _draft(**kw):
    base = dict(action="reply", message="model words", reasoning="", gap=None, confidence=0.9, cost_paise=0,
                context_message_ids=[])
    base.update(kw)
    return agent_module.Draft(**base)


def test_a_new_change_is_asked_back_never_done_at_once(monkeypatch):
    from services.workers import respond

    async def none(db, customer_id):
        return None

    async def plan(db, *, business, customer, proposal, now=None):
        return actions.Plan(ok=True, kind=proposal.kind, data={"appointment_id": "a"}, summary="cancel your booking on Sun 2 Nov, 5:00 PM")

    async def never(*a, **k):
        raise AssertionError("must not be done before a yes")

    monkeypatch.setattr(respond, "_last_confirmation", none)
    monkeypatch.setattr(respond.ai_actions, "plan", plan)
    monkeypatch.setattr(respond.ai_actions, "carry_out", never)
    proposal, pending = asyncio.run(respond._changes(
        _draft(cancel_appointment_at="2026-11-02T17:00:00+05:30"), message=SimpleNamespace(content="kal wali booking cancel kar do"),
        business=_business(), customer=SimpleNamespace(id=uuid.uuid4()), channel="whatsapp", db=None,
    ))
    assert proposal.message.startswith("Confirm kar lein") and proposal.cancel_appointment_at is None
    assert pending["kind"] == "cancel_booking" and pending["data"] == {"appointment_id": "a"}


def test_a_yes_does_the_saved_change_whatever_the_model_says(monkeypatch):
    from services.workers import respond

    saved = {"kind": "cancel_booking", "data": {"appointment_id": "a"}, "summary": "s",
             "asked_at": datetime.now(timezone.utc).isoformat()}

    async def last(db, customer_id):
        return saved

    async def carry(db, *, business, customer, saved, channel, now=None):
        return actions.Done(ok=True, say="Done - your booking on Sun 2 Nov, 5:00 PM is cancelled.")

    monkeypatch.setattr(respond, "_last_confirmation", last)
    monkeypatch.setattr(respond.ai_actions, "carry_out", carry)
    proposal, pending = asyncio.run(respond._changes(
        _draft(action="no_action", message=None), message=SimpleNamespace(content="haan"),
        business=_business(), customer=SimpleNamespace(id=uuid.uuid4()), channel="whatsapp", db=None,
    ))
    assert proposal.action == "reply" and "is cancelled" in proposal.message and pending is None


def test_a_change_not_allowed_on_this_channel_goes_to_the_team(monkeypatch):
    from services.workers import respond

    async def none(db, customer_id):
        return None

    monkeypatch.setattr(respond, "_last_confirmation", none)
    proposal, pending = asyncio.run(respond._changes(
        _draft(order_items=[("Kurta", 2)]), message=SimpleNamespace(content="2 kurta bhej do"),
        business=_business(), customer=SimpleNamespace(id=uuid.uuid4()), channel="instagram", db=None,
    ))
    assert proposal.action == "escalate" and "not switched on" in proposal.gap and pending is None


def test_the_last_confirmation_query_compiles():
    from services.workers import respond

    assert asyncio.run(respond._last_confirmation(_Db(), uuid.uuid4())) is None


# ── calls ───────────────────────────────────────────────────────────────────

def test_a_calls_change_lines_become_one_change():
    from shared.channels.voice.pipeline import _voice_change

    start = agent_module.ReplyStart(action="reply", move_from="2026-11-02T17:00:00+05:30", move_to="2026-11-03T11:00:00+05:30")
    change = _voice_change(start)
    assert change.kind == "reschedule_booking" and change.new_at.startswith("2026-11-03")
    order = _voice_change(agent_module.ReplyStart(action="reply", order_lines=[("Kurta - M", 2)], pay="cod"))
    assert order.kind == "take_order" and order.lines == [("Kurta - M", 2)] and order.payment == "cod"
    assert _voice_change(agent_module.ReplyStart(action="reply", cancel_order="latest")).order_number is None
    assert _voice_change(agent_module.ReplyStart(action="reply")) is None


def test_order_lines_from_the_model_are_cleaned():
    assert agent_module._order_lines([{"name": " Kurta ", "quantity": 3}, {"name": ""}, {"quantity": 2}, "x"]) == [("Kurta", 3)]
    assert agent_module._order_lines([{"name": "Pen", "quantity": 500}]) == [("Pen", 99)]


# ── not steering back to the past ───────────────────────────────────────────

def _context(recent, channel="whatsapp", **kw):
    base = dict(
        business_name="Shop", vertical="general", dna_summary=None, tone=None, policies=None, known_gaps=[],
        escalate_immediately=[], offerings={}, pricing_notes=None, opening_hours={}, availability=None,
        upcoming_bookings="- 2026-11-02T17:00:00+05:30 with Dr Mehta", cases=None, orders=None, properties=None,
        claims=None, open_shifts=None, customer_name="Ravi", customer_summary="Asked about a cleaning on 12 Oct.",
        customer_since=None, identities=[], recent=recent, channel=channel, actions=["cancel_booking"],
        open_commitments=[{"direction": "they_owe", "description": "Consultation fee", "amount": "₹500", "due": None}],
    )
    base.update(kw)
    return ctx.AgentContext(**base)


def _said(*texts):
    return [{"channel": "whatsapp", "direction": "inbound", "text": t} for t in texts]


def test_bookings_and_dues_stay_back_until_the_customer_brings_them_up():
    live = _context(_said("hello, aapke yahan parking hai?")).render_live()
    assert "2026-11-02T17:00" not in live and "Consultation fee" not in live
    assert "shown only once the customer brings it up" in live and "never say there is nothing on record" in live
    asked = _context(_said("mera appointment kab hai?")).render_live()
    assert "2026-11-02T17:00:00+05:30 with Dr Mehta" in asked and "Consultation fee" in asked


def test_a_call_kroVA_placed_keeps_the_reason_it_called():
    recent = [{"channel": "voice", "direction": "outbound", "text": "Hi, calling about your pending fees of ₹500."},
              {"channel": "voice", "direction": "inbound", "text": "haan boliye"}]
    assert "Consultation fee" in _context(recent, channel="voice").render_live()


def test_what_the_ai_can_do_here_is_spelled_out():
    assert "What you can do here: Cancel a booking." in _context(_said("hi")).render_live()
    assert "answer questions only" in _context(_said("hi"), actions=[]).render_live()


def test_the_prompts_no_longer_push_the_past():
    assert "that is context worth using" not in agent_module.SYSTEM
    assert "across several earlier calls" not in agent_module.SYSTEM_STREAM
    assert "background for answering, never the topic" in agent_module.SYSTEM
    from shared.ai import compression, outbound_opener

    assert "never \"Wants to book a cleaning\"" in compression.SYSTEM
    assert "Speak only about the reason for this call" in outbound_opener.SYSTEM


# ── the Settings page API ───────────────────────────────────────────────────

def test_the_settings_api_shows_every_action_and_refuses_unknown_ones(monkeypatch):
    from fastapi import HTTPException

    from services.api.routers import ai_actions as api
    from shared import verticals

    monkeypatch.setattr(verticals, "has_capability", lambda business, cap: cap == "scheduling")
    business = _business()

    class Db:
        async def get(self, model, key):
            return business

        async def flush(self):
            return None

    user = SimpleNamespace(business=BIZ)
    out = asyncio.run(api.get_settings(user, Db()))
    by_key = {a.key: a for a in out.actions}
    assert by_key["cancel_booking"].available and not by_key["take_order"].available
    assert by_key["cancel_booking"].asks_to_confirm and not by_key["book"].asks_to_confirm
    saved = asyncio.run(api.put_settings(api.SettingsIn(actions={"take_order": ["whatsapp"]}, cutoff_hours=6), user, Db()))
    assert {a.key: a.channels for a in saved.actions}["take_order"] == ["whatsapp"] and saved.cutoff_hours == 6
    assert {a.key: a.channels for a in saved.actions}["cancel_booking"] == list(actions.CHANNELS)  # untouched
    with pytest.raises(HTTPException):
        asyncio.run(api.put_settings(api.SettingsIn(actions={"refund": ["voice"]}), user, Db()))


# ── an action is offered only when its features are on ─────────────────────

def _typed(vertical, overrides=None, actions=None):
    settings = {}
    if overrides:
        settings["capability_overrides"] = overrides
    if actions:
        settings["ai_actions"] = {"actions": actions}
    return SimpleNamespace(id=BIZ, settings=settings, timezone="Asia/Kolkata", vertical=vertical, capabilities=None)


def test_taking_an_order_needs_orders_and_the_price_list():
    on = {"take_order": ["whatsapp"], "cancel_order": ["whatsapp"]}
    shop = _typed("commerce", overrides={"price_list_payments": False}, actions=on)
    assert not actions.available(shop, "take_order") and actions.available(shop, "cancel_order")
    assert not actions.allowed(shop, "take_order", "whatsapp") and actions.allowed(shop, "cancel_order", "whatsapp")
    priced = _typed("commerce", actions=on)  # commerce starts with Price list & payments on
    assert actions.available(priced, "take_order") and actions.allowed(priced, "take_order", "whatsapp")
    other = _typed("b2b", overrides={"order_sync": True}, actions=on)  # any other type switches it on itself
    assert not actions.available(other, "take_order")


def test_a_switched_off_feature_hides_its_actions_even_when_the_owner_ticked_them():
    ticked = {"book": ["voice"], "cancel_booking": ["voice"], "reschedule_booking": [], "book_token": ["voice"],
              "cancel_token": []}
    plain = _typed("general", actions=ticked)
    assert actions.allowed_on(plain, "voice") == [] and not actions.allowed(plain, "book", "web")
    salon = _typed("local_service", actions=ticked)
    assert set(actions.allowed_on(salon, "voice")) == {"book", "cancel_booking", "book_token"}
    no_queue = _typed("local_service", overrides={"opd_queue": False}, actions=ticked)
    assert "book_token" not in actions.allowed_on(no_queue, "voice") and not actions.allowed(no_queue, "book_token", "web")


def test_the_settings_screen_shows_the_same_rule():
    from services.api.routers import ai_actions as api

    out = {a.key: a.available for a in api._out(_typed("commerce", overrides={"price_list_payments": False})).actions}
    assert out == {"book": False, "cancel_booking": False, "reschedule_booking": False, "book_token": False,
                   "cancel_token": False, "take_order": False, "cancel_order": True}
    assert set(actions.NEEDS) == set(actions.ACTIONS)

"""Feedback: asking how a visit, token, order or chat went - on WhatsApp, by call or both - and what happens next."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from sqlalchemy.dialects import postgresql  # noqa: E402

from shared.care import feedback  # noqa: E402
from shared.db.models import FeedbackRequest  # noqa: E402

BIZ = uuid.uuid4()
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _business(**fb):
    settings = {"feedback": fb} if fb else {}
    return SimpleNamespace(id=BIZ, name="Sharma Clinic", settings=settings, timezone="Asia/Kolkata",
                           vertical="local_service", is_active=True)


def test_feedback_is_off_until_switched_on_and_settings_are_cleaned():
    b = _business()
    s = feedback.read(b)
    assert not s["enabled"] and s["channel"] == "whatsapp" and s["triggers"] == ["visit", "token", "order"]
    out = feedback.write(b, {"enabled": True, "channel": "both", "triggers": ["chat", "junk"], "delay_minutes": 30,
                             "hack": 1})
    assert out["enabled"] and out["channel"] == "both" and out["triggers"] == ["chat"] and out["delay_minutes"] == 30
    assert "hack" not in b.settings["feedback"]
    assert feedback.read(_business(channel="sms", delay_minutes=-5))["channel"] == "whatsapp"


@pytest.mark.parametrize("said, rating", [
    ("4", 4), ("I'd say 5 out of 5", 5), ("char", 4), ("bahut achha tha", 5), ("theek tha", 3),
    ("kharab experience", 1), ("not good at all", 2), ("pata nahi", None), ("", None),
])
def test_a_spoken_rating_is_read_from_what_they_said(said, rating):
    assert feedback.rating_from(said) == rating


class _Db:
    def __init__(self, objects=None, first=None):
        self.objects, self.first, self.added = objects or {}, first, []

    async def get(self, model, key):
        return self.objects.get(key)

    async def execute(self, stmt, *a, **k):
        stmt.compile(dialect=postgresql.dialect())
        first = self.first
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: first, all=lambda: []), all=lambda: [],
                               first=lambda: None, scalar=lambda: 0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def _request(customer_id, **kw):
    base = dict(business_id=BIZ, customer_id=customer_id, trigger="visit", ref="appt:1", about="visit on 1 Nov",
                channel="whatsapp", status="sent")
    base.update(kw)
    r = FeedbackRequest(**base)
    r.id = uuid.uuid4()
    return r


def _tap(request, rating):
    return {"kind": "button_reply", "payload": f"fb:{request.id}:{rating}"}


def test_a_great_tap_is_kept_and_the_happy_customer_gets_the_review_link(monkeypatch):
    customer = SimpleNamespace(id=uuid.uuid4(), display_name="Asha")
    request = _request(customer.id)
    business = _business(enabled=True)
    business.settings["google_review_url"] = "https://g.page/r/sharma"
    reply = asyncio.run(feedback.handle_reply(_Db({request.id: request}), business=business, customer=customer,
                                              text="Great", media=_tap(request, 5), now=NOW))
    assert request.rating == 5 and request.status == "answered" and request.answered_via == "whatsapp"
    assert "https://g.page/r/sharma" in reply and request.review_sent and not request.awaiting_comment


def test_no_review_link_for_a_type_that_may_not_ask(monkeypatch):
    from shared.scheduling import recall

    monkeypatch.setattr(recall, "type_allows_reviews", lambda b: False)
    customer = SimpleNamespace(id=uuid.uuid4(), display_name="Asha")
    request = _request(customer.id)
    business = _business(enabled=True)
    business.settings["google_review_url"] = "https://g.page/r/x"
    reply = asyncio.run(feedback.handle_reply(_Db({request.id: request}), business=business, customer=customer,
                                              text="Great", media=_tap(request, 5), now=NOW))
    assert "g.page" not in reply and not request.review_sent


def test_not_good_goes_to_the_team_and_the_next_message_is_the_comment(monkeypatch):
    from shared.team import requests as team_requests

    filed = []

    async def open_request(db, *, business_id, customer, channel, what, **k):
        filed.append(what)

    monkeypatch.setattr(team_requests, "open_request", open_request)
    customer = SimpleNamespace(id=uuid.uuid4(), display_name="Asha")
    request = _request(customer.id)
    reply = asyncio.run(feedback.handle_reply(_Db({request.id: request}), business=_business(enabled=True),
                                              customer=customer, text="Not good", media=_tap(request, 1), now=NOW))
    assert request.rating == 1 and request.awaiting_comment and "What went wrong" in reply
    assert filed == ["Unhappy - rated 1/5 for their visit on 1 Nov"]

    reply = asyncio.run(feedback.handle_reply(_Db(first=request), business=_business(enabled=True), customer=customer,
                                              text="Waited 1 hour past my slot", media=None, now=NOW))
    assert request.comment == "Waited 1 hour past my slot" and not request.awaiting_comment
    assert "team" in reply and filed[-1].endswith(": Waited 1 hour past my slot")


def test_a_tap_from_someone_else_or_twice_changes_nothing():
    customer = SimpleNamespace(id=uuid.uuid4(), display_name="Asha")
    request = _request(uuid.uuid4())  # someone else's
    assert asyncio.run(feedback.handle_reply(_Db({request.id: request}), business=_business(), customer=customer,
                                             text="Great", media=_tap(request, 5), now=NOW)) is None
    mine = _request(customer.id, rating=3, status="answered")
    again = asyncio.run(feedback.handle_reply(_Db({mine.id: mine}), business=_business(), customer=customer,
                                              text="Great", media=_tap(mine, 5), now=NOW))
    assert mine.rating == 3 and "already" in again


def test_an_ordinary_message_is_left_to_the_ai():
    customer = SimpleNamespace(id=uuid.uuid4(), display_name="Asha")
    assert asyncio.run(feedback.handle_reply(_Db(first=None), business=_business(), customer=customer,
                                             text="price kya hai", media=None, now=NOW)) is None


def test_a_feedback_calls_answers_become_the_rating(monkeypatch):
    from shared.team import requests as team_requests

    async def open_request(*a, **k):
        return None

    monkeypatch.setattr(team_requests, "open_request", open_request)
    customer_id = uuid.uuid4()
    script = SimpleNamespace(id=uuid.uuid4(), name=feedback.SCRIPT_NAME)
    request = _request(customer_id, channel="voice", status="calling")
    db = _Db({script.id: script, BIZ: _business(enabled=True)}, first=request)
    assert asyncio.run(feedback.record_call(db, script_id=script.id, customer_id=customer_id, answers={
        feedback.CALL_QUESTIONS[0]: "Four, it was good", feedback.CALL_QUESTIONS[1]: "Parking is hard"}))
    assert request.rating == 4 and request.comment == "Parking is hard" and request.answered_via == "voice"
    other = SimpleNamespace(id=uuid.uuid4(), name="Lead qualification")
    assert not asyncio.run(feedback.record_call(_Db({other.id: other}), script_id=other.id, customer_id=customer_id,
                                                answers={"q": "5"}))


def test_sending_follows_the_chosen_channel(monkeypatch):
    calls, sent = [], []

    async def whatsapp(db, business, request):
        sent.append(request.id)
        return True

    async def call(db, business, request):
        calls.append(request.id)
        return True

    monkeypatch.setattr(feedback, "_send_whatsapp", whatsapp)
    monkeypatch.setattr(feedback, "_start_call", call)
    business = _business(enabled=True, call_after_hours=6)
    c = uuid.uuid4()
    wa, voice, both = _request(c, status="due"), _request(c, status="due", channel="voice"), _request(c, status="due", channel="both")

    class Db(_Db):
        async def execute(self, stmt, *a, **k):
            stmt.compile(dialect=postgresql.dialect())
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [wa, voice, both]))

        def begin_nested(self):
            class _N:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *a):
                    return False
            return _N()

    asyncio.run(feedback.send_due(Db({BIZ: business}), NOW))
    assert wa.status == "sent" and voice.status == "calling" and both.status == "sent"
    assert both.call_at == NOW + timedelta(hours=6) and wa.call_at is None
    assert sent == [wa.id, both.id] and calls == [voice.id]


def test_the_page_queries_compile():
    db = _Db()
    out = asyncio.run(feedback.stats(db, BIZ))
    assert out["answered"] == 0 and out["average"] is None and set(out["spread"]) == {"1", "2", "3", "4", "5"}
    for trigger in feedback.TRIGGERS:
        assert asyncio.run(feedback._candidates(db, _business(), trigger, NOW, timedelta(hours=2))) == []


def test_reviews_are_not_asked_twice_when_feedback_is_on():
    from shared.scheduling import recall

    assert not recall._asks_for_reviews(_business(enabled=True))
    assert recall._asks_for_reviews(_business())

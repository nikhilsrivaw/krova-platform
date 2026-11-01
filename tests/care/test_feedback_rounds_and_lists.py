"""Feedback rounds (ask again until they answer) and customers added by hand - file, paste, form, API."""

import asyncio
import io
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
# 12:00 UTC = 17:30 in India - inside the default asking hours (9 AM to 8 PM)
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _business(**fb):
    return SimpleNamespace(id=BIZ, name="Sharma Travels", settings={"feedback": {"enabled": True, **fb}},
                           timezone="Asia/Kolkata", vertical="local_service", is_active=True)


# ── settings ────────────────────────────────────────────────────────────────

def test_rounds_are_cleaned_and_old_settings_still_work():
    b = _business(rounds=[{"channel": "whatsapp", "wait_hours": 5}, {"channel": "fax"}, {"channel": "voice", "wait_hours": 24},
                          {"channel": "voice", "wait_hours": 999}])
    assert feedback.read(b)["rounds"] == [{"channel": "whatsapp", "wait_hours": 0}, {"channel": "voice", "wait_hours": 24},
                                          {"channel": "voice", "wait_hours": 24}]
    assert feedback.read(b)["channel"] == "both"
    old = _business(channel="both", call_after_hours=6)
    assert feedback.read(old)["rounds"] == [{"channel": "whatsapp", "wait_hours": 0}, {"channel": "voice", "wait_hours": 6}]
    assert len(feedback.read(_business(rounds=[{"channel": "voice", "wait_hours": 2}] * 9))["rounds"]) == feedback.MAX_ROUNDS


def test_asking_hours_follow_the_business_clock():
    b = _business(ask_from=9, ask_until=20)
    assert feedback.asking_hours(b, NOW)  # 5:30 PM IST
    assert not feedback.asking_hours(b, datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc))  # 10:30 PM IST
    assert feedback.read(_business(ask_from=20, ask_until=9))["ask_from"] == 9  # nonsense falls back


def test_own_review_link_wins_over_google():
    b = _business(review_link="https://www.redbus.in/review/sharma")
    b.settings["google_review_url"] = "https://g.page/r/x"
    assert feedback.review_url(b) == "https://www.redbus.in/review/sharma"
    assert feedback.read(_business(review_link="javascript:alert(1)"))["review_link"] is None


# ── rounds ──────────────────────────────────────────────────────────────────

class _Db:
    def __init__(self, rows=(), objects=None):
        self.rows, self.objects = list(rows), objects or {}

    async def execute(self, stmt, *a, **k):
        stmt.compile(dialect=postgresql.dialect())
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows, first=lambda: None), first=lambda: None)

    async def get(self, model, key):
        return self.objects.get(key)

    def begin_nested(self):
        class _N:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False
        return _N()

    async def flush(self):
        return None


def _request(**kw):
    base = dict(business_id=BIZ, customer_id=uuid.uuid4(), trigger="external", ref="ext:1", about="trip on 2 Nov",
                channel="both", status="due", send_at=NOW, next_at=NOW, round=0)
    base.update(kw)
    r = FeedbackRequest(**base)
    r.id = uuid.uuid4()
    return r


def _channels(monkeypatch, whatsapp_ok=True, call_ok=True):
    log = []

    async def wa(db, business, request):
        log.append("whatsapp")
        return whatsapp_ok

    async def call(db, business, request):
        log.append("voice")
        return call_ok

    monkeypatch.setattr(feedback, "_send_whatsapp", wa)
    monkeypatch.setattr(feedback, "_start_call", call)
    return log


ROUNDS = [{"channel": "whatsapp", "wait_hours": 0}, {"channel": "voice", "wait_hours": 24}, {"channel": "voice", "wait_hours": 48}]


def test_rounds_go_one_after_another_until_the_last_then_no_reply(monkeypatch):
    log = _channels(monkeypatch)
    business = _business(rounds=ROUNDS)
    r = _request()
    db = _Db([r], {BIZ: business})
    asyncio.run(feedback.send_due(db, NOW))
    assert (r.status, r.round, r.next_at) == ("sent", 1, NOW + timedelta(hours=24)) and log == ["whatsapp"]
    later = NOW + timedelta(hours=24)
    asyncio.run(feedback.send_due(db, later))
    assert (r.status, r.round, r.next_at) == ("calling", 2, later + timedelta(hours=48)) and log[-1] == "voice"
    last = later + timedelta(hours=48)
    asyncio.run(feedback.send_due(db, last))
    assert (r.round, r.next_at) == (3, last + feedback.LAST_WAIT["voice"])
    asyncio.run(feedback.send_due(db, last + timedelta(hours=4)))
    assert r.status == "no_reply" and r.next_at is None and log == ["whatsapp", "voice", "voice"]


def test_a_round_that_cannot_go_out_moves_straight_to_the_next(monkeypatch):
    log = _channels(monkeypatch, whatsapp_ok=False)
    r = _request()
    db = _Db([r], {BIZ: _business(rounds=ROUNDS)})
    asyncio.run(feedback.send_due(db, NOW))
    assert r.round == 1 and r.next_at == NOW and r.status == "due" and "not approved" in r.note
    asyncio.run(feedback.send_due(db, NOW))
    assert r.status == "calling" and log == ["whatsapp", "voice"]


def test_nothing_is_sent_outside_asking_hours_or_when_switched_off(monkeypatch):
    log = _channels(monkeypatch)
    r = _request()
    night = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)  # 11:30 PM IST
    asyncio.run(feedback.send_due(_Db([r], {BIZ: _business(rounds=ROUNDS)}), night))
    off = _business(rounds=ROUNDS)
    off.settings["feedback"]["enabled"] = False
    asyncio.run(feedback.send_due(_Db([r], {BIZ: off}), NOW))
    assert log == [] and r.status == "due" and r.round == 0


def test_an_answer_stops_the_rounds(monkeypatch):
    customer = SimpleNamespace(id=uuid.uuid4(), display_name="Ravi")
    r = _request(customer_id=customer.id, status="sent", round=1, next_at=NOW + timedelta(hours=24))
    asyncio.run(feedback.handle_reply(_Db(objects={r.id: r}), business=_business(), customer=customer, text="Great",
                                      media={"kind": "button_reply", "payload": f"fb:{r.id}:5"}, now=NOW))
    assert r.status == "answered" and r.next_at is None


def test_a_happy_feedback_call_sends_the_review_link(monkeypatch):
    from shared.scheduling import notify
    from shared.team import requests as team_requests

    sent = {}

    async def review(db, *, business, customer, about, review_url):
        sent.update(about=about, url=review_url)
        return True

    async def none(*a, **k):
        return None

    monkeypatch.setattr(notify, "send_feedback_review_link", review)
    monkeypatch.setattr(team_requests, "open_request", none)
    customer = SimpleNamespace(id=uuid.uuid4(), display_name="Ravi")
    script = SimpleNamespace(id=uuid.uuid4(), name=feedback.SCRIPT_NAME)
    r = _request(customer_id=customer.id, status="calling", round=2)
    business = _business(review_link="https://www.redbus.in/review/sharma")

    class Db(_Db):
        async def execute(self, stmt, *a, **k):
            stmt.compile(dialect=postgresql.dialect())
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: r))

    db = Db(objects={script.id: script, BIZ: business, customer.id: customer})
    asyncio.run(feedback.record_call(db, script_id=script.id, customer_id=customer.id,
                                     answers={"q1": "paanch", "q2": "Driver was very polite"}))
    assert r.rating == 5 and r.review_sent and sent == {"about": "trip on 2 Nov", "url": "https://www.redbus.in/review/sharma"}


def test_a_call_with_no_answers_leaves_the_next_round_to_run():
    customer_id = uuid.uuid4()
    script = SimpleNamespace(id=uuid.uuid4(), name=feedback.SCRIPT_NAME)
    r = _request(customer_id=customer_id, status="calling", round=1, next_at=NOW + timedelta(hours=24))

    class Db(_Db):
        async def execute(self, stmt, *a, **k):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: r))

    asyncio.run(feedback.record_call(Db(objects={script.id: script}), script_id=script.id, customer_id=customer_id, answers={}))
    assert r.status == "calling" and r.next_at == NOW + timedelta(hours=24)


# ── lists: file, paste, form ────────────────────────────────────────────────

def test_a_csv_with_its_own_column_names_is_read():
    csv = "Passenger Name,Mobile No,Route,Travel Date,PNR\nRavi,98765 43210,Delhi to Jaipur,02/11/2026,PNR1\n,,,,\n"
    rows = feedback.parse_file("trips.csv", csv.encode())
    assert rows == [{"name": "Ravi", "phone": "98765 43210", "about": "Delhi to Jaipur", "date": "02/11/2026", "ref": "PNR1"}]


def test_an_excel_file_is_read():
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Customer", "Phone", "Service", "Date"])
    sheet.append(["Asha", "9876500000", "AC repair", datetime(2026, 11, 1, 15, 30)])
    buffer = io.BytesIO()
    book.save(buffer)
    rows = feedback.parse_file("list.xlsx", buffer.getvalue())
    assert rows[0]["name"] == "Asha" and rows[0]["date"] == datetime(2026, 11, 1, 15, 30)


def test_a_list_pasted_from_a_sheet_needs_no_header():
    rows = feedback.parse_text("Ravi\t9876543210\tDelhi to Jaipur\t2 Nov 2026\nNeha\t9123456780\tJaipur to Delhi\t3 Nov 2026")
    assert [r["phone"] for r in rows] == ["9876543210", "9123456780"] and rows[1]["about"] == "Jaipur to Delhi"


def test_lists_that_cannot_be_read_say_why():
    with pytest.raises(ValueError, match="xlsx"):
        feedback.parse_file("old.xls", b"x")
    with pytest.raises(ValueError, match="phone"):
        feedback.parse_text("name,city\nRavi,Delhi")


@pytest.mark.parametrize("value, expected", [
    ("02/11/2026", (2026, 11, 2)), ("2 Nov 2026", (2026, 11, 2)), ("2026-11-02 18:30", (2026, 11, 2)),
])
def test_dates_are_read_the_indian_way(value, expected):
    at = feedback._when(_business(), value)
    assert (at.year, at.month, at.day) == expected and at.tzinfo is not None


def test_adding_customers_asks_after_the_trip_and_explains_what_was_skipped(monkeypatch):
    from shared.identity import resolver

    customer = SimpleNamespace(id=uuid.uuid4(), is_private=False)

    async def resolve(business_id, kind, value, db, display_name=None, **k):
        return SimpleNamespace(customer=customer)

    async def not_recent(db, customer_id, since):
        return False

    monkeypatch.setattr(resolver, "resolve", resolve)
    monkeypatch.setattr(feedback, "_recently_asked", not_recent)
    added = []

    class Db(_Db):
        def add(self, obj):
            added.append(obj)

    rows = [
        {"name": "Ravi", "phone": "9876543210", "about": "Delhi to Jaipur bus", "date": "03/11/2026 06:00"},
        {"name": "Bad", "phone": "12"},
        {"name": "Old", "phone": "9876543211", "date": "01/08/2026"},
        {"name": "Odd", "phone": "9876543212", "date": "not a date"},
    ]
    report = asyncio.run(feedback.add_external(Db(), _business(delay_minutes=120), rows, source="upload", now=NOW))
    assert report["added"] == 1 and [s["row"] for s in report["skipped"]] == [2, 3, 4]
    r = added[0]
    assert r.about == "Delhi to Jaipur bus on 3 Nov" and r.source == "upload" and r.trigger == "external"
    assert r.next_at == r.happened_at + timedelta(minutes=120)  # asked two hours after the trip, not now


def test_a_trip_name_with_a_date_is_kept_as_it_is(monkeypatch):
    from shared.identity import resolver

    async def resolve(*a, **k):
        return SimpleNamespace(customer=SimpleNamespace(id=uuid.uuid4(), is_private=False))

    async def not_recent(*a, **k):
        return False

    monkeypatch.setattr(resolver, "resolve", resolve)
    monkeypatch.setattr(feedback, "_recently_asked", not_recent)
    added = []

    class Db(_Db):
        def add(self, obj):
            added.append(obj)

    asyncio.run(feedback.add_external(Db(), _business(), [{"phone": "9876543210", "about": "PNR 4471 Delhi-Jaipur"}],
                                      source="form", now=NOW))
    assert added[0].about == "PNR 4471 Delhi-Jaipur" and added[0].next_at == NOW + timedelta(minutes=120)


def test_the_form_and_api_shapes():
    from services.api.routers import feedback as api
    from services.api.routers import public_api

    assert api.AddIn(phone="9876543210", about="trip").model_dump()["date"] is None
    body = public_api.FeedbackIn(customers=[{"phone": "9876543210", "about": "trip", "ref": "PNR1"}])
    assert body.customers[0].ref == "PNR1"


@pytest.mark.parametrize("typed, kept", [
    ("how was the product", "product"), ("How was your trip?", "trip"), ("product kaisa laga", "product"),
    ("feedback on AC repair", "AC repair"), ("How did you find the service?", "service"),
    ("Delhi to Jaipur bus", "Delhi to Jaipur bus"), ("Theme park visit", "Theme park visit"),
    ("Howrah to Puri trip", "Howrah to Puri trip"), ("Rate card printing", "Rate card printing"),
])
def test_a_question_typed_as_what_is_turned_into_the_thing(typed, kept):
    assert feedback.clean_what(typed) == kept


class _StatusDb(_Db):
    async def execute(self, stmt, *a, **k):
        stmt.compile(dialect=postgresql.dialect())
        rows = self.rows
        return SimpleNamespace(all=lambda: [(s,) for s in rows])


@pytest.mark.parametrize("statuses, why", [
    (["due"], "waiting"), (["calling", "answered"], "waiting"), (["answered"], "asked"), ([], None),
])
def test_only_a_request_lined_up_or_sent_counts_against_the_cooldown(statuses, why):
    assert asyncio.run(feedback._recently_asked(_StatusDb(statuses), uuid.uuid4(), NOW - timedelta(days=7))) == why


@pytest.mark.parametrize("why, reason", [
    ("waiting", "Already on the list, waiting to be asked"), ("asked", "Asked in the last 7 days"),
])
def test_a_skipped_customer_is_told_the_real_reason(monkeypatch, why, reason):
    from shared.identity import resolver

    async def resolve(*a, **k):
        return SimpleNamespace(customer=SimpleNamespace(id=uuid.uuid4(), is_private=False))

    async def recent(*a, **k):
        return why

    monkeypatch.setattr(resolver, "resolve", resolve)
    monkeypatch.setattr(feedback, "_recently_asked", recent)
    report = asyncio.run(feedback.add_external(_Db(), _business(), [{"phone": "9876543210", "about": "order"}],
                                               source="form", now=NOW))
    assert report["added"] == 0 and report["skipped"] == [{"row": 1, "reason": reason}]


def test_the_form_saves_the_thing_not_the_question(monkeypatch):
    from shared.identity import resolver

    async def resolve(*a, **k):
        return SimpleNamespace(customer=SimpleNamespace(id=uuid.uuid4(), is_private=False))

    async def not_recent(*a, **k):
        return None

    monkeypatch.setattr(resolver, "resolve", resolve)
    monkeypatch.setattr(feedback, "_recently_asked", not_recent)
    added = []

    class Db(_Db):
        def add(self, obj):
            added.append(obj)

    asyncio.run(feedback.add_external(Db(), _business(), [{"phone": "9876543210", "about": "how was the product",
                                                           "date": "30/10/2026"}], source="form", now=NOW))
    assert added[0].about == "product on 30 Oct"

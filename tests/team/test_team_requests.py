"""Talk to team: a customer's ask for a person, on any channel, waiting for whoever is free."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from sqlalchemy.dialects import postgresql  # noqa: E402

from shared.db.models import CustomerNote, TeamRequest  # noqa: E402
from shared.team import requests as team_requests  # noqa: E402

BIZ = uuid.uuid4()


@pytest.mark.parametrize("text, asks", [
    ("mujhe team se baat karni hai", True),
    ("Can I talk to someone from your team?", True),
    ("please call me back after 6", True),
    ("manager ko bulao", True),
    ("mujhe call karo kal", True),
    ("team size kitna hai?", False),
    ("sales team ka contact number do", False),
    ("price kya hai", False),
])
def test_what_counts_as_asking_for_a_person(text, asks):
    assert team_requests.asks_for_person(text) is asks


def test_the_customer_hears_it_reached_the_team_in_their_language():
    assert "team tak" in team_requests.acknowledgement("hinglish")
    assert "our team" in team_requests.acknowledgement("english")
    assert "टीम" in team_requests.acknowledgement("hindi")


class _Db:
    def __init__(self, existing=None, objects=None):
        self.existing, self.objects, self.added = existing, objects or {}, []

    async def execute(self, stmt, *a, **k):
        stmt.compile(dialect=postgresql.dialect())
        existing = self.existing
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: existing, all=lambda: []), all=lambda: [])

    async def get(self, model, key):
        return self.objects.get(key)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def _customer(**kw):
    base = dict(id=uuid.uuid4(), display_name="Ravi", team_id=None, assigned_to_user_id=None, assigned_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_new_ask_is_filed_and_the_free_people_are_told(monkeypatch):
    told = {}

    async def people(db, business_id, team_id):
        return ["u1"]

    async def push(db, business_id, user_ids, title, body):
        told.update(ids=user_ids, title=title, body=body)

    monkeypatch.setattr(team_requests, "_people_to_tell", people)
    monkeypatch.setattr(team_requests, "_push", push)
    db = _Db()
    request = asyncio.run(team_requests.open_request(db, business_id=BIZ, customer=_customer(), channel="instagram",
                                                     what="  team se   baat karni hai  "))
    assert isinstance(request, TeamRequest) and request.status == "open" and request.channel == "instagram"
    assert request.what == "team se baat karni hai" and told["title"] == "Talk to team: Ravi" and told["ids"] == ["u1"]


def test_asking_again_adds_to_the_open_request_instead_of_a_second_one(monkeypatch):
    async def never(*a, **k):
        raise AssertionError("no second push")

    monkeypatch.setattr(team_requests, "_push", never)
    existing = TeamRequest(business_id=BIZ, customer_id=uuid.uuid4(), channel="voice", what="call me back", status="open")
    db = _Db(existing=existing)
    out = asyncio.run(team_requests.open_request(db, business_id=BIZ, customer=_customer(), channel="whatsapp",
                                                 what="kal 6 baje ke baad"))
    assert out is existing and existing.what == "call me back | kal 6 baje ke baad" and not db.added


def test_taking_a_request_makes_the_chat_yours_and_someone_else_is_refused():
    me, other = uuid.uuid4(), uuid.uuid4()
    customer = _customer()
    request = TeamRequest(business_id=BIZ, customer_id=customer.id, channel="voice", what="x", status="open")
    db = _Db(objects={customer.id: customer, other: SimpleNamespace(full_name="Priya")})
    asyncio.run(team_requests.take(db, request=request, user_id=me))
    assert request.status == "taken" and request.taken_by_user_id == me and customer.assigned_to_user_id == me
    request.taken_by_user_id = other
    with pytest.raises(team_requests.Taken) as err:
        asyncio.run(team_requests.take(db, request=request, user_id=me))
    assert err.value.name == "Priya"
    asyncio.run(team_requests.take(db, request=request, user_id=me, override=True))
    assert request.taken_by_user_id == me


def test_closing_with_a_note_leaves_it_on_the_customer():
    request = TeamRequest(business_id=BIZ, customer_id=uuid.uuid4(), channel="web", what="refund ke baare mein", status="taken")
    db = _Db()
    asyncio.run(team_requests.finish(db, request=request, user_id=uuid.uuid4(), note="Refund on Monday, told him"))
    assert request.status == "done" and request.outcome == "Refund on Monday, told him"
    note = db.added[0]
    assert isinstance(note, CustomerNote) and "refund ke baare mein" in note.body and "Refund on Monday" in note.body
    request2 = TeamRequest(business_id=BIZ, customer_id=uuid.uuid4(), channel="web", what="x", status="open")
    db2 = _Db()
    asyncio.run(team_requests.finish(db2, request=request2, user_id=uuid.uuid4(), note=None, cancelled=True))
    assert request2.status == "cancelled" and not db2.added


def test_the_reminder_query_compiles():
    assert asyncio.run(team_requests.remind_waiting(_Db(), datetime.now(timezone.utc))) == 0


def test_the_page_queries_compile(monkeypatch):
    from services.api.routers import team_requests as api

    async def everyone(db, user):
        return None

    monkeypatch.setattr(api.access, "visible_ids", everyone)
    user = SimpleNamespace(business=BIZ, id=uuid.uuid4(), role="agent")

    class Db(_Db):
        async def execute(self, stmt, *a, **k):
            stmt.compile(dialect=postgresql.dialect())
            return SimpleNamespace(all=lambda: [], scalar=lambda: 0)

    for state in ("waiting", "open", "taken", "done", "all"):
        assert asyncio.run(api.list_requests(user, Db(), state=state, mine=False, limit=100)) == []
    assert asyncio.run(api.count_requests(user, Db())).open == 0


def test_only_the_owner_or_an_admin_hands_a_request_to_someone():
    from fastapi import HTTPException

    from services.api.routers import team_requests as api

    with pytest.raises(HTTPException) as err:
        asyncio.run(api.assign_request(uuid.uuid4(), api.AssignIn(user_id=uuid.uuid4()),
                                       SimpleNamespace(business=BIZ, id=uuid.uuid4(), role="agent"), _Db()))
    assert err.value.status_code == 403


def test_a_chat_ask_for_a_person_is_filed_and_answered_without_the_model(monkeypatch):
    """The WhatsApp/Instagram worker: the request is filed and the reply is the fixed acknowledgement."""
    import inspect

    from services.workers import respond

    source = inspect.getsource(respond)
    assert "team_requests.asks_for_person(message.content)" in source
    assert "team_requests.acknowledgement(" in source


def test_the_website_asks_a_visitor_for_a_number_before_filing(monkeypatch):
    from shared.channels.web import reply as web_reply

    session = SimpleNamespace(customer_id=None, transcript=[], needed_person_at=None)

    class Db:
        async def get(self, model, key):
            return None

    out = asyncio.run(web_reply.generate_reply(Db(), business=SimpleNamespace(id=BIZ), session=session,
                                               text="mujhe team se baat karni hai"))
    assert out.needs_contact and session.needed_person_at is not None


def test_a_known_website_visitor_is_filed_and_told(monkeypatch):
    from shared.channels.web import reply as web_reply

    customer = _customer()
    filed = {}

    async def ingest(*a, **k):
        return uuid.uuid4()

    async def open_request(db, *, business_id, customer, channel, what, **k):
        filed.update(channel=channel, what=what)

    monkeypatch.setattr(web_reply, "_do_ingest", ingest)
    monkeypatch.setattr(team_requests, "open_request", open_request)

    class Db:
        async def get(self, model, key):
            return customer

    session = SimpleNamespace(customer_id=customer.id, transcript=[], needed_person_at=None)
    out = asyncio.run(web_reply.generate_reply(Db(), business=SimpleNamespace(id=BIZ), session=session,
                                               text="please call me back"))
    assert filed == {"channel": "web", "what": "please call me back"} and "our team" in out.reply_text

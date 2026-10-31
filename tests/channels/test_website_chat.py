"""Website chat: which sites the widget runs on, a person's replies reaching the widget, the AI stepping aside."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from shared.ai import agent as agent_module  # noqa: E402
from shared.channels.web import reply as web_reply  # noqa: E402
from shared.channels.web import sites  # noqa: E402

BIZ = uuid.uuid4()


@pytest.mark.parametrize("raw, expected", [
    ("example.com", "example.com"),
    ("https://www.Example.com/contact?x=1", "example.com"),
    ("shop.example.co.in", "shop.example.co.in"),
    ("http://example.com:8080", "example.com"),
    ("not a site", None),
    ("localhost", None),
    ("", None),
])
def test_a_website_is_kept_as_its_bare_domain(raw, expected):
    assert sites.clean_domain(raw) == expected


def test_the_widget_runs_on_the_domain_and_its_www_form_only():
    assert sites.host_allowed("example.com", "example.com")
    assert sites.host_allowed("WWW.example.com", "example.com")
    assert not sites.host_allowed("evil-example.com", "example.com")
    assert not sites.host_allowed("shop.example.com", "example.com")
    assert not sites.host_allowed(None, "example.com")


def test_only_a_real_colour_is_kept():
    assert sites.clean_colour("#12abEF") == "#12abEF"
    assert sites.clean_colour("red") is None and sites.clean_colour(None) is None


class _Db:
    async def execute(self, stmt, *a, **k):
        stmt.compile(dialect=postgresql.dialect())
        return SimpleNamespace(first=lambda: None, scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None))


def test_the_widget_queries_compile():
    db = _Db()
    assert asyncio.run(sites.team_replies(db, uuid.uuid4(), datetime.now(timezone.utc))) == []
    assert asyncio.run(sites.person_replied_recently(db, uuid.uuid4())) is False
    assert asyncio.run(sites.latest_session(db, BIZ, uuid.uuid4())) is None


def _business():
    return SimpleNamespace(id=BIZ)


def _session(customer_id=None):
    return SimpleNamespace(customer_id=customer_id, transcript=[])


class _GetDb:
    def __init__(self, customer):
        self.customer = customer

    async def get(self, model, key):
        return self.customer


def test_while_a_person_is_talking_the_ai_stays_quiet_and_they_are_told(monkeypatch):
    customer = SimpleNamespace(id=uuid.uuid4(), assigned_to_user_id=uuid.uuid4(), display_name="Ravi")
    told = []

    async def ingest(*a, **k):
        return uuid.uuid4()

    async def recently(db, customer_id):
        return True

    async def tell(db, *, business, customer, text):
        told.append(text)

    async def never(*a, **k):
        raise AssertionError("the AI must not answer")

    monkeypatch.setattr(web_reply, "_do_ingest", ingest)
    monkeypatch.setattr(web_reply.web_sites, "person_replied_recently", recently)
    monkeypatch.setattr(web_reply, "_tell_person", tell)
    monkeypatch.setattr(web_reply.agent_context, "build", never)
    out = asyncio.run(web_reply.generate_reply(_GetDb(customer), business=_business(),
                                               session=_session(customer.id), text="kab tak aayega?"))
    assert out.with_person and out.reply_text == "" and told == ["kab tak aayega?"]


def _stream(*events):
    async def gen(context):
        for e in events:
            yield e
    return gen


def test_an_anonymous_visitor_who_needs_a_person_is_asked_for_a_number(monkeypatch):
    async def anon(*a, **k):
        return SimpleNamespace()

    async def quiet(*a, **k):
        return None

    monkeypatch.setattr(web_reply.agent_context, "build_anonymous", anon)
    monkeypatch.setattr(agent_module, "stream_reply", _stream(
        agent_module.ReplyStart(action="escalate"), agent_module.ReplyDone(gap="the refund status", cost_paise=0)))
    monkeypatch.setattr(agent_module, "notify_escalation", quiet)
    monkeypatch.setattr(agent_module, "record_gap", quiet)
    session = _session()
    out = asyncio.run(web_reply.generate_reply(_GetDb(None), business=_business(), session=session,
                                               text="I want to talk to someone"))
    assert out.needs_contact and "phone number" in out.reply_text
    assert session.transcript[-1]["role"] == "assistant"


def test_a_known_visitor_who_needs_a_person_is_handed_to_one(monkeypatch):
    customer = SimpleNamespace(id=uuid.uuid4(), assigned_to_user_id=None, display_name="Ravi")
    handed = []

    async def ingest(*a, **k):
        return uuid.uuid4()

    async def not_recently(db, customer_id):
        return False

    async def ctx(*a, **k):
        return SimpleNamespace()

    async def quiet(*a, **k):
        return None

    async def hand(db, *, business, customer, reason):
        handed.append(reason)

    monkeypatch.setattr(web_reply, "_do_ingest", ingest)
    monkeypatch.setattr(web_reply.web_sites, "person_replied_recently", not_recently)
    monkeypatch.setattr(web_reply.agent_context, "build", ctx)
    monkeypatch.setattr(agent_module, "stream_reply", _stream(
        agent_module.ReplyStart(action="escalate"), agent_module.ReplyDone(gap=None, cost_paise=0)))
    monkeypatch.setattr(agent_module, "notify_escalation", quiet)
    monkeypatch.setattr(web_reply, "_hand_to_person", hand)
    out = asyncio.run(web_reply.generate_reply(_GetDb(customer), business=_business(),
                                               session=_session(customer.id), text="manager se baat karao"))
    assert handed == ["needs a person"] and "reply right here" in out.reply_text and not out.needs_contact


def _send_setup(monkeypatch, *, seen_ago):
    from services.api.routers import messages
    from shared.billing import guards

    customer = SimpleNamespace(id=uuid.uuid4(), business_id=BIZ)

    async def ok(*a, **k):
        return None

    async def session(db, business_id, customer_id):
        return None if seen_ago is None else SimpleNamespace(last_seen_at=datetime.now(timezone.utc) - seen_ago)

    monkeypatch.setattr(guards, "require_active_plan", ok)
    monkeypatch.setattr(messages.access, "check_id", ok)
    monkeypatch.setattr(messages.conflict, "guard_reply", ok)
    monkeypatch.setattr(sites, "latest_session", session)
    user = SimpleNamespace(business=BIZ, id=uuid.uuid4(), role="owner")
    return messages, customer, user


@pytest.mark.parametrize("seen_ago", [None, timedelta(days=8)])
def test_a_reply_to_a_visitor_who_left_long_ago_is_refused(monkeypatch, seen_ago):
    messages, customer, user = _send_setup(monkeypatch, seen_ago=seen_ago)
    with pytest.raises(HTTPException) as err:
        asyncio.run(messages.send_web_text(messages.SendWebText(customer_id=customer.id, body="hello"), user,
                                           _GetDb(customer)))
    assert err.value.status_code == 409 and "WhatsApp" in err.value.detail


def test_a_reply_to_another_business_customer_is_not_found(monkeypatch):
    messages, customer, user = _send_setup(monkeypatch, seen_ago=timedelta(minutes=1))
    other = SimpleNamespace(id=customer.id, business_id=uuid.uuid4())
    with pytest.raises(HTTPException) as err:
        asyncio.run(messages.send_web_text(messages.SendWebText(customer_id=customer.id, body="hello"), user,
                                           _GetDb(other)))
    assert err.value.status_code == 404


def test_the_site_api_refuses_a_bad_address():
    from services.api.routers import web_chat

    with pytest.raises(HTTPException):
        web_chat._domain("my shop")
    assert web_chat._domain("https://www.myshop.in/") == "myshop.in"


def test_the_widget_script_reads_its_look_and_asks_for_team_replies():
    from services.api.routers.widget_asset import _WIDGET_JS

    assert "/config" in _WIDGET_JS and "/updates?session_token=" in _WIDGET_JS and "team_can_reply" in _WIDGET_JS

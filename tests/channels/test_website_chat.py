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
    return SimpleNamespace(customer_id=customer_id, transcript=[], needed_person_at=None)


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


# ── refinements: settings, history, contact first, the Website chat page ───

def test_settings_keep_only_known_choices():
    assert sites.pick("left", sites.POSITIONS) == "left" and sites.pick("middle", sites.POSITIONS) is None
    assert sites.clean_starters([" Price? ", "", "Price?", "Where?", "a", "b", "c"]) == ["Price?", "Where?", "a", "b"]
    assert sites.clean_page("https://shop.in/pricing?phone=98765#x") == "https://shop.in/pricing"
    assert sites.clean_page("javascript:alert(1)") is None and sites.clean_page(None) is None


def test_the_install_check_is_written_at_most_every_few_minutes():
    site = SimpleNamespace(last_loaded_at=None)
    now = datetime.now(timezone.utc)
    assert sites.note_loaded(site, now) and site.last_loaded_at == now
    assert not sites.note_loaded(site, now + timedelta(minutes=1))
    assert sites.note_loaded(site, now + timedelta(minutes=6))


def test_an_anonymous_chat_comes_back_from_its_own_transcript():
    session = SimpleNamespace(customer_id=None, transcript=[
        {"role": "user", "text": "price?", "at": "t1"}, {"role": "assistant", "text": "2,499", "at": "t2"}])
    out = asyncio.run(sites.history(_Db(), session))
    assert out == [{"role": "user", "text": "price?", "at": "t1"}, {"role": "bot", "text": "2,499", "at": "t2"}]


def test_a_known_visitors_history_is_only_this_chat():
    seen = {}

    class Db:
        async def execute(self, stmt, *a, **k):
            seen["sql"] = str(stmt.compile(dialect=postgresql.dialect()))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    session = SimpleNamespace(customer_id=uuid.uuid4(), created_at=datetime.now(timezone.utc), transcript=[])
    assert asyncio.run(sites.history(Db(), session)) == []
    assert "messages.occurred_at >=" in seen["sql"]  # never the customer's earlier conversations


def _widget_db(config, business, session=None):
    class Db:
        def __init__(self):
            self.added = []

        async def execute(self, stmt, *a, **k):
            stmt.compile(dialect=postgresql.dialect())
            text = str(stmt)
            if "web_widget_configs" in text and "web_sessions" not in text:
                row = config
            elif "web_sessions" in text:
                row = session
            else:
                row = None
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row), scalar_one_or_none=lambda: None)

        async def get(self, model, key):
            return business

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            return None

    return Db()


def _config(**kw):
    base = dict(id=uuid.uuid4(), business_id=BIZ, active=True, title=None, greeting=None, accent=None, position=None,
                launcher_text=None, teaser=None, starters=[], ask_contact=None, language=None, theme=None,
                whatsapp_button=False, last_loaded_at=None, rate_limit_window_started_at=None, rate_limit_count=0)
    base.update(kw)
    return SimpleNamespace(**base)


def _no_plan_block(monkeypatch):
    from services.api.routers import widget

    async def ok(db, business_id):
        return False

    monkeypatch.setattr(widget, "plan_blocked", ok)
    return widget


def test_the_widget_gets_its_defaults_and_notes_it_was_loaded(monkeypatch):
    widget = _no_plan_block(monkeypatch)
    config = _config(language="hi")
    look = asyncio.run(widget.widget_config("KEY", _widget_db(config, SimpleNamespace(id=BIZ, is_active=True))))
    assert look.title == "Chat with us" and look.position == "right" and look.ask_contact == "when_needed"
    assert look.greeting == widget.DEFAULT_GREETING_HI and look.whatsapp is None
    assert config.last_loaded_at is not None


def test_contact_first_keeps_the_ai_out_until_a_number_is_given(monkeypatch):
    widget = _no_plan_block(monkeypatch)
    session = SimpleNamespace(id=uuid.uuid4(), session_token="TOKEN123", customer_id=None, consent_given_at=datetime.now(timezone.utc),
                              turn_count=0, site_id=None, page_url=None, last_seen_at=None, transcript=[])

    async def never(*a, **k):
        raise AssertionError("the AI must not answer before the number")

    monkeypatch.setattr(widget.web_reply, "generate_reply", never)
    config = _config(ask_contact="before_chat")
    out = asyncio.run(widget.widget_message("KEY", widget.MessageIn(session_token="TOKEN123", text="price?", consent=True,
                                                                    page_url="https://shop.in/a?x=1"),
                                            _widget_db(config, SimpleNamespace(id=BIZ, is_active=True), session)))
    assert out.needs_contact and "phone number" in out.reply_text
    assert session.page_url == "https://shop.in/a" and session.site_id == config.id


def test_the_contact_form_needs_the_notice_accepted_and_a_number(monkeypatch):
    widget = _no_plan_block(monkeypatch)
    db = _widget_db(_config(), SimpleNamespace(id=BIZ, is_active=True))
    with pytest.raises(HTTPException):
        asyncio.run(widget.widget_contact("KEY", widget.ContactOnlyIn(consent=False, contact=widget.ContactIn(phone="9876543210")), db))
    with pytest.raises(HTTPException):
        asyncio.run(widget.widget_contact("KEY", widget.ContactOnlyIn(consent=True, contact=widget.ContactIn(name="Ravi")), db))


def test_the_site_settings_are_cleaned_when_saved():
    from services.api.routers import web_chat

    site = SimpleNamespace()
    web_chat._look(site, web_chat.SiteIn(domain="shop.in", title="  ", accent="blue", position="top", starters=["a", "a", " b "],
                                         ask_contact="before_chat", language="fr", theme="light", whatsapp_button=True))
    assert site.title is None and site.accent is None and site.position is None and site.starters == ["a", "b"]
    assert site.ask_contact == "before_chat" and site.language is None and site.theme == "light" and site.whatsapp_button


def test_the_page_queries_compile():
    from services.api.routers import web_chat

    class Db:
        async def execute(self, stmt, *a, **k):
            stmt.compile(dialect=postgresql.dialect())
            return SimpleNamespace(one=lambda: (3, 1, 1), all=lambda: [])

    user = SimpleNamespace(business=BIZ)
    out = asyncio.run(web_chat.stats(user, Db()))
    assert (out.chats, out.with_number, out.needed_person) == (3, 1, 1)
    assert asyncio.run(web_chat.recent_chats(user, Db(), limit=30)) == []


def test_a_question_asked_again_with_a_number_is_not_stored_twice():
    msg = SimpleNamespace(id=uuid.uuid4(), content="talk to a person")

    class Db:
        async def execute(self, stmt, *a, **k):
            stmt.compile(dialect=postgresql.dialect())
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: msg))

    session = SimpleNamespace(customer_id=uuid.uuid4(), created_at=datetime.now(timezone.utc))
    assert asyncio.run(web_reply.earlier_question(Db(), session, "talk to a person ")) == msg.id
    assert asyncio.run(web_reply.earlier_question(Db(), session, "something else")) is None

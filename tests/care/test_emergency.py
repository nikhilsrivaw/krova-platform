"""An emergency in a message is told where to get help at once, on every channel, before the AI - healthcare only."""

import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared import verticals  # noqa: E402
from shared.care import emergency  # noqa: E402

HEALTH = ["health_clinic", "health_hospital", "health_lab", "health_therapy", "clinic"]


def _b(vertical="health_clinic", **settings):
    return SimpleNamespace(id=uuid.uuid4(), vertical=vertical, settings=settings, name="Sharma Clinic")


def test_only_healthcare_types_have_an_emergency_set():
    for key in HEALTH:
        assert verticals.get(key)["emergency"] == "medical" and emergency.phrases(_b(key))
    for key in ("general", "b2b", "commerce", "local_service", "programs_membership", "showroom_vehicle"):
        assert emergency.phrases(_b(key)) == () and emergency.detect(_b(key), "accident ho gaya") is None


@pytest.mark.parametrize("said", [
    "papa ko seene mein dard ho raha hai", "सांस नहीं आ रही", "मेरे पापा बेहोश हो गए", "I think he had an accident",
    "my mother can't breathe", "bahut khoon beh raha hai", "एक्सीडेंट हो गया है", "it's an emergency please",
    "dil ka daura pada lagta hai",
])
def test_an_emergency_is_heard_in_english_hinglish_and_devanagari(said):
    assert emergency.detect(_b(), said)


@pytest.mark.parametrize("said", [
    "khoon ki jaanch kitne ki hai", "emergency shift kab hai", "Is there an emergency doctor on Sunday?",
    "blood test ka rate", "Doctor kab baithte hain", "report kab milegi",
])
def test_an_ordinary_question_is_not_an_emergency(said):
    assert emergency.detect(_b(), said) is None


def test_the_line_is_in_their_words_with_the_businesss_own_number():
    assert emergency.reply(_b(), "my father can't breathe").startswith("This sounds like an emergency. Please call 112")
    assert emergency.reply(_b(), "papa behosh ho gaye").startswith("Ye emergency lag rahi hai. Abhi 112")
    assert emergency.reply(_b(), "सांस नहीं आ रही").startswith("यह इमरजेंसी")
    assert emergency.reply(_b(emergency_phone="98765 43210"), "chest pain").endswith("Our emergency desk: 98765 43210.")
    assert "98765" not in emergency.reply(_b(emergency_phone="call us"), "chest pain")
    assert emergency.clean_phone("+91 98765-43210") == "+91 98765-43210" and emergency.clean_phone("abc") is None


def test_whatsapp_sends_the_line_whatever_the_autonomy_and_alerts_the_team(monkeypatch):
    from services.workers import respond
    from shared.team import rules as team_rules

    sent, alerts, handed = [], [], []

    async def send_draft(draft, business_id, db, reviewed_by_user_id=None):
        sent.append(draft.body)

    async def notify(business_id, **kw):
        alerts.append(kw["reason"])

    async def hand(db, **kw):
        handed.append(kw["reason"])

    monkeypatch.setattr(respond, "send_draft", send_draft)
    monkeypatch.setattr(respond.agent_module, "notify_escalation", notify)
    monkeypatch.setattr(team_rules, "hand_to_person", hand)

    class Db:
        def add(self, obj):
            obj.id = uuid.uuid4()

        async def flush(self):
            return None

        def begin_nested(self):
            class N:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *a):
                    return False
            return N()

    business = _b()
    message = SimpleNamespace(id=uuid.uuid4(), business_id=business.id, customer_id=uuid.uuid4(), channel="whatsapp",
                              content="papa ko seene mein dard ho raha hai", occurred_at=datetime.now(timezone.utc))
    asyncio.run(respond._answer_emergency(Db(), message=message, business=business,
                                          customer=SimpleNamespace(id=message.customer_id), phrase="seene mein dard"))
    assert sent and sent[0].startswith("Ye emergency lag rahi hai")
    assert alerts == handed == ['EMERGENCY - customer said "seene mein dard"']


def test_a_call_speaks_the_line_then_tries_the_team(monkeypatch):
    from shared.channels.voice import pipeline as pl
    from shared.channels.voice.pipeline import CallPipeline, Turn

    business = _b()
    said, done = [], []

    async def speak(chunks):
        async for text in chunks:
            said.append(text)
            yield b"x"

    async def nothing(*a, **k):
        return None

    class Db:
        async def get(self, model, key):
            return business

    route = SimpleNamespace(business_id=business.id, staff_phone_number="919999900000", connection_id="c",
                            greeting="hi", keypad_menu=None, language="hi-IN", extra={})
    pipe = CallPipeline(route=route, caller_phone="+91 90000 00000", provider_call_id="call-1", send_audio=nothing,
                        send_clear=nothing, speak=speak, db=Db(), end_stream=nothing)
    pipe.turns.append(Turn(role="caller", text="मेरे पापा बेहोश हो गए"))

    async def targets(self):
        return ["member"]

    async def transfer(self, t):
        done.append(t)
        return True

    monkeypatch.setattr(pl.agent_module, "notify_escalation", nothing)
    monkeypatch.setattr(CallPipeline, "_mark_escalated", lambda self, reason: nothing())
    monkeypatch.setattr(CallPipeline, "_transfer_targets", targets)
    monkeypatch.setattr(CallPipeline, "_alert_first", lambda self, t: nothing())
    monkeypatch.setattr(CallPipeline, "_try_transfer", transfer)
    monkeypatch.setattr(CallPipeline, "_store_turn", lambda self, **k: nothing())
    monkeypatch.setattr(pl, "_PLAYBACK_MARGIN_SECONDS", 0)
    assert asyncio.run(pipe._answer_emergency()) is True
    assert said and said[0].startswith("यह इमरजेंसी") and done == [["member"]]

    pipe.turns.append(Turn(role="caller", text="Doctor kab baithte hain"))
    assert asyncio.run(pipe._answer_emergency()) is False


def test_website_chat_answers_an_anonymous_visitor_and_asks_for_a_number(monkeypatch):
    from shared.channels.web import reply as web_reply

    alerts = []

    async def notify(business_id, **kw):
        alerts.append(kw["customer_id"])

    monkeypatch.setattr(web_reply.agent_module, "notify_escalation", notify)
    session = SimpleNamespace(id=uuid.uuid4(), customer_id=None, transcript=[], needed_person_at=None)
    out = asyncio.run(web_reply.generate_reply(None, business=_b(), session=session, text="my father can't breathe"))
    assert out.reply_text.startswith("This sounds like an emergency") and out.needs_contact
    assert alerts == [None] and session.needed_person_at is not None
    assert [e["role"] for e in session.transcript] == ["user", "assistant"]

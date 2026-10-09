import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.ai.context import _carousel_line  # noqa: E402
from shared.channels import carousel_send  # noqa: E402
from shared.channels.whatsapp import carousel_media  # noqa: E402
from shared.db.models import TemplateStatus  # noqa: E402


def _template(*, body="Hi {{1}}, our offers", cards=("Only {{1}} rupees", "A plain card"), media=("m1", "m2")):
    return SimpleNamespace(
        status=TemplateStatus.approved, name="offers", language="en", body_text=body,
        components=[{
            "type": "CAROUSEL",
            "cards": [{"components": [{"type": "BODY", "text": text}]} for text in cards],
        }],
        extra={"carousel_media_ids": list(media)},
    )


# ── the slots, and cleaning what the AI supplies ────────────────────────────

def test_a_carousel_with_variables_is_described_but_not_sendable_without_values():
    template = _template()
    spec = carousel_media.whatsapp_carousel_spec(template)
    assert spec.needs_values
    assert spec.body_variables == ["1"]
    assert [c.variables for c in spec.cards] == [["1"], []]
    assert carousel_media.usable_whatsapp_carousel_media(template) is None
    assert 'intro "Hi {{1}}, our offers"' in carousel_media.describe_slots(spec)


def test_values_are_cleaned_into_single_bounded_lines():
    cleaned = carousel_media.clean_values({"body": ["  Rahul\nSharma  "], "cards": [["x" * 500], []]})
    assert cleaned["body"] == ["Rahul Sharma"]
    assert len(cleaned["cards"][0][0]) == carousel_media.MAX_VALUE_LENGTH
    assert cleaned["cards"][1] == []


@pytest.mark.parametrize("raw", [None, "text", {}, {"body": [], "cards": []}, {"body": "not a list"}])
def test_nothing_usable_cleans_to_none(raw):
    assert carousel_media.clean_values(raw) is None


def test_values_must_cover_every_slot_with_something():
    assert carousel_media.covers(["1", "2"], ["a", "b"])
    assert not carousel_media.covers(["1", "2"], ["a"])
    assert not carousel_media.covers(["1"], [""])
    assert not carousel_media.covers(["1"], None)
    assert carousel_media.covers([], ["ignored"])  # nothing to fill: extras are harmless


# ── Instagram ───────────────────────────────────────────────────────────────

IG = [
    {"title": "Hi {{1}}", "subtitle": "Your offer"},
    {"title": "A plain card"},
]


def test_instagram_placeholders_are_filled_from_the_values():
    filled = carousel_send.fill_instagram_elements(IG, {"cards": [["Rahul"], []]})
    assert filled[0]["title"] == "Hi Rahul" and filled[0]["subtitle"] == "Your offer"
    assert filled[1] == IG[1]


@pytest.mark.parametrize("values", [None, {}, {"cards": [[]]}, {"cards": [[""]]}, {"body": ["Rahul"]}])
def test_an_instagram_card_with_an_unfilled_placeholder_is_never_sent(values):
    assert carousel_send.fill_instagram_elements(IG, values) is None


def test_an_instagram_carousel_with_no_placeholders_needs_no_values():
    plain = [{"title": "One"}, {"title": "Two", "subtitle": "x"}]
    assert carousel_send.fill_instagram_elements(plain, None) == plain
    assert not carousel_media.instagram_carousel_needs_values(plain)
    assert carousel_media.instagram_carousel_needs_values(IG)


# ── WhatsApp: the parameters actually handed to Meta ────────────────────────

@pytest.fixture
def whatsapp(monkeypatch):
    sent = {}

    class FakeClient:
        def __init__(self, token, phone_number_id):
            pass

        async def send_template(self, phone, name, language, *, body_params=None, carousel_cards=None):
            sent.update(phone=phone, name=name, body_params=body_params, cards=carousel_cards)
            return SimpleNamespace(external_id="wamid")

    async def fake_identity(customer_id, kind, db):
        return "919876543210"

    async def fake_connection(business_id, channel, db):
        return SimpleNamespace(id=uuid.uuid4(), access_token="enc", external_account_id="123")

    async def fake_ingest(**kwargs):
        sent["ingested_text"] = kwargs["text"]

    monkeypatch.setattr(carousel_send, "WhatsAppClient", FakeClient)
    monkeypatch.setattr(carousel_send, "_identity", fake_identity)
    monkeypatch.setattr(carousel_send, "_connection", fake_connection)
    monkeypatch.setattr(carousel_send, "decrypt", lambda token: "plain")
    monkeypatch.setattr(carousel_send.ingest, "ingest", fake_ingest)
    return sent


def _send_whatsapp(template, values):
    return asyncio.run(
        carousel_send.send_whatsapp_carousel(uuid.uuid4(), uuid.uuid4(), template, db=None, values=values)
    )


def test_a_whatsapp_carousel_goes_out_with_the_values_in_the_right_places(whatsapp):
    went = _send_whatsapp(_template(), {"body": ["Rahul"], "cards": [["499"], []]})

    assert went is True
    assert whatsapp["body_params"] == ["Rahul"]
    assert [c.body_params for c in whatsapp["cards"]] == [["499"], []]
    assert [c.media_id for c in whatsapp["cards"]] == ["m1", "m2"]
    assert whatsapp["ingested_text"] == "Hi Rahul, our offers"  # the timeline shows what the customer read


@pytest.mark.parametrize(
    "values",
    [
        None,
        {"body": ["Rahul"], "cards": [[], []]},          # the card's price is missing
        {"body": [], "cards": [["499"], []]},            # the intro's name is missing
        {"body": ["Rahul"], "cards": [[""], []]},        # blank value
    ],
)
def test_a_whatsapp_carousel_with_any_blank_left_is_not_sent(whatsapp, values):
    assert _send_whatsapp(_template(), values) is False
    assert "phone" not in whatsapp  # nothing reached Meta


def test_a_carousel_with_no_variables_needs_no_values(whatsapp):
    plain = _template(body="Our offers", cards=("One", "Two"))
    assert _send_whatsapp(plain, None) is True
    assert whatsapp["body_params"] is None


# ── what the AI is shown ────────────────────────────────────────────────────

def test_the_ai_is_shown_what_to_fill_in_only_when_there_is_something():
    assert _carousel_line({"name": "a", "description": "d", "slots": ""}) == "- a: d"
    line = _carousel_line({"name": "a", "description": "d", "slots": 'intro "Hi {{1}}"'})
    assert "share_carousel_values" in line and 'intro "Hi {{1}}"' in line

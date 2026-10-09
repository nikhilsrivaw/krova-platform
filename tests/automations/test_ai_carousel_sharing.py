import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.channels import send_draft as send_draft_module  # noqa: E402
from shared.channels.whatsapp.carousel_media import usable_whatsapp_carousel_media  # noqa: E402
from shared.db.models import DraftStatus, IdentityKind, TemplateStatus  # noqa: E402


def _template(*, status=TemplateStatus.approved, body="Hello", cards=("Card one", "Card two"), media=("m1", "m2")):
    return SimpleNamespace(
        status=status, name="new_arrivals", language="en", body_text=body,
        components=[{
            "type": "CAROUSEL",
            "cards": [{"components": [{"type": "BODY", "text": text}]} for text in cards],
        }],
        extra={"carousel_media_ids": list(media)},
    )


def test_an_approved_carousel_with_an_image_per_card_is_sendable():
    assert usable_whatsapp_carousel_media(_template()) == ["m1", "m2"]


@pytest.mark.parametrize(
    "template",
    [
        _template(status=TemplateStatus.pending),
        _template(body="Hi {{1}}"),
        _template(cards=("Card for {{1}}", "Card two")),
        _template(media=("m1",)),  # an image missing for one card
        None,
    ],
)
def test_anything_that_would_need_filling_in_or_is_incomplete_is_not_offered(template):
    assert usable_whatsapp_carousel_media(template) is None


def test_a_plain_text_template_is_not_a_carousel():
    template = _template()
    template.components = [{"type": "BODY", "text": "Hello"}]
    assert usable_whatsapp_carousel_media(template) is None


def _draft(**extra):
    return SimpleNamespace(
        id=uuid.uuid4(), customer_id=uuid.uuid4(), channel="whatsapp", final_body="Here you go",
        extra=extra, status=DraftStatus.pending, reviewed_by_user_id=None, reviewed_at=None,
        sent_message_id=None,
    )


@pytest.fixture
def wired(monkeypatch):
    """send_draft with the channel send, ingest and carousel sender replaced by fakes."""
    shared = {"carousel_calls": [], "carousel_result": True}

    async def sender(draft, business_id, text, db):
        return SimpleNamespace(external_id="wamid"), SimpleNamespace(id=uuid.uuid4()), IdentityKind.phone, "919876543210"

    async def fake_ingest(**kwargs):
        return SimpleNamespace(message=SimpleNamespace(id=uuid.uuid4()))

    async def fake_share(**kwargs):
        shared["carousel_calls"].append(kwargs)
        if isinstance(shared["carousel_result"], Exception):
            raise shared["carousel_result"]
        return shared["carousel_result"]

    monkeypatch.setitem(send_draft_module._SENDERS, "whatsapp", sender)
    monkeypatch.setattr(send_draft_module.ingest, "ingest", fake_ingest)
    monkeypatch.setattr(send_draft_module.carousel_send, "share_named_carousel", fake_share)
    return shared


def _send(draft, **kwargs):
    return asyncio.run(
        send_draft_module.send_draft(draft, uuid.uuid4(), db=None, reviewed_by_user_id=None, **kwargs)
    )


def test_the_carousel_the_ai_chose_goes_out_with_the_approved_reply(wired):
    draft = _draft(share_carousel="new_arrivals")
    _send(draft)

    assert draft.status == DraftStatus.sent
    assert len(wired["carousel_calls"]) == 1
    call = wired["carousel_calls"][0]
    assert call["name"] == "new_arrivals" and call["channel"] == "whatsapp"
    assert draft.extra["carousel_sent"] is True


def test_a_person_can_approve_the_reply_without_the_carousel(wired):
    draft = _draft(share_carousel="new_arrivals")
    _send(draft, send_carousel=False)

    assert draft.status == DraftStatus.sent
    assert wired["carousel_calls"] == []


def test_a_draft_with_no_carousel_sends_none(wired):
    _send(_draft())
    assert wired["carousel_calls"] == []


def test_a_carousel_that_blows_up_never_undoes_the_reply_that_already_went(wired):
    wired["carousel_result"] = RuntimeError("graph api down")
    draft = _draft(share_carousel="new_arrivals")
    _send(draft)

    assert draft.status == DraftStatus.sent
    assert draft.extra["carousel_sent"] is False

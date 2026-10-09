import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi import HTTPException  # noqa: E402

from services.api.routers.post_call_rules import StepIn, _validate_step  # noqa: E402
from shared.care import post_call_actions  # noqa: E402
from shared.db.models import TemplateStatus  # noqa: E402


def _check(config: dict) -> None:
    _validate_step("message.received", StepIn(action_type="send_carousel", action_config=config), 0)


def test_a_complete_instagram_or_whatsapp_carousel_step_is_accepted():
    _check({"kind": "instagram", "carousel_id": str(uuid.uuid4())})
    _check({"kind": "whatsapp", "template_name": "new_arrivals"})


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"kind": "telegram", "carousel_id": "x"},
        {"kind": "instagram"},
        {"kind": "whatsapp"},
    ],
)
def test_an_incomplete_carousel_step_is_refused_when_saved(config):
    with pytest.raises(HTTPException) as err:
        _check(config)
    assert err.value.status_code == 422


class _FakeDb:
    """db.execute(...).scalars().first() -> the template, for the one lookup the action makes first."""

    def __init__(self, template):
        self._template = template

    async def execute(self, *_a, **_k):
        template = self._template
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: template))

    async def get(self, *_a, **_k):
        return None


def _template(body_text: str, card_body: str):
    return SimpleNamespace(
        status=TemplateStatus.approved, name="t", language="en", body_text=body_text,
        components=[{"type": "CAROUSEL", "cards": [{"components": [{"type": "BODY", "text": card_body}]}]}],
        extra={"carousel_media_ids": ["m1"]},
    )


def _send(db) -> bool:
    return asyncio.run(
        post_call_actions._send_whatsapp_carousel(
            uuid.uuid4(), uuid.uuid4(), {"kind": "whatsapp", "template_name": "t"}, db
        )
    )


def test_a_whatsapp_carousel_with_variables_is_skipped_not_sent_with_blanks():
    assert _send(_FakeDb(_template("Hi {{1}}", "A card"))) is False
    assert _send(_FakeDb(_template("Hi", "Card for {{1}}"))) is False


def test_a_missing_or_unapproved_template_is_skipped():
    assert _send(_FakeDb(None)) is False
    pending = _template("Hi", "A card")
    pending.status = TemplateStatus.pending
    assert _send(_FakeDb(pending)) is False


def test_an_instagram_carousel_with_a_bad_id_is_skipped():
    sent = asyncio.run(
        post_call_actions._send_instagram_carousel(
            uuid.uuid4(), uuid.uuid4(), {"kind": "instagram", "carousel_id": "not-a-uuid"}, _FakeDb(None)
        )
    )
    assert sent is False

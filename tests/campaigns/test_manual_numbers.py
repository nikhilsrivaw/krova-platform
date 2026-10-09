import asyncio
import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.campaigns import audience as audience_module  # noqa: E402
from shared.db.models import Audience  # noqa: E402


def test_every_audience_has_a_label_in_both_campaign_routers():
    """
    list_audiences() indexes the label dict by every Audience member, so a new
    audience with no label would 500 the whole campaigns page on load.
    """
    from services.api.routers import call_campaigns, campaigns

    for member in Audience:
        assert member in campaigns.AUDIENCE_LABELS, f"campaigns.py has no label for {member}"
        assert member in call_campaigns._AUDIENCE_LABELS, f"call_campaigns.py has no label for {member}"


def test_numbers_audience_is_accepted_by_the_whatsapp_campaign_request():
    from services.api.routers.campaigns import CampaignIn

    body = CampaignIn(
        name="Test", audience="numbers", audience_params={"numbers": ["9876543210"]},
        template_name="hello",
    )
    assert body.audience == "numbers"


def test_unusable_numbers_are_skipped_with_a_reason_and_never_touch_the_database():
    ids, new_numbers, skipped = asyncio.run(
        audience_module._customers_for_numbers(uuid.uuid4(), ["abc", "12", ""], db=None)
    )
    assert ids == [] and new_numbers == []
    assert [s["name"] for s in skipped] == ["abc", "12"]
    assert all(s["reason"] == "Not a valid phone number" for s in skipped)


def test_manual_list_is_capped():
    junk = [f"bad{i}" for i in range(audience_module.MAX_MANUAL_NUMBERS + 10)]
    _, _, skipped = asyncio.run(audience_module._customers_for_numbers(uuid.uuid4(), junk, db=None))
    assert len(skipped) == audience_module.MAX_MANUAL_NUMBERS


class _NoExistingCustomers:
    """A db whose phone-identity lookup finds nobody."""

    async def execute(self, *_args, **_kwargs):
        class _Result:
            def all(self):
                return []

        return _Result()


def test_a_preview_returns_unknown_numbers_without_creating_a_customer(monkeypatch):
    created: list[str] = []

    async def _fail_if_called(*args, **kwargs):
        created.append("customer")
        raise AssertionError("a preview must not create a customer")

    monkeypatch.setattr(audience_module.identity_resolver, "resolve", _fail_if_called)

    ids, new_numbers, skipped = asyncio.run(
        audience_module._customers_for_numbers(
            uuid.uuid4(), ["9876543210", "+91 98765 43210"], _NoExistingCustomers(), create_missing=False
        )
    )
    assert ids == []
    assert new_numbers == ["919876543210"]  # both spellings are one number
    assert skipped == [] and created == []

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi import HTTPException  # noqa: E402

from services.api.routers import capabilities as caps_router  # noqa: E402
from shared import verticals  # noqa: E402
from shared.verticals.capability_info import SWITCHABLE  # noqa: E402


def _business(vertical="local_service", settings=None):
    return SimpleNamespace(id=uuid.uuid4(), vertical=vertical, settings=settings)


# ── the helper ──────────────────────────────────────────────────────────────

def test_a_capability_the_type_lacks_can_be_switched_on_and_off_again():
    business = _business("b2b")  # b2b has quotations but not scheduling
    assert not verticals.has_capability(business, "scheduling")

    verticals.set_capability(business, "scheduling", True)
    assert verticals.has_capability(business, "scheduling")
    assert business.settings["capability_overrides"] == {"scheduling": True}

    verticals.set_capability(business, "scheduling", False)
    assert not verticals.has_capability(business, "scheduling")
    # Back to the type's own answer: no leftover override.
    assert "capability_overrides" not in business.settings


def test_a_capability_the_type_has_can_be_switched_off():
    business = _business("local_service")
    verticals.set_capability(business, "scheduling", False)
    assert not verticals.has_capability(business, "scheduling")
    assert business.settings["capability_overrides"] == {"scheduling": False}


def test_an_override_that_repeats_the_default_is_not_kept():
    business = _business("local_service")
    verticals.set_capability(business, "scheduling", True)  # it already has it
    assert "capability_overrides" not in (business.settings or {})


def test_other_settings_survive_and_the_settings_dict_is_replaced():
    original = {"queue": {"turn_near_threshold": 3}, "capability_overrides": {"quotations": True}}
    business = _business("local_service", original)
    verticals.set_capability(business, "scheduling", False)

    assert business.settings is not original  # reassigned, so the JSONB change is noticed
    assert business.settings["queue"] == {"turn_near_threshold": 3}
    assert business.settings["capability_overrides"] == {"quotations": True, "scheduling": False}


# ── the registry ────────────────────────────────────────────────────────────

def test_every_switchable_capability_is_one_a_template_really_declares():
    declared = set()
    for key in verticals.keys():
        declared |= set(verticals.get(key).get("capabilities", []))
    assert set(SWITCHABLE) <= declared


def test_the_always_on_and_the_dead_capability_are_not_offered():
    assert "conversation_intelligence" not in SWITCHABLE
    assert "voice_booking" not in SWITCHABLE


def test_every_entry_says_what_it_gives():
    for info in SWITCHABLE.values():
        assert info.label and info.description and info.adds, info.key


# ── the endpoints ───────────────────────────────────────────────────────────

class _FakeDb:
    def __init__(self, business):
        self.business = business
        self.committed = False

    async def get(self, _model, _id):
        return self.business

    async def commit(self):
        self.committed = True


def _user(role):
    return SimpleNamespace(id=uuid.uuid4(), role=role, business=uuid.uuid4())


def _put(key, enabled, role, business):
    db = _FakeDb(business)
    out = asyncio.run(
        caps_router.set_capability(key, caps_router.CapabilityIn(enabled=enabled), _user(role), db)
    )
    return out, db


def test_the_owner_can_switch_a_feature_and_it_is_saved():
    business = _business("b2b")
    out, db = _put("scheduling", True, "owner", business)
    assert out.enabled is True and out.default is False and out.overridden is True
    assert db.committed


@pytest.mark.parametrize("role", ["agent", None])
def test_an_agent_cannot_switch_features(role):
    business = _business("b2b")
    with pytest.raises(HTTPException) as err:
        _put("scheduling", True, role, business)
    assert err.value.status_code == 403
    assert business.settings is None  # nothing was written


def test_an_unknown_feature_is_a_404_not_a_silent_write():
    with pytest.raises(HTTPException) as err:
        _put("conversation_intelligence", False, "owner", _business())
    assert err.value.status_code == 404


def test_the_list_marks_what_the_business_changed_itself():
    business = _business("b2b", {"capability_overrides": {"scheduling": True}})
    db = _FakeDb(business)
    rows = {r.key: r for r in asyncio.run(caps_router.list_capabilities(_user("agent"), db))}

    assert rows["quotations"].enabled and rows["quotations"].default and not rows["quotations"].overridden
    assert rows["scheduling"].enabled and not rows["scheduling"].default and rows["scheduling"].overridden
    assert not rows["opd_queue"].enabled


def test_the_queue_settings_endpoint_now_refuses_an_agent():
    from services.api.routers import queue as queue_router

    body = queue_router.QueueSettingsIn(enabled=True)
    with pytest.raises(HTTPException) as err:
        asyncio.run(queue_router.update_queue_settings(body, _user("agent"), _FakeDb(_business())))
    assert err.value.status_code == 403


def test_the_queue_toggle_still_works_for_the_owner_and_keeps_its_other_settings():
    from services.api.routers import queue as queue_router

    business = _business("b2b", {"queue": {"turn_near_threshold": 5}})
    body = queue_router.QueueSettingsIn(enabled=True, turn_near_threshold=7)
    out = asyncio.run(queue_router.update_queue_settings(body, _user("owner"), _FakeDb(business)))

    assert out.enabled is True and out.turn_near_threshold == 7
    assert business.settings["capability_overrides"] == {"opd_queue": True}
    assert business.settings["queue"]["turn_near_threshold"] == 7


def test_no_capability_a_template_declares_is_missing_from_the_features_list():
    """
    A capability added to a vertical template but not to the registry would
    simply never appear on the Features screen - nothing would fail, an owner
    just could not switch it. conversation_intelligence is the one that is
    deliberately not switchable.
    """
    declared = set()
    for key in verticals.keys():
        declared |= set(verticals.get(key).get("capabilities", []))
    assert declared - {"conversation_intelligence"} <= set(SWITCHABLE), (
        declared - {"conversation_intelligence"} - set(SWITCHABLE)
    )


def test_the_type_picker_says_what_each_type_includes():
    by_key = {v["key"]: v for v in verticals.available()}

    assert by_key["b2b"]["features"] == ["Quotations", "Product feedback loop"]
    assert "Walk-in queue" in by_key["local_service"]["features"]
    assert "Appointments & staff calendar" in by_key["programs"]["features"]
    assert "Orders & products" in by_key["commerce"]["features"]
    assert all(isinstance(v["features"], list) for v in by_key.values())

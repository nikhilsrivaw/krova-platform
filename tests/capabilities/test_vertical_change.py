import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi import HTTPException  # noqa: E402

from services.api.routers import auth as auth_router  # noqa: E402
from shared import verticals  # noqa: E402
from shared.db.models import Business, BusinessDNA, User  # noqa: E402


def _business(vertical="local_service", settings=None):
    return SimpleNamespace(
        id=uuid.uuid4(), name="Acme", vertical=vertical, settings=settings, autonomy="draft",
    )


def _dna(source="template", vertical="local_service"):
    seed = verticals.seed_dna(vertical)
    return SimpleNamespace(
        summary=seed["summary"], tone=seed["tone"], policies=seed["policies"],
        known_gaps={"from_template": seed["known_gaps"]["from_template"], "learned": ["asked about parking"]},
        pricing_notes="Haircut 300", offerings={"cut": 300}, opening_hours={"mon": "9-6"}, source=source,
    )


def test_changing_type_refreshes_the_ai_rules_from_the_new_template():
    business, dna = _business("local_service"), _dna()
    assert verticals.apply_vertical_change(business, dna, "b2b") is True

    b2b = verticals.get("b2b")
    assert business.vertical == "b2b"
    assert dna.summary == b2b["summary"] and dna.tone == b2b["tone"] and dna.policies == b2b["policies"]
    assert dna.known_gaps["from_template"] == b2b["known_gaps"]


def test_what_the_business_and_the_system_added_is_kept():
    business, dna = _business("local_service"), _dna()
    verticals.apply_vertical_change(business, dna, "b2b")

    assert dna.known_gaps["learned"] == ["asked about parking"]
    assert dna.pricing_notes == "Haircut 300"
    assert dna.offerings == {"cut": 300} and dna.opening_hours == {"mon": "9-6"}


def test_a_dna_a_person_has_edited_is_not_overwritten():
    business, dna = _business("local_service"), _dna(source="edited")
    before = (dna.summary, dna.tone, dna.policies)
    verticals.apply_vertical_change(business, dna, "b2b")

    assert business.vertical == "b2b"  # the type still moves
    assert (dna.summary, dna.tone, dna.policies) == before


def test_choosing_the_same_type_changes_nothing():
    business, dna = _business("b2b"), _dna(vertical="b2b")
    before = dna.summary
    assert verticals.apply_vertical_change(business, dna, "b2b") is False
    assert dna.summary == before


def test_an_unknown_type_is_refused_before_anything_changes():
    business, dna = _business("local_service"), _dna()
    with pytest.raises(verticals.UnknownVertical):
        verticals.apply_vertical_change(business, dna, "no_such_type")
    assert business.vertical == "local_service"


def test_overrides_that_the_new_type_already_gives_are_dropped_and_real_ones_kept():
    # b2b + scheduling switched on by the owner, and queue switched off while on.
    business = _business("b2b", {"capability_overrides": {"scheduling": True, "quotations": False}})
    verticals.apply_vertical_change(business, _dna(vertical="b2b"), "local_service")

    # local_service already has scheduling, so that override is redundant now;
    # quotations is off and local_service does not have it - also redundant.
    assert "capability_overrides" not in business.settings


def test_an_override_that_still_differs_from_the_new_default_survives():
    business = _business("b2b", {"capability_overrides": {"quotations": True}})
    verticals.apply_vertical_change(business, _dna(vertical="b2b"), "local_service")
    assert business.settings["capability_overrides"] == {"quotations": True}


# ── the endpoint ────────────────────────────────────────────────────────────

class _Db:
    def __init__(self, business, dna, user):
        self._rows = {Business: business, BusinessDNA: dna, User: user}
        self.committed = False

    async def get(self, model, _id):
        return self._rows[model]

    async def commit(self):
        self.committed = True


def _call(body, role, business, dna=None):
    user = SimpleNamespace(full_name="Asha", phone=None, phone_verified_at=None)
    caller = SimpleNamespace(id=uuid.uuid4(), role=role, email="a@b.c", business_id=business.id)
    db = _Db(business, dna, user)
    out = asyncio.run(auth_router.update_me(auth_router.UpdateMeRequest(**body), caller, db))
    return out, db, user


def test_an_agent_cannot_change_the_business_type_or_name():
    business = _business("local_service")
    for body in ({"vertical": "b2b"}, {"business_name": "New"}, {"google_review_url": "https://x"}):
        with pytest.raises(HTTPException) as err:
            _call(body, "agent", business, _dna())
        assert err.value.status_code == 403
    assert business.vertical == "local_service" and business.name == "Acme"


def test_an_agent_can_still_change_their_own_name():
    business = _business()
    out, db, user = _call({"full_name": "Ravi"}, "agent", business, _dna())
    assert user.full_name == "Ravi" and db.committed


def test_the_owner_changing_the_type_refreshes_the_ai_and_the_menu():
    business, dna = _business("local_service"), _dna()
    out, db, _ = _call({"vertical": "b2b"}, "owner", business, dna)

    assert out.vertical == "b2b"
    assert "quotations" in out.capabilities and "scheduling" not in out.capabilities
    assert dna.policies == verticals.get("b2b")["policies"]


# ── the AI's autonomy is the owner's call ───────────────────────────────────

def _approvals_call(fn, body, role, business):
    from services.api.routers import approvals as approvals_router  # noqa: F401

    caller = SimpleNamespace(id=uuid.uuid4(), role=role, business=business.id)
    return asyncio.run(fn(body, caller, _Db(business, None, None)))


@pytest.mark.parametrize("role", ["agent", None])
def test_an_agent_cannot_put_the_ai_into_act_mode(role):
    from services.api.routers import approvals as approvals_router

    business = _business()
    with pytest.raises(HTTPException) as err:
        _approvals_call(approvals_router.set_autonomy, approvals_router.AutonomyBody(autonomy="act"), role, business)
    assert err.value.status_code == 403
    assert business.autonomy == "draft"  # untouched


def test_an_agent_cannot_loosen_the_auto_send_rules():
    from services.api.routers import approvals as approvals_router

    business = _business()
    with pytest.raises(HTTPException) as err:
        _approvals_call(
            approvals_router.update_auto_send_rules,
            approvals_router.AutoSendRulesIn(enabled=True),
            "agent", business,
        )
    assert err.value.status_code == 403
    assert business.settings is None


def test_the_owner_can_still_set_the_autonomy_level():
    from services.api.routers import approvals as approvals_router

    business = _business()
    out = _approvals_call(approvals_router.set_autonomy, approvals_router.AutonomyBody(autonomy="act"), "owner", business)
    assert out["autonomy"] == "act" and business.autonomy == "act"


def test_an_agent_re_sending_the_unchanged_business_form_can_still_fix_their_own_name():
    # Settings' Save posts every field, business ones included.
    business = _business("local_service", {"google_review_url": "https://g.page/r/abc"})
    body = {
        "full_name": "Ravi", "business_name": "Acme", "vertical": "local_service",
        "google_review_url": "https://g.page/r/abc",
    }
    out, db, user = _call(body, "agent", business, _dna())
    assert user.full_name == "Ravi" and db.committed
    assert business.vertical == "local_service"

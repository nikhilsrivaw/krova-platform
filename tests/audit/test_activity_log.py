import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from services.api.audit_middleware import ActivityLogMiddleware  # noqa: E402
from services.api.routers import team_activity  # noqa: E402
from shared.audit import activity  # noqa: E402
from tests.capabilities.test_owner_only_routes import LOCKED  # noqa: E402

API = activity.API


def _real_routes():
    from services.api.main import app

    return {(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}


# ── the labels ──────────────────────────────────────────────────────────────

def test_every_owner_only_route_has_its_own_readable_label():
    """An owner reading the feed should never meet 'POST /campaigns/{campaign_id}/send'."""
    unlabelled = {key for key in LOCKED if key not in activity.LABELS}
    assert not unlabelled, f"no label for: {sorted(unlabelled)}"


def test_labels_point_at_routes_that_exist():
    stale = {key for key in activity.LABELS if key not in _real_routes()}
    assert not stale, f"labels for routes that do not exist: {sorted(stale)}"


def test_the_staff_day_to_day_actions_are_labelled_as_work():
    for key in [
        ("POST", API + "/messages/text"), ("POST", API + "/approvals/{draft_id}/approve"),
        ("POST", API + "/conversations/{customer_id}/assign"),
    ]:
        assert activity.describe(*key)[2] == activity.WORK


def test_money_and_access_actions_are_labelled_security_or_config():
    assert activity.describe("DELETE", API + "/integrations/api-keys/{key_id}")[2] == activity.SECURITY
    assert activity.describe("GET", API + "/export/customers")[2] == activity.SECURITY
    assert activity.describe("POST", API + "/voice-onboarding/numbers/buy")[2] == activity.CONFIG


def test_a_route_nobody_labelled_still_gets_a_readable_row_not_none():
    action, summary, kind = activity.describe("POST", API + "/crm/customers/{customer_id}/tags")
    assert action == "customer_record_changed" and "customer" in summary.lower() and kind == activity.WORK
    action, summary, _ = activity.describe("DELETE", API + "/something-new/{id}")
    assert action and summary  # falls back to the route itself, never raises


def test_every_mutating_route_in_the_app_gets_a_description():
    for method, path in _real_routes():
        if method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith(API):
            action, summary, kind = activity.describe(method, path)
            assert action and summary and kind in (activity.WORK, activity.CONFIG, activity.SECURITY)


def test_actions_group_into_kinds_for_the_feed_filters():
    assert "message_sent" in activity.actions_of_kind(activity.WORK)
    assert "campaign_sent" in activity.actions_of_kind(activity.CONFIG)
    assert {"data_export", "api_key_created", "login"} <= set(activity.actions_of_kind(activity.SECURITY))
    assert activity.kind_of("login") == activity.SECURITY
    assert activity.kind_of("a_future_unknown_action") == activity.CONFIG  # when unsure, show it


# ── what is and is not recorded ─────────────────────────────────────────────

def test_only_changes_and_data_leaving_are_logged():
    assert activity.should_log("POST", API + "/messages/text")
    assert activity.should_log("DELETE", API + "/templates/{template_id}")
    assert activity.should_log("GET", API + "/export/customers")
    assert activity.should_log("GET", API + "/ledger/export/tally")
    assert not activity.should_log("GET", API + "/conversations")          # an ordinary read
    assert not activity.should_log("GET", API + "/analytics/overview")
    assert not activity.should_log("POST", API + "/campaigns/preview")     # a question, not a change
    assert not activity.should_log("POST", API + "/post-call-rules/test")
    assert not activity.should_log("POST", API + "/owner/ask")
    assert not activity.should_log("POST", API + "/push/subscribe")
    assert not activity.should_log("POST", API + "/auth/login")            # sign-ins are recorded by hand
    assert activity.should_log("POST", API + "/auth/me")                   # but a profile change is a change


@pytest.mark.parametrize(
    "status,expected",
    [(200, "ok"), (201, "ok"), (204, "ok"), (302, "ok"), (403, "denied"), (400, "failed"), (409, "failed"),
     (500, "failed"), (401, None), (404, None), (405, None), (422, None), (429, None), (None, None)],
)
def test_outcome_from_status(status, expected):
    assert activity.outcome_for(status) == expected


def test_a_phone_number_is_masked_before_it_is_written():
    assert activity.mask_phone("919876543210") == "****3210"
    assert activity.mask_phone("+91 98765 43210") == "****3210"
    assert activity.mask_phone(None) is None
    assert "9876" not in activity.mask_phone("919876543210")


def test_note_outside_a_request_does_nothing():
    activity._meta.set(None)
    activity.note(recipients=3)  # must not raise


# ── the middleware, on a small app ──────────────────────────────────────────

ACTOR_BIZ = uuid.uuid4()
ACTOR_USER = uuid.uuid4()


def _app(captured: list, *, fail_writes: bool = False):
    async def fake_write(**kwargs):
        if fail_writes:
            raise RuntimeError("database is down")
        captured.append(kwargs)

    activity.write = fake_write  # the middleware calls activity.write

    app = FastAPI()
    app.add_middleware(ActivityLogMiddleware)

    def as_member(request: Request):
        request.state.actor = {"user_id": ACTOR_USER, "business_id": ACTOR_BIZ, "role": "agent", "label": "Ravi"}

    def refuse(request: Request):
        as_member(request)
        raise HTTPException(403, "Only the owner or an admin can do this.")

    @app.post(API + "/messages/text", dependencies=[Depends(as_member)])
    async def send(body: dict):
        activity.note(to=activity.mask_phone(body["to"]))
        return {"sent": True}

    @app.post(API + "/campaigns/{campaign_id}/send", dependencies=[Depends(refuse)])
    async def campaign_send(campaign_id: str):
        return {}

    @app.get(API + "/export/customers", dependencies=[Depends(as_member)])
    async def export():
        return {"rows": 1}

    @app.get(API + "/conversations", dependencies=[Depends(as_member)])
    async def conversations():
        return []

    @app.post(API + "/campaigns/preview", dependencies=[Depends(as_member)])
    async def preview():
        return {}

    @app.post(API + "/approvals/{draft_id}/approve")  # no actor: not signed in
    async def anonymous(draft_id: str):
        return {}

    @app.post(API + "/leads/manual", dependencies=[Depends(as_member)])
    async def lead():
        raise HTTPException(422, "bad input")

    @app.post(API + "/knowledge", dependencies=[Depends(as_member)])
    async def boom():
        raise HTTPException(400, "nope")

    return app


@pytest.fixture
def written(monkeypatch):
    rows: list = []
    original = activity.write
    client = TestClient(_app(rows), raise_server_exceptions=False)
    yield client, rows
    activity.write = original


def test_a_team_members_action_is_recorded_with_who_what_and_how_it_went(written):
    client, rows = written
    response = client.post(
        API + "/messages/text", json={"to": "919876543210", "body": "your invoice is attached"},
        headers={"user-agent": "TestBrowser/1.0", "x-forwarded-for": "203.0.113.9, 10.0.0.1"},
    )
    assert response.status_code == 200 and len(rows) == 1
    row = rows[0]
    assert row["user_label"] == "Ravi" and row["role"] == "agent" and row["business_id"] == ACTOR_BIZ
    assert row["action"] == "message_sent" and row["summary"] == "Sent a WhatsApp message"
    assert row["outcome"] == "ok" and row["status_code"] == 200
    assert row["ip"] == "203.0.113.9" and row["user_agent"] == "TestBrowser/1.0"
    assert row["detail"] == {"to": "****3210"}


def test_the_message_text_is_never_stored(written):
    client, rows = written
    client.post(API + "/messages/text", json={"to": "919876543210", "body": "SECRET-PAYMENT-DETAILS"})
    assert "SECRET-PAYMENT-DETAILS" not in repr(rows)
    assert "919876543210" not in repr(rows)


def test_a_refused_attempt_is_recorded_as_denied(written):
    client, rows = written
    response = client.post(API + "/campaigns/abc123/send")
    assert response.status_code == 403
    assert len(rows) == 1
    assert rows[0]["outcome"] == "denied" and rows[0]["action"] == "campaign_sent"
    assert rows[0]["summary"].startswith("Tried to: sent a WhatsApp campaign")
    assert rows[0]["detail"] == {"campaign_id": "abc123"}  # ids from the URL


def test_downloading_data_is_recorded_even_though_it_is_a_read(written):
    client, rows = written
    client.get(API + "/export/customers")
    assert [r["action"] for r in rows] == ["data_export"]


def test_ordinary_reads_questions_and_anonymous_calls_are_not_recorded(written):
    client, rows = written
    client.get(API + "/conversations")
    client.post(API + "/campaigns/preview")
    client.post(API + "/approvals/xyz/approve")
    assert rows == []


def test_nothing_happened_responses_are_not_recorded_but_failures_are(written):
    client, rows = written
    client.post(API + "/leads/manual")        # 422 - nothing happened
    client.post(API + "/not-a-route")          # 404
    assert rows == []
    client.post(API + "/knowledge")            # 400 - an attempt that failed
    assert [(r["action"], r["outcome"]) for r in rows] == [("knowledge_changed", "failed")]


def test_a_logging_failure_never_breaks_the_request():
    rows: list = []
    original = activity.write
    try:
        client = TestClient(_app(rows, fail_writes=True), raise_server_exceptions=False)
        response = client.post(API + "/messages/text", json={"to": "919876543210"})
        assert response.status_code == 200 and response.json() == {"sent": True}
    finally:
        activity.write = original


# ── sign-ins ────────────────────────────────────────────────────────────────

def test_a_sign_in_is_added_to_the_session_that_made_it():
    added = []
    db = SimpleNamespace(add=added.append)
    user = SimpleNamespace(id=uuid.uuid4(), full_name="Asha", email="a@b.c", phone=None)
    business = SimpleNamespace(id=uuid.uuid4())
    activity.begin_request("198.51.100.4", "Phone/2.0")

    activity.add_login(db, user=user, business=business, role="agent")

    assert len(added) == 1
    row = added[0]
    assert row.action == "login" and row.user_label == "Asha" and row.role == "agent"
    assert row.ip == "198.51.100.4" and row.user_agent == "Phone/2.0"
    assert row.business_id == business.id and row.outcome == "ok"


def test_a_sign_in_without_a_business_is_skipped_quietly():
    added = []
    activity.add_login(SimpleNamespace(add=added.append), user=SimpleNamespace(id=uuid.uuid4()), business=None, role=None)
    assert added == []


def test_a_token_refresh_is_not_recorded_as_a_sign_in():
    import inspect

    from shared.auth import service

    source = inspect.getsource(service.refresh_session)
    assert "record_login=False" in source
    assert "record_login=False" in inspect.getsource(service.login_via_google)


# ── the owner's summary ─────────────────────────────────────────────────────

def _t(minutes_ago):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def test_the_summary_counts_per_person_and_ignores_people_who_have_left():
    ravi, asha, gone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    people = {ravi: ("Ravi", "agent"), asha: ("Asha", "admin")}
    rows = [
        (ravi, "message_sent", "ok", 12, _t(5)),
        (ravi, "draft_approved", "ok", 3, _t(20)),
        (ravi, "data_export", "denied", 1, _t(2)),           # tried and was refused
        (ravi, "campaign_sent", "denied", 1, _t(3)),
        (asha, "campaign_sent", "ok", 2, _t(60)),
        (asha, "template_created", "ok", 1, _t(90)),
        (asha, "login", "ok", 4, _t(120)),
        (gone, "message_sent", "ok", 99, _t(1)),             # no longer on the team
    ]
    out = {m.name: m for m in team_activity.summarise(rows, people)}

    assert out["Ravi"].messages_sent == 12 and out["Ravi"].drafts_approved == 3
    assert out["Ravi"].refused_attempts == 2 and out["Ravi"].data_exports == 0  # a refused export is not an export
    assert out["Ravi"].config_changes == 0
    assert out["Asha"].config_changes == 3 and out["Asha"].sign_ins == 4
    assert "gone" not in {m.name for m in out.values()} and len(out) == 2


def test_someone_with_no_activity_still_appears_with_zeros():
    quiet = uuid.uuid4()
    out = team_activity.summarise([], {quiet: ("Meera", "agent")})
    assert len(out) == 1 and out[0].last_active_at is None and out[0].messages_sent == 0


def test_the_most_recently_active_person_is_listed_first():
    a, b = uuid.uuid4(), uuid.uuid4()
    people = {a: ("Old", "agent"), b: ("New", "agent")}
    rows = [(a, "message_sent", "ok", 1, _t(500)), (b, "message_sent", "ok", 1, _t(5))]
    assert [m.name for m in team_activity.summarise(rows, people)] == ["New", "Old"]


def test_only_the_owner_or_an_admin_can_read_the_log():
    from services.api.dependencies import require_owner_or_admin
    from services.api.main import app

    for path in (API + "/team/activity", API + "/team/activity/summary"):
        route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == path)
        assert require_owner_or_admin in {d.call for d in route.dependant.dependencies}, path

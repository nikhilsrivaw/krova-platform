import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi import HTTPException  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from services.api.dependencies import require_owner_or_admin  # noqa: E402


def _caller(role):
    return SimpleNamespace(id=uuid.uuid4(), role=role, business_id=uuid.uuid4())


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_pass(role):
    caller = _caller(role)
    assert asyncio.run(require_owner_or_admin(caller)) is caller


@pytest.mark.parametrize("role", ["agent", None, "viewer"])
def test_everyone_else_is_refused(role):
    with pytest.raises(HTTPException) as err:
        asyncio.run(require_owner_or_admin(_caller(role)))
    assert err.value.status_code == 403


# The rule: a team member working the inbox does the day-to-day for ONE
# customer at a time. The owner or an admin does whatever
#   - reaches many customers at once      (campaigns, bulk lead import),
#   - keeps acting on its own from now on (automations, flows, forms, scripts),
#   - spends money or a Meta/Plivo account (templates, numbers, KYC),
#   - or changes the business's channels, credentials and data access
#     (API keys, webhooks, connections, exports, WhatsApp profile and PIN).
# This pins both lists so a route added or edited later cannot quietly drop
# a guard, or have one added where staff genuinely need to act.
_P = "/api/v1"
LOCKED = {(m, _P + path) for m, path in [
    # data access and credentials
    ("POST", "/integrations/api-keys"), ("DELETE", "/integrations/api-keys/{key_id}"),
    ("POST", "/integrations/webhooks"), ("PATCH", "/integrations/webhooks/{webhook_id}"),
    ("DELETE", "/integrations/webhooks/{webhook_id}"),
    ("POST", "/integrations/github"), ("DELETE", "/integrations/github"),
    ("POST", "/integrations/email-connection"), ("DELETE", "/integrations/email-connection"),
    ("POST", "/integrations/stripe"), ("DELETE", "/integrations/stripe"),
    ("POST", "/integrations/google-calendar/disconnect"), ("GET", "/integrations/google-calendar/connect-url"),
    ("GET", "/export/customers"), ("GET", "/export/conversations"),
    # many customers at once
    ("POST", "/campaigns"), ("POST", "/campaigns/{campaign_id}/steps"),
    ("DELETE", "/campaigns/{campaign_id}/steps/{step_id}"), ("POST", "/campaigns/{campaign_id}/send"),
    ("POST", "/call-campaigns"), ("POST", "/call-campaigns/{campaign_id}/send"),
    ("POST", "/leads/import"),
    # acts on its own from now on
    ("POST", "/post-call-rules"), ("PATCH", "/post-call-rules/{rule_id}"), ("DELETE", "/post-call-rules/{rule_id}"),
    ("POST", "/call-scripts"), ("PATCH", "/call-scripts/{script_id}"), ("DELETE", "/call-scripts/{script_id}"),
    ("POST", "/flows"), ("POST", "/flows/{flow_id}/publish"), ("POST", "/flows/{flow_id}/deprecate"),
    ("POST", "/flows/{flow_id}/refresh"), ("POST", "/flows/{flow_id}/enable-live-data"),
    ("POST", "/forms"), ("PATCH", "/forms/{form_id}"), ("DELETE", "/forms/{form_id}"),
    ("POST", "/forms/{form_id}/logo"),
    # money, Meta and Plivo
    ("POST", "/templates"), ("DELETE", "/templates/{template_id}"),
    ("POST", "/voice-onboarding/subaccount"), ("POST", "/voice-onboarding/compliance/end-user"),
    ("POST", "/voice-onboarding/compliance/documents"), ("POST", "/voice-onboarding/compliance/application"),
    ("POST", "/voice-onboarding/compliance/resubmit"), ("POST", "/voice-onboarding/numbers/buy"),
    ("POST", "/voice-onboarding/numbers/{number}/release"), ("PATCH", "/voice-onboarding/agent-settings"),
    ("POST", "/voice-onboarding/number-requests"), ("PATCH", "/voice-onboarding/number-requests/{request_id}"),
    # the business's channels and identity
    ("POST", "/channels/whatsapp/embedded-signup"), ("POST", "/channels/whatsapp/ad-tracking"),
    ("POST", "/channels/whatsapp/catalog-id"), ("POST", "/channels/whatsapp/payment-config"),
    ("DELETE", "/channels/whatsapp"), ("POST", "/channels/gmail/backfill"),
    ("POST", "/account/whatsapp/profile"), ("POST", "/account/whatsapp/profile/picture"),
    ("POST", "/account/whatsapp/request-code"), ("POST", "/account/whatsapp/verify-code"),
    ("POST", "/account/whatsapp/two-step-pin"),
    ("POST", "/migration/whatsapp/start"), ("POST", "/migration/whatsapp/request-code"),
    ("POST", "/migration/whatsapp/verify-code"), ("POST", "/migration/whatsapp/finish"),
    ("POST", "/justdial/token"), ("POST", "/indiamart/token"), ("POST", "/lead-sources/{key}/token"),
    ("POST", "/email-leads/token"), ("POST", "/zoho/sync"), ("DELETE", "/zoho/connection"),
    # who is on the team (the finer owner-vs-admin rules are in shared/team/members.py)
    ("PUT", "/team/settings"), ("POST", "/team/transfer-ownership"), ("POST", "/team/members"), ("PATCH", "/team/members/{user_id}"),
    ("DELETE", "/team/members/{user_id}"), ("POST", "/team/members/{user_id}/reset-password"),
]}

# What staff may not READ either: business-wide numbers and money, the voice KYC
# file, and the integration screens (webhook URLs and keys often carry secrets).
LOCKED_READS = {(m, _P + path) for m, path in [
    ("GET", "/analytics/overview"), ("GET", "/analytics/team"), ("GET", "/analytics/agent"),
    ("GET", "/analytics/kept"), ("GET", "/analytics/channels"), ("GET", "/analytics/response-speed"),
    ("GET", "/analytics/receivables"), ("GET", "/analytics/trust-report"),
    ("GET", "/ledger/export/tally"),
    ("GET", "/migration/whatsapp/readiness"),
    ("GET", "/voice-onboarding/compliance/state"), ("GET", "/voice-onboarding/compliance/requirements"),
    ("GET", "/voice-onboarding/compliance/status"), ("GET", "/voice-onboarding/numbers/owned"),
    ("GET", "/voice-onboarding/numbers/search"), ("GET", "/voice-onboarding/number-requests"),
    ("GET", "/voice-onboarding/number-requests/all"),
    ("GET", "/integrations/google-calendar"), ("GET", "/integrations/github"),
    ("GET", "/integrations/email-connection"), ("GET", "/integrations/stripe"),
    ("GET", "/integrations/webhooks"), ("GET", "/integrations/api-keys"),
    # the owner's record of what each person did
    ("GET", "/team/activity"), ("GET", "/team/activity/summary"),
]}

# What staff do all day - one customer at a time - must stay open to them.
OPEN_TO_STAFF = {(m, _P + path) for m, path in [
    ("POST", "/messages/text"), ("POST", "/messages/template"), ("POST", "/messages/interactive-buttons"),
    ("POST", "/messages/instagram/text"), ("POST", "/messages/instagram/carousel"),
    ("POST", "/messages/instagram/carousels/{carousel_id}/send"),
    ("POST", "/approvals/{draft_id}/approve"), ("POST", "/approvals/{draft_id}/reject"),
    ("POST", "/flows/{flow_id}/send"),
    ("POST", "/campaigns/preview"), ("POST", "/call-campaigns/preview"),
    ("POST", "/post-call-rules/test"),
    ("POST", "/leads/manual"),
    ("POST", "/templates/sync"),
    ("POST", "/knowledge"),
    ("POST", "/queue/check-in"),
    ("GET", "/conversations"), ("GET", "/ledger/commitments"), ("GET", "/team"),
    ("GET", "/escalations"), ("GET", "/voice-onboarding/logs"),
    # working a shared inbox as a team
    ("POST", "/conversations/{customer_id}/take-over"), ("POST", "/conversations/{customer_id}/presence"),
    ("POST", "/escalations/{escalation_id}/claim"), ("POST", "/escalations/{escalation_id}/release"),
    ("POST", "/auth/change-password"),
    ("GET", "/team/settings"), ("GET", "/team/my-work"), ("PUT", "/team/me/availability"),
]}


def _guarded_routes():
    from services.api.main import app

    found = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = {d.call for d in route.dependant.dependencies}
        if require_owner_or_admin in calls:
            found |= {(method, route.path) for method in route.methods}
    return found


def test_every_sensitive_route_requires_the_owner_or_an_admin():
    missing = (LOCKED | LOCKED_READS) - _guarded_routes()
    assert not missing, f"no owner/admin guard on: {sorted(missing)}"


def test_staff_can_still_do_their_day_to_day_work():
    from services.api.main import app

    open_now = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and require_owner_or_admin not in {d.call for d in route.dependant.dependencies}:
            open_now |= {(method, route.path) for method in route.methods}

    # A path that does not exist (renamed) is a test to update, not a pass.
    existing = {(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert OPEN_TO_STAFF <= existing, f"no such route: {sorted(OPEN_TO_STAFF - existing)}"
    assert OPEN_TO_STAFF <= open_now, f"wrongly locked: {sorted(OPEN_TO_STAFF - open_now)}"


def test_meta_callbacks_are_not_mistaken_for_staff_actions():
    # Meta calls this with a signed request, not a user session - a role guard on it would break it.
    from services.api.main import app

    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == "/api/v1/channels/instagram/deauthorize")
    assert require_owner_or_admin not in {d.call for d in route.dependant.dependencies}

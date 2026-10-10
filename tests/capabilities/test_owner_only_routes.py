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


# Handing out a key, pointing a webhook at a URL, connecting an account with
# credentials and downloading everything are owner/admin matters. This pins
# the list so a route added or edited later cannot quietly drop the guard.
LOCKED = {
    ("POST", "/api/v1/integrations/api-keys"),
    ("DELETE", "/api/v1/integrations/api-keys/{key_id}"),
    ("POST", "/api/v1/integrations/webhooks"),
    ("PATCH", "/api/v1/integrations/webhooks/{webhook_id}"),
    ("DELETE", "/api/v1/integrations/webhooks/{webhook_id}"),
    ("POST", "/api/v1/integrations/github"),
    ("DELETE", "/api/v1/integrations/github"),
    ("POST", "/api/v1/integrations/email-connection"),
    ("DELETE", "/api/v1/integrations/email-connection"),
    ("POST", "/api/v1/integrations/stripe"),
    ("DELETE", "/api/v1/integrations/stripe"),
    ("POST", "/api/v1/integrations/google-calendar/disconnect"),
    ("GET", "/api/v1/integrations/google-calendar/connect-url"),
    ("GET", "/api/v1/export/customers"),
    ("GET", "/api/v1/export/conversations"),
}


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
    missing = LOCKED - _guarded_routes()
    assert not missing, f"no owner/admin guard on: {sorted(missing)}"

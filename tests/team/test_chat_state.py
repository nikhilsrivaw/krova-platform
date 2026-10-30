"""Team chats: who is waiting, resolved chats reopening on a new message, unread counts, and the new routes."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

from services.api.routers import conversations as conv  # noqa: E402

T = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)


def m(minutes):
    return T + timedelta(minutes=minutes)


def test_the_customer_waits_from_their_first_message_after_the_last_reply():
    since, resolved = conv.chat_state(last_inbound=m(10), last_outbound=m(0), first_waiting=m(5), resolved_at=None)
    assert since == m(5).isoformat() and resolved is False


def test_nobody_waits_once_the_business_replied():
    assert conv.chat_state(last_inbound=m(0), last_outbound=m(1), first_waiting=None, resolved_at=None) == (None, False)


def test_a_resolved_chat_stays_resolved_until_the_customer_writes_again():
    assert conv.chat_state(last_inbound=m(0), last_outbound=None, first_waiting=m(0), resolved_at=m(2)) == (None, True)
    since, resolved = conv.chat_state(last_inbound=m(5), last_outbound=None, first_waiting=m(5), resolved_at=m(2))
    assert resolved is False and since == m(5).isoformat()


def test_naive_times_from_the_database_are_read_as_utc():
    naive = m(10).replace(tzinfo=None)
    since, _ = conv.chat_state(last_inbound=naive, last_outbound=m(0), first_waiting=None, resolved_at=None)
    assert since == m(10).isoformat()


class _Db:
    def __init__(self, customer=None):
        self.customer = customer
        self.sql = []

    async def get(self, model, key):
        return self.customer

    async def execute(self, stmt, *a, **k):
        self.sql.append(str(stmt.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(scalar=lambda: 4)


def _user(role="agent"):
    return SimpleNamespace(id=uuid.uuid4(), business=uuid.uuid4(), role=role)


def test_the_unread_count_compiles_and_respects_who_may_see_what(monkeypatch):
    async def rule(db, user):
        return None

    monkeypatch.setattr(conv.access, "customer_rule", rule)
    db = _Db()
    out = asyncio.run(conv.unread_count(_user("owner"), db))
    assert out == {"unread_chats": 4}
    sql = db.sql[0]
    assert "conversation_reads" in sql and "resolved_at" in sql and "is_private" in sql


def test_resolving_and_reopening_a_chat(monkeypatch):
    async def ok(db, user, customer):
        return None

    monkeypatch.setattr(conv.access, "check", ok)
    user = _user()
    customer = SimpleNamespace(id=uuid.uuid4(), business_id=user.business, resolved_at=None, resolved_by_user_id=None)
    out = asyncio.run(conv.resolve_conversation(customer.id, user, _Db(customer)))
    assert out["resolved"] is True and customer.resolved_by_user_id == user.id and customer.resolved_at is not None
    asyncio.run(conv.resolve_conversation(customer.id, user, _Db(customer), resolved=False))
    assert customer.resolved_at is None and customer.resolved_by_user_id is None


def test_another_businesss_chat_cannot_be_resolved_or_read():
    user = _user()
    stranger = SimpleNamespace(id=uuid.uuid4(), business_id=uuid.uuid4())
    for call in (conv.resolve_conversation(stranger.id, user, _Db(stranger)),
                 conv.mark_read(stranger.id, user, _Db(stranger))):
        with pytest.raises(HTTPException) as err:
            asyncio.run(call)
        assert err.value.status_code == 404


def test_marking_read_is_an_upsert_for_this_person_only(monkeypatch):
    async def ok(db, user, customer):
        return None

    monkeypatch.setattr(conv.access, "check", ok)
    user = _user()
    customer = SimpleNamespace(id=uuid.uuid4(), business_id=user.business)
    db = _Db(customer)
    asyncio.run(conv.mark_read(customer.id, user, db))
    assert "ON CONFLICT ON CONSTRAINT uq_conversation_read DO UPDATE" in db.sql[0]

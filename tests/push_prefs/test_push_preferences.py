"""Turning a kind of phone notification off: the API, and that a muted person's installs are skipped."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from services.api.routers import push as push_router
from shared.integrations import web_push


class MemberDb:
    def __init__(self, member):
        self.member, self.commits = member, 0

    async def execute(self, _q):
        return SimpleNamespace(scalar_one_or_none=lambda: self.member)

    async def commit(self):
        self.commits += 1


USER = SimpleNamespace(business=uuid.uuid4(), id=uuid.uuid4())


def test_every_kind_is_listed_and_on_by_default():
    out = asyncio.run(push_router.push_preferences(USER, MemberDb(SimpleNamespace(push_muted=[]))))
    assert [k.key for k in out] == list(web_push.PUSH_KINDS) and all(k.on for k in out)


def test_muting_saves_and_shows_off():
    member = SimpleNamespace(push_muted=[])
    db = MemberDb(member)
    out = asyncio.run(push_router.set_push_preferences(push_router.PushPreferencesIn(muted=["wallet", "draft", "wallet"]), USER, db))
    assert member.push_muted == ["draft", "wallet"] and db.commits == 1
    assert {k.key for k in out if not k.on} == {"draft", "wallet"}


def test_an_unknown_kind_is_refused():
    with pytest.raises(HTTPException):
        asyncio.run(push_router.set_push_preferences(push_router.PushPreferencesIn(muted=["spam"]), USER, MemberDb(SimpleNamespace(push_muted=[]))))


def test_a_muted_person_is_skipped(monkeypatch):
    muted_id = uuid.uuid4()
    seen = []

    class Db:
        async def execute(self, stmt):
            seen.append(stmt)
            if len(seen) == 1:  # who muted this kind
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [muted_id]))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    monkeypatch.setattr(web_push.settings, "vapid_private_key", "k")
    asyncio.run(web_push.send_to_business(Db(), business_id=uuid.uuid4(), payload={}, kind="wallet"))
    sql = str(seen[1].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "NOT IN" in sql.upper() and muted_id.hex in sql.replace("-", "")


def test_no_kind_means_everyone(monkeypatch):
    seen = []

    class Db:
        async def execute(self, stmt):
            seen.append(stmt)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    monkeypatch.setattr(web_push.settings, "vapid_private_key", "k")
    asyncio.run(web_push.send_to_business(Db(), business_id=uuid.uuid4(), payload={}))
    assert len(seen) == 1

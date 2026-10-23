"""The owner can wipe the whole knowledge base and the gaps queue in one go - never by accident, never as staff."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.api.routers import knowledge


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _DB:
    def __init__(self, items, dna):
        self.items, self.dna, self.deleted = items, dna, []

    async def execute(self, *_):
        return _Result(self.items)

    async def delete(self, item):
        self.deleted.append(item)

    async def get(self, *_):
        return self.dna

    async def flush(self):
        pass


def _user(role="owner"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, business=uuid.uuid4())


def _dna():
    return SimpleNamespace(known_gaps={
        "learned": ["parking", "home visits"], "owner_rules": ["refunds"], "from_template": ["diagnosis"],
    })


def test_reset_removes_every_entry_and_every_gap_but_keeps_hand_over_rules():
    items = [SimpleNamespace(topic="when"), SimpleNamespace(topic="catalogue"), SimpleNamespace(topic=None)]
    dna = _dna()
    db = _DB(items, dna)
    out = asyncio.run(knowledge.reset_knowledge(knowledge.ResetIn(confirm="delete"), _user(), db))
    assert (out.items, out.gaps) == (3, 2)
    assert db.deleted == items
    assert dna.known_gaps["learned"] == []
    assert dna.known_gaps["owner_rules"] == ["refunds"]
    assert dna.known_gaps["from_template"] == ["diagnosis"]


@pytest.mark.parametrize("role", ["admin", "agent"])
def test_only_the_owner_can_reset(role):
    db = _DB([SimpleNamespace(topic="when")], _dna())
    with pytest.raises(HTTPException) as err:
        asyncio.run(knowledge.reset_knowledge(knowledge.ResetIn(confirm="DELETE"), _user(role), db))
    assert err.value.status_code == 403
    assert db.deleted == []


@pytest.mark.parametrize("word", ["", "yes", "DELET"])
def test_reset_needs_the_word_typed(word):
    db = _DB([SimpleNamespace(topic="when")], _dna())
    with pytest.raises(HTTPException) as err:
        asyncio.run(knowledge.reset_knowledge(knowledge.ResetIn(confirm=word), _user(), db))
    assert err.value.status_code == 422
    assert db.deleted == []

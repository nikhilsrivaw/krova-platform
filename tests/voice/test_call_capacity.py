"""Campaigns dial slowly enough for Plivo and leave lines free for people calling in."""

import asyncio
import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

import httpx  # noqa: E402
import pytest  # noqa: E402

from services.workers import call_campaign  # noqa: E402
from shared.channels.voice import capacity  # noqa: E402
from shared.config.settings import settings  # noqa: E402
from shared.db import queue  # noqa: E402
from shared.db.models import JobStatus  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_limits_are_plivos_and_leave_room_for_incoming():
    assert capacity.limit() == 50 and settings.voice_inbound_reserve == 15
    assert capacity.campaign_ceiling() == 35


def test_dials_are_a_second_apart():
    clock = Clock()
    d = capacity.Dialler(clock)
    assert d.wait_before_next() == 0
    d.dialled()
    clock.now += 0.3
    assert d.wait_before_next() == pytest.approx(0.7)
    clock.now += 0.7
    assert d.wait_before_next() == 0


def test_a_full_house_stops_the_campaign_and_ringing_calls_count():
    clock = Clock()
    d = capacity.Dialler(clock)
    assert d.has_room(34)
    assert not d.has_room(35)
    for _ in range(5):
        d.dialled()
    assert not d.has_room(30)        # 30 open + 5 still ringing
    clock.now += capacity.RINGING_SECONDS + 1
    assert d.has_room(30)            # those have rung out or become open calls


def test_the_limits_can_be_changed_without_code(monkeypatch):
    monkeypatch.setattr(settings, "voice_concurrent_call_limit", 100)
    monkeypatch.setattr(settings, "voice_inbound_reserve", 20)
    assert capacity.campaign_ceiling() == 80


def _job():
    return SimpleNamespace(payload={"recipient_id": str(uuid.uuid4())}, attempts=1, status=JobStatus.running,
                           run_after=None, locked_by="w", locked_at="t", max_attempts=5)


def test_a_full_house_puts_the_recipient_back_without_using_an_attempt(monkeypatch):
    placed = []

    async def full(db):
        return 40

    async def place(rid, db):
        placed.append(rid)

    monkeypatch.setattr(capacity, "lines_in_use", full)
    monkeypatch.setattr(call_campaign.outbound, "place_call", place)
    monkeypatch.setattr(call_campaign, "dialler", capacity.Dialler())
    job = _job()
    asyncio.run(call_campaign._run_job(job, db=None))
    assert not placed
    assert job.status == JobStatus.pending and job.attempts == 0 and job.locked_by is None and job.run_after is not None


def test_room_on_the_lines_dials(monkeypatch):
    placed, done = [], []

    async def quiet(db):
        return 3

    async def place(rid, db):
        placed.append(rid)

    async def complete(job, db):
        done.append(job)

    monkeypatch.setattr(capacity, "lines_in_use", quiet)
    monkeypatch.setattr(call_campaign.outbound, "place_call", place)
    monkeypatch.setattr(call_campaign.queue, "complete", complete)
    monkeypatch.setattr(call_campaign, "dialler", capacity.Dialler())
    asyncio.run(call_campaign._run_job(_job(), db=None))
    assert len(placed) == 1 and len(done) == 1


def test_later_is_a_real_queue_function():
    assert callable(queue.later)


def _plivo(monkeypatch, handler):
    """Point capacity's Plivo calls at a fake, with fresh caches and a configured main account."""
    real = httpx.AsyncClient
    monkeypatch.setattr(capacity.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(capacity, "_live", None)
    monkeypatch.setattr(capacity, "_subaccounts", None)
    monkeypatch.setattr(settings, "plivo_auth_id", "MAIN")
    monkeypatch.setattr(settings, "plivo_auth_token", "main-token")


def _live_on(per_account):
    seen = []

    def handler(request):
        parts = request.url.path.strip("/").split("/")   # v1 Account <id> Call|Subaccount
        seen.append(parts[2])
        if parts[3] == "Subaccount":
            return httpx.Response(200, json={"objects": [{"auth_id": "SUB1", "auth_token": "t1"},
                                                         {"auth_id": "SUB2", "auth_token": "t2"}]})
        return httpx.Response(200, json={"calls": ["x"] * per_account.get(parts[2], 0)})
    return handler, seen


def test_team_calls_seen_only_by_plivo_are_counted(monkeypatch):
    handler, _ = _live_on({"MAIN": 1, "SUB1": 4, "SUB2": 6})   # e.g. 5 calls bridged to the team = 10 legs

    async def our_rows(db):
        return 2

    _plivo(monkeypatch, handler)
    monkeypatch.setattr(capacity, "open_calls", our_rows)
    assert asyncio.run(capacity.lines_in_use(db=None)) == 11


def test_our_count_wins_when_it_is_higher(monkeypatch):
    handler, _ = _live_on({"SUB1": 1})

    async def our_rows(db):
        return 3

    _plivo(monkeypatch, handler)
    monkeypatch.setattr(capacity, "open_calls", our_rows)
    assert asyncio.run(capacity.lines_in_use(db=None)) == 3


def test_plivo_down_falls_back_to_our_count(monkeypatch):
    async def our_rows(db):
        return 7

    _plivo(monkeypatch, lambda request: httpx.Response(503))
    monkeypatch.setattr(capacity, "open_calls", our_rows)
    assert asyncio.run(capacity.plivo_lines_in_use()) is None
    assert asyncio.run(capacity.lines_in_use(db=None)) == 7


def _warnings(monkeypatch):
    from shared.channels.voice import relay

    said = []
    monkeypatch.setattr(relay.logger, "warning", lambda msg, *a: said.append(msg % a))
    return relay, said


def test_the_near_full_warning_sees_team_calls(monkeypatch):
    relay, said = _warnings(monkeypatch)

    async def plivo():
        return 42

    monkeypatch.setattr(capacity, "plivo_lines_in_use", plivo)
    asyncio.run(relay._warn_with_plivo(own=3))
    assert said and "42/50" in said[0]


def test_the_warning_survives_plivo_failing(monkeypatch):
    relay, said = _warnings(monkeypatch)

    async def broken():
        raise RuntimeError("down")

    monkeypatch.setattr(capacity, "plivo_lines_in_use", broken)
    asyncio.run(relay._warn_with_plivo(own=10))
    assert not said


def test_a_reading_is_reused_for_a_few_seconds(monkeypatch):
    handler, seen = _live_on({"SUB2": 2})
    _plivo(monkeypatch, handler)
    assert asyncio.run(capacity.plivo_lines_in_use()) == 2
    asked = len(seen)
    assert asyncio.run(capacity.plivo_lines_in_use()) == 2
    assert len(seen) == asked                      # no second round of requests

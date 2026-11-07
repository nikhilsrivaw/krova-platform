"""Campaigns dial slowly enough for Plivo and leave lines free for people calling in."""

import asyncio
import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

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

    monkeypatch.setattr(capacity, "open_calls", full)
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

    monkeypatch.setattr(capacity, "open_calls", quiet)
    monkeypatch.setattr(call_campaign.outbound, "place_call", place)
    monkeypatch.setattr(call_campaign.queue, "complete", complete)
    monkeypatch.setattr(call_campaign, "dialler", capacity.Dialler())
    asyncio.run(call_campaign._run_job(_job(), db=None))
    assert len(placed) == 1 and len(done) == 1


def test_later_is_a_real_queue_function():
    assert callable(queue.later)

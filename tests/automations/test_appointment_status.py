"""What staff may record about a booking after the fact."""

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from services.api.routers.scheduling import next_status  # noqa: E402
from shared.db.models import AppointmentStatus as S  # noqa: E402

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
PAST = NOW - timedelta(hours=3)
FUTURE = NOW + timedelta(hours=3)


def test_a_past_booking_can_be_marked_visited_or_no_show():
    assert next_status(S.confirmed, "visited", PAST, NOW) is S.visited
    assert next_status(S.confirmed, "no_show", PAST, NOW) is S.no_show


def test_a_slip_can_be_undone():
    assert next_status(S.no_show, "confirmed", PAST, NOW) is S.confirmed
    assert next_status(S.visited, "no_show", PAST, NOW) is S.no_show


def test_visited_is_allowed_early_but_no_show_is_not():
    assert next_status(S.confirmed, "visited", FUTURE, NOW) is S.visited
    with pytest.raises(ValueError):
        next_status(S.confirmed, "no_show", FUTURE, NOW)


@pytest.mark.parametrize("current", [S.cancelled, S.awaiting_deposit])
def test_cancelled_and_deposit_holds_are_not_flipped_by_hand(current):
    with pytest.raises(ValueError):
        next_status(current, "visited", PAST, NOW)


def test_unknown_statuses_are_refused():
    with pytest.raises(ValueError):
        next_status(S.confirmed, "cancelled", PAST, NOW)

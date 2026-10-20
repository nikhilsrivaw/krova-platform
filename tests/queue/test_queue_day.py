"""The queue day is the business's own date, not the UTC date."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

from shared.scheduling import queue_booking


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        # 00:30 IST on 21 Oct = 19:00 UTC on 20 Oct
        return datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc).astimezone(tz)


def test_after_midnight_india_is_already_today(monkeypatch):
    monkeypatch.setattr(queue_booking, "datetime", _Clock)
    assert queue_booking.today_for(SimpleNamespace(timezone="Asia/Kolkata")) == date(2026, 10, 21)


def test_other_timezone_and_default(monkeypatch):
    monkeypatch.setattr(queue_booking, "datetime", _Clock)
    assert queue_booking.today_for(SimpleNamespace(timezone="Europe/London")) == date(2026, 10, 20)
    assert queue_booking.today_for(SimpleNamespace(timezone=None)) == date(2026, 10, 21)

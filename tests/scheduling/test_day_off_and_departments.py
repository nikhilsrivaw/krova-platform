"""
A day off shows who is already booked that day (and can cancel them), and the AI sees each doctor's department.
"""

import asyncio
import uuid
from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from services.api.routers import scheduling
from shared.ai import context

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 10, 25)


def _appt(hour, minute=0):
    return SimpleNamespace(id=uuid.uuid4(), starts_at=datetime.combine(DAY, time(hour, minute), tzinfo=IST).astimezone(timezone.utc))


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, statement):
        rows = self.rows
        return SimpleNamespace(all=lambda: rows)


def _exc(is_unavailable=True, start=None, end=None):
    return SimpleNamespace(doctor_id=uuid.uuid4(), date=DAY, is_unavailable=is_unavailable, start_time=start, end_time=end)


ROWS = [(_appt(10), "Ramesh"), (_appt(15), "Sita"), (_appt(18, 30), "Ali")]


def _names(hit):
    return [name for _, name in hit]


def test_a_whole_day_off_hits_every_booking_that_day():
    hit = asyncio.run(scheduling._bookings_hit_by(_Db(ROWS), _exc(), "Asia/Kolkata"))
    assert _names(hit) == ["Ramesh", "Sita", "Ali"]


def test_hours_off_hit_only_the_bookings_inside_them():
    hit = asyncio.run(scheduling._bookings_hit_by(_Db(ROWS), _exc(start=time(14), end=time(17)), "Asia/Kolkata"))
    assert _names(hit) == ["Sita"]


def test_changed_hours_hit_the_bookings_outside_what_is_left():
    hit = asyncio.run(scheduling._bookings_hit_by(_Db(ROWS), _exc(is_unavailable=False, start=time(9), end=time(16)), "Asia/Kolkata"))
    assert _names(hit) == ["Ali"]


def test_the_exception_reply_can_carry_the_affected_bookings():
    out = scheduling.AvailabilityExceptionOut(id="x", date=DAY, is_unavailable=True, start_time=None, end_time=None, reason=None)
    assert out.affected == []
    out.affected = [scheduling.AffectedBooking(id="a", customer_name="Ramesh", starts_at=ROWS[0][0].starts_at)]
    assert out.affected[0].customer_name == "Ramesh"


@pytest.mark.parametrize("doctor,expected", [
    (SimpleNamespace(name="Dr Mehta", department_id="d1", qualifications="MS (Ortho)"), "Dr Mehta (Orthopaedics, MS (Ortho))"),
    (SimpleNamespace(name="Dr Rao", department_id=None, qualifications="MBBS"), "Dr Rao (MBBS)"),
    (SimpleNamespace(name="Dr Sen", department_id=None, qualifications=None), "Dr Sen"),
])
def test_the_ai_sees_each_doctors_department_and_qualification(doctor, expected):
    assert context._doctor_label(doctor, {"d1": "Orthopaedics"}) == expected

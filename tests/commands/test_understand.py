from datetime import datetime
from zoneinfo import ZoneInfo

from shared.commands.understand import parse

NOW = datetime(2026, 10, 3, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def test_block_slot_tomorrow_with_hours():
    hit = parse("kal 2 se 4 band karo", NOW)
    assert hit and hit.tool == "block_slot"
    assert hit.args == {"date": "2026-10-04", "start": "14:00", "end": "16:00"}


def test_block_slot_with_explicit_24h_times():
    hit = parse("aaj 14:30 to 16:00 block karo", NOW)
    assert hit.args == {"date": "2026-10-03", "start": "14:30", "end": "16:00"}


def test_cancellation_window_setting():
    hit = parse("cancellation 6 ghante ki kar do", NOW)
    assert hit.tool == "set_setting"
    assert hit.args == {"key": "cancellation_window_hours", "value": 6}


def test_deposit_on_and_off():
    assert parse("deposit chalu karo", NOW).args["value"] is True
    assert parse("deposit band karo", NOW).args["value"] is False


def test_reports_recognised():
    assert parse("aaj ki bookings batao", NOW).args == {"name": "todays_bookings"}
    assert parse("is hafte ke no-show kitne the", NOW).tool == "report"
    assert parse("waitlist mein kitne log hain", NOW).args == {"name": "waitlist"}
    assert parse("ledger ka total kya hai", NOW).args == {"name": "ledger_summary"}


def test_unrecognised_returns_none():
    assert parse("mujhe ek poem likh do", NOW) is None
    assert parse("", NOW) is None

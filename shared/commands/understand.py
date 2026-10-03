"""
Mode B: turn a plain-language command into one tool call with no model and
no data leaving KROVA. Covers the common commands in Hindi, Hinglish and
English. Anything it does not recognise returns None, and the caller says so.

Times are read as 24-hour, or with am/pm. A bare hour below 7 ("2", "2 baje",
"2:30") is read as the afternoon or evening, since salons and restaurants
rarely open at 2 am. Write "2 am" to mean the night.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class Parsed:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|baje|baj|bje)?"


def _to_hhmm(hour: str, minute: str | None, marker: str | None) -> str | None:
    h = int(hour)
    m = int(minute) if minute else 0
    if m > 59:
        return None
    if marker == "pm" and h < 12:
        h += 12
    elif marker == "am" and h == 12:
        h = 0
    elif marker in (None, "baje", "baj", "bje") and h < 7:
        h += 12
    if not 0 <= h <= 23:
        return None
    return f"{h:02d}:{m:02d}"


def _day(word: str, now: datetime) -> str | None:
    if word in ("aaj", "today"):
        return now.date().isoformat()
    if word in ("kal", "tomorrow"):
        return (now + timedelta(days=1)).date().isoformat()
    return None


def parse(text: str, now: datetime | None = None) -> Parsed | None:
    now = now or datetime.now(LOCAL_TZ)
    t = " ".join(text.lower().strip().split())
    if not t:
        return None

    # Reports: a fixed set, read-only.
    if re.search(r"aaj.*(booking|appointment)|today.*booking", t):
        return Parsed("report", {"name": "todays_bookings"})
    if re.search(r"no.?show", t):
        days = re.search(r"(\d{1,3})\s*(din|day)", t)
        args: dict[str, Any] = {"name": "no_show_summary"}
        if days:
            args["period_days"] = int(days.group(1))
        return Parsed("report", args)
    if re.search(r"waitlist|walk.?in", t):
        return Parsed("report", {"name": "waitlist"})
    if re.search(r"ledger|baaki|udhaar|owed|overdue", t):
        return Parsed("report", {"name": "ledger_summary"})

    # Settings: the cancellation window and the deposit switch.
    cancel = re.search(r"(cancel|cancellation|policy)\D{0,20}(\d{1,2})\s*(ghante|hour|hr|h)", t)
    if cancel:
        return Parsed("set_setting", {"key": "cancellation_window_hours", "value": int(cancel.group(2))})
    if re.search(r"deposit", t):
        if re.search(r"\b(on|haan|chalu|lagao|yes)\b", t):
            return Parsed("set_setting", {"key": "deposit_required", "value": True})
        if re.search(r"\b(off|nahi|band|hatao|no)\b", t):
            return Parsed("set_setting", {"key": "deposit_required", "value": False})

    # Block a time range: "kal 2 se 4 band karo", "aaj 14:00 se 16:00 block".
    block = re.search(
        rf"\b(aaj|kal|today|tomorrow)\b.*?{_TIME}\s*(se|to|-)\s*{_TIME}.*?\b(band|block|close|off)\b",
        t,
    )
    if block:
        day = _day(block.group(1), now)
        start = _to_hhmm(block.group(2), block.group(3), block.group(4))
        end = _to_hhmm(block.group(6), block.group(7), block.group(8))
        if day and start and end:
            return Parsed("block_slot", {"date": day, "start": start, "end": end})

    return None


def describe_examples() -> list[str]:
    return [
        "kal 2 se 4 band karo",
        "cancellation 6 ghante ki kar do",
        "aaj ki bookings batao",
        "is hafte ke no-show kitne the",
        "waitlist mein kitne log hain",
        "ledger ka total kya hai",
        "deposit chalu karo",
    ]

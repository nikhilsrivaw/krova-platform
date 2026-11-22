"""An institution's students ask a question and leave: a short history, no reading for promises, no profiles.
Every other type keeps the defaults."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from shared import verticals  # noqa: E402
from shared.ai import context as ctx  # noqa: E402


def test_institution_keeps_a_short_history_and_skips_reading():
    talk = verticals.conversation_options("institution")
    assert talk["recent_turns"] == 4 and talk["earlier_turns"] == 0
    assert talk["read_for_promises"] is False and talk["customer_profiles"] is False


def test_other_types_keep_the_defaults():
    for key in verticals.keys() - {"institution"}:
        talk = verticals.conversation_options(key)
        assert talk.get("read_for_promises", True) and talk.get("customer_profiles", True), key
    assert verticals.conversation_options(None) == {} and verticals.conversation_options("no-such-type") == {}


def _msgs(*minutes_ago):
    now = datetime.now(timezone.utc)
    return [
        SimpleNamespace(channel="whatsapp", direction="inbound", occurred_at=now - timedelta(minutes=m))
        for m in minutes_ago
    ]


def test_a_short_history_is_cut_to_its_size():
    current, earlier = ctx.split_conversation(_msgs(*range(10)), recent_turns=4, earlier_turns=0)
    assert len(current) == 4 and earlier == []
    # The default is unchanged for everyone else.
    current, _ = ctx.split_conversation(_msgs(*range(30)))
    assert len(current) == ctx.RECENT_TURNS

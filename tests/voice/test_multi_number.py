"""
A business with several voice numbers: which number is used for what.

The queries are built for real and compiled to SQL (a wrong column fails here); answers come from
scripted stand-ins, since there is no database.
"""

import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from services.api.routers import call_campaigns as campaigns  # noqa: E402
from services.api.routers import voice_provisioning as provisioning  # noqa: E402
from shared.channels.voice import numbers as vn  # noqa: E402
from shared.channels.voice.tenant import _build_route  # noqa: E402
from shared.db.models import Channel, ChannelConnection, ConnectionStatus  # noqa: E402

BIZ = uuid.uuid4()


def number(digits: str, **extra) -> ChannelConnection:
    return ChannelConnection(
        id=uuid.uuid4(), business_id=BIZ, channel=Channel.voice, external_account_id=digits,
        status=ConnectionStatus.active, extra=extra,
    )


SERVICE = "+918041234567"   # 080 -> service
PROMO = "+911400123456"     # 140 -> promotional


# ── series ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("digits,series", [
    ("+918041234567", "080"), ("918041234567", "080"), ("+912241234567", "022"),
    ("+911400123456", "140"), ("1600123456", "160"), ("+919876543210", None), ("", None),
])
def test_series_is_read_from_the_digits(digits, series):
    assert vn.series_of(digits) == series


# ── choosing the number for an outgoing call ─────────────────────────────────

def test_with_nothing_marked_the_oldest_number_is_used_every_time():
    a, b = number(SERVICE), number(PROMO)
    assert vn.choose_outbound([a, b]) is a
    assert vn.choose_outbound([a, b]) is a


def test_the_default_for_outgoing_calls_wins_when_no_purpose_asks_otherwise():
    a, b = number(SERVICE), number(PROMO, default_outbound=True)
    assert vn.choose_outbound([a, b]) is b


def test_a_promotional_campaign_goes_out_of_the_140_number_even_if_080_is_the_default():
    service, promo = number(SERVICE, default_outbound=True), number(PROMO)
    assert vn.choose_outbound([service, promo], purpose="promotional") is promo
    assert vn.choose_outbound([service, promo], purpose="service") is service


def test_with_no_matching_series_the_default_is_returned_so_the_check_can_explain():
    only_service = number(SERVICE)
    assert vn.choose_outbound([only_service], purpose="promotional") is only_service


def test_a_number_the_caller_named_is_used_or_refused_never_swapped():
    a, b = number(SERVICE), number(PROMO)
    assert vn.choose_outbound([a, b], preferred=b.id) is b
    incoming_only = number(PROMO, use="inbound")
    assert vn.choose_outbound([a, incoming_only], preferred=incoming_only.id) is None
    assert vn.choose_outbound([a, b], preferred=uuid.uuid4()) is None


def test_an_incoming_only_number_never_places_calls_but_an_outgoing_only_one_does():
    inbound, outbound = number(SERVICE, use="inbound"), number(PROMO, use="outbound")
    assert vn.choose_outbound([inbound]) is None
    assert vn.choose_outbound([inbound, outbound]) is outbound


def test_the_staff_phone_comes_from_the_default_number_first():
    a = number(SERVICE, staff_phone_number="919800000001")
    b = number(PROMO, staff_phone_number="919800000002", default_outbound=True)
    assert vn.staff_phone_of([a, b]) == "919800000002"
    assert vn.staff_phone_of([number(SERVICE), a]) == "919800000001"
    assert vn.staff_phone_of([number(SERVICE)]) is None


# ── editing a number ─────────────────────────────────────────────────────────

def test_making_one_number_the_default_unmakes_the_others():
    a, b = number(SERVICE, default_outbound=True), number(PROMO)
    vn.apply_default([a, b], b, True)
    assert vn.is_default_outbound(b) and not vn.is_default_outbound(a)


def test_an_incoming_only_number_cannot_be_the_outgoing_default():
    a = number(SERVICE, use="inbound")
    with pytest.raises(vn.NumberError):
        vn.apply_default([a], a, True)


def test_names_and_uses_are_checked():
    assert vn.clean_label("  Delhi   branch ") == "Delhi branch"
    with pytest.raises(vn.NumberError):
        vn.clean_label("x" * 41)
    with pytest.raises(vn.NumberError):
        vn.clean_use("sideways")


def test_a_number_without_a_name_is_shown_by_its_digits():
    assert vn.label_of(number(SERVICE)) == SERVICE
    assert vn.label_of(number(SERVICE, label="Sales")) == "Sales"


def test_the_route_for_a_call_knows_what_the_number_is_for():
    business = SimpleNamespace(id=BIZ, name="B", owner_phone=None)
    route = _build_route(business, number(SERVICE, use="outbound", label="Outbound line"))
    assert route.use == "outbound" and route.label == "Outbound line"
    assert _build_route(business, number(SERVICE)).use == "both"


# ── the settings screens no longer break on a second number ──────────────────

class _Rows:
    def __init__(self, items):
        self.items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self.items), first=lambda: (self.items[0] if self.items else None))

    def all(self):
        return list(self.items)


class _Db:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.compiled = []

    async def execute(self, stmt):
        self.compiled.append(str(stmt.compile(dialect=postgresql.dialect())))
        return _Rows(self.answers.pop(0))


def _conn(db_numbers, cid=None):
    return asyncio.run(provisioning._voice_connection(BIZ, _Db(db_numbers), cid))


def test_two_numbers_no_longer_crash_the_settings_and_the_default_is_used():
    a, b = number(SERVICE), number(PROMO, default_outbound=True)
    assert _conn([a, b]) is b


def test_a_number_can_be_asked_for_by_id_and_a_stranger_is_not_found():
    a, b = number(SERVICE), number(PROMO)
    assert _conn([a, b], b.id) is b
    with pytest.raises(HTTPException) as err:
        _conn([a, b], uuid.uuid4())
    assert err.value.status_code == 404


def test_with_no_number_the_owner_is_told_to_buy_one():
    with pytest.raises(HTTPException) as err:
        _conn([])
    assert err.value.status_code == 409


# ── a campaign is judged on the number it really uses ────────────────────────

def _campaign(purpose, connection_id=None):
    return SimpleNamespace(business_id=BIZ, purpose=purpose, connection_id=connection_id)


def _check(campaign, *connections):
    return asyncio.run(campaigns._check_number_series(campaign, None, _Db(list(connections))))


def test_a_business_with_both_kinds_of_number_can_send_both_kinds_of_campaign():
    service, promo = number(SERVICE), number(PROMO)
    _check(_campaign("service"), service, promo)
    _check(_campaign("promotional"), service, promo)


def test_a_promotional_campaign_on_a_business_with_only_an_080_line_is_refused_by_name():
    with pytest.raises(HTTPException) as err:
        _check(_campaign("promotional"), number(SERVICE, label="Support"))
    assert "140" in err.value.detail and "Support" in err.value.detail


def test_naming_the_wrong_number_for_the_purpose_is_refused():
    service, promo = number(SERVICE), number(PROMO)
    with pytest.raises(HTTPException) as err:
        _check(_campaign("promotional", service.id), service, promo)
    assert "080" in err.value.detail


def test_no_number_that_can_call_out_means_no_campaign():
    with pytest.raises(HTTPException) as err:
        _check(_campaign("service"), number(SERVICE, use="inbound"))
    assert "can place calls" in err.value.detail


def test_the_old_typed_series_setting_is_no_longer_needed():
    # Previously a business that had not typed its series into settings could not send at all.
    _check(_campaign("service"), number(SERVICE))


# ── the numbers list and the call log ────────────────────────────────────────

def test_the_numbers_list_shows_rent_use_and_what_outgoing_calls_will_really_use():
    a, b = number(SERVICE, label="Support"), number(PROMO, use="outbound")
    usage = [(a.id, 4, 1250)]
    db = _Db([a, b], usage)
    out = asyncio.run(provisioning.my_numbers(SimpleNamespace(business=BIZ), db))
    by_label = {n.label: n for n in out}
    assert by_label["Support"].calls_30d == 4 and by_label["Support"].cost_paise_30d == 1250
    assert by_label["Support"].rent_paise == 22000
    assert by_label[PROMO].use == "outbound" and by_label[PROMO].series_label == "Promotional (140)"
    # nothing was ticked, so the oldest number that can call out is the one calls will use
    assert by_label["Support"].default_outbound and not by_label[PROMO].default_outbound


def test_the_call_log_can_be_limited_to_one_number():
    a = number(SERVICE, label="Sales")
    call = SimpleNamespace(
        id=uuid.uuid4(), customer_id=None, connection_id=a.id, direction=SimpleNamespace(value="inbound"),
        duration_seconds=30, cost_paise=100, cost_breakdown={}, hangup_cause=None,
        started_at=datetime(2026, 10, 12, tzinfo=timezone.utc), escalated=False, escalation_reason=None,
        outcome=None, sentiment=None, topic=None, summary=None,
    )
    # The wallet bills 150 paise for this call: the 100 stored on it plus 50 of AI that ran the call.
    db = _Db([call], [a], [(call.id, 150)])
    out = asyncio.run(provisioning.call_logs(SimpleNamespace(business=BIZ, role="owner"), db, limit=10, connection_id=a.id))
    assert out[0].number == SERVICE and out[0].number_label == "Sales"
    assert "calls.connection_id" in db.compiled[0]
    # what the page shows is exactly what settle_voice_usage takes from the wallet (no platform charge for now)
    assert (out[0].ai_paise, out[0].platform_paise, out[0].charged_paise) == (50, 0, 150)
    assert out[0].charged_display == "₹1.50"

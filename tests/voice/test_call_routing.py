"""Who a call rings, and the one-number-at-a-time chain Plivo walks."""

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.channels.voice import answer  # noqa: E402
from shared.team import call_routing as cr  # noqa: E402

A, B, C, D = (uuid.uuid4() for _ in range(4))


def _t(number, user=None, name="x"):
    return cr.Target(number, user, name)


def test_members_are_capped_and_the_business_number_comes_last():
    out = cr.order([_t("1", A), _t("2", B), _t("3", C), _t("4", D)], "9")
    assert [t.number for t in out] == ["1", "2", "3", "9"]
    assert out[-1].user_id is None


def test_a_repeated_number_is_rung_once():
    out = cr.order([_t("1", A), _t("1", B)], "1")
    assert [t.number for t in out] == ["1"]


def test_no_members_and_no_fallback_means_nobody_to_ring():
    assert cr.order([], None) == []


def test_a_business_with_only_its_old_staff_number_still_rings_it():
    assert [t.number for t in cr.order([], "919800000000")] == ["919800000000"]


def test_the_list_survives_the_trip_through_a_url():
    targets = [_t("919811111111", A), _t("919822222222", None)]
    assert cr.decode(cr.encode(targets)) == [("919811111111", A), ("919822222222", None)]


def test_a_mangled_user_id_is_ignored_not_fatal():
    assert cr.decode("919811111111:not-a-uuid") == [("919811111111", None)]


# ── the endpoint Plivo calls after each attempt ─────────────────────────────

class _Req:
    def __init__(self, query: str, form: dict):
        self.url = SimpleNamespace(query=query)
        self._form = form

    async def form(self):
        return self._form


@pytest.fixture(autouse=True)
def _no_signature(monkeypatch):
    monkeypatch.setattr(answer, "verify", lambda **kw: None)


def _call(t, i, status=None, call=None):
    form = {} if status is None else {"DialStatus": status}
    recorded = []

    async def fake_record(call_id, user_id, b_leg=""):
        recorded.append((call_id, user_id))

    answer._record_handler = fake_record
    resp = asyncio.run(answer.transfer_xml(_Req("q", form), t=t, i=i, call=call))
    return resp.body.decode(), recorded


def test_first_request_rings_the_first_member_and_points_back_at_the_next():
    t = cr.encode([_t("919811111111", A), _t("919822222222", B)])
    body, _ = _call(t, 0)
    assert "<Number>+919811111111</Number>" in body and "<Number>+919822222222" not in body
    assert "i=1" in body and "timeout=\"18\"" in body


def test_when_the_first_does_not_answer_the_second_is_tried():
    t = cr.encode([_t("919811111111", A), _t("919822222222", B)])
    body, recorded = _call(t, 1, "no-answer")
    assert "<Number>+919822222222</Number>" in body and "i=2" in body
    assert recorded == []


def test_when_everyone_is_tried_the_caller_is_told_honestly():
    t = cr.encode([_t("919811111111", A)])
    body, _ = _call(t, 1, "busy")
    assert "nobody is free" in body and "<Hangup/>" in body


def test_an_answered_call_ends_cleanly_and_remembers_who_took_it():
    t = cr.encode([_t("919811111111", A), _t("919822222222", B)])
    call = uuid.uuid4()
    body, recorded = _call(t, 2, "completed", call)
    assert "nobody is free" not in body
    assert recorded == [(call, B)]


def test_the_old_single_number_form_still_works():
    resp = asyncio.run(answer.transfer_xml(_Req("q", {}), number="+919800000000"))
    assert "<Number>+919800000000</Number>" in resp.body.decode()

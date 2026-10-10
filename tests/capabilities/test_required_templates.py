import asyncio
import os
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from fastapi import HTTPException  # noqa: E402

from services.api.routers import capabilities as caps_router  # noqa: E402
from shared.channels.whatsapp import required_templates as rt  # noqa: E402
from shared.channels.whatsapp import template_service  # noqa: E402
from shared.channels.whatsapp import templates as meta  # noqa: E402
from shared.db.models import Channel, ClaimStatus, TemplateStatus  # noqa: E402
from shared.scheduling import notify  # noqa: E402
from shared.verticals.capability_info import SWITCHABLE  # noqa: E402

VARIABLE = re.compile(r"\{\{\s*(\d+)\s*\}\}")


# ── the catalogue is internally sound ───────────────────────────────────────

@pytest.mark.parametrize("t", rt.CATALOGUE, ids=lambda t: t.name)
def test_every_template_is_well_formed(t):
    numbers = [int(n) for n in VARIABLE.findall(t.body)]
    assert numbers == list(range(1, len(numbers) + 1)), "variables must appear as {{1}}, {{2}}, ... in order"
    assert len(t.variables) == len(numbers) == len(t.examples)
    assert not t.body.lstrip().startswith("{{") and not t.body.rstrip().endswith("}}"), (
        "Meta refuses a body that starts or ends with a variable"
    )
    assert t.category in ("UTILITY", "MARKETING")
    assert t.capability in SWITCHABLE
    assert t.purpose and (t.one_click or t.note), "a template KROVA cannot create must say why"


@pytest.mark.parametrize("t", [t for t in rt.CATALOGUE if t.one_click], ids=lambda t: t.name)
def test_every_one_click_template_builds_valid_meta_components(t):
    components = t.draft().to_components()  # also runs Meta's own length/shape checks
    body = next(c for c in components if c["type"] == "BODY")
    if t.examples:
        assert body["example"]["body_text"] == [list(t.examples)]


def test_only_real_promotions_are_filed_as_marketing():
    marketing = {t.name for t in rt.CATALOGUE if t.category == "MARKETING"}
    assert marketing == {"abandoned_cart_recovery", "repeat_purchase_nudge", "onboarding_nudge", "expansion_nudge"}


def test_names_are_unique_and_are_ones_the_senders_really_look_for():
    assert len(rt.BY_NAME) == len(rt.CATALOGUE)
    constants = {
        value for name, value in vars(notify).items()
        if name.endswith("_TEMPLATE_NAME") and isinstance(value, str)
    }
    assert set(rt.BY_NAME) <= constants, set(rt.BY_NAME) - constants


# ── drift guard: the wording has the variable count the sender passes ───────

def _business():
    return SimpleNamespace(
        id=uuid.uuid4(), name="Sharma Clinic", vertical="local_service", settings=None,
        timezone="Asia/Kolkata",
    )


def _customer():
    return SimpleNamespace(id=uuid.uuid4(), display_name="Asha", preferred_language=None)


class _MsgDb:
    async def get(self, _model, _id):
        return SimpleNamespace(channel=Channel.whatsapp)


def test_every_template_has_the_variable_count_its_sender_passes(monkeypatch):
    sent = {}
    payloads = {}

    async def fake_send(db, *, business, customer, template_name, body_params, plain_text, button_payloads=None):
        sent[template_name] = body_params
        payloads[template_name] = button_payloads
        return True

    monkeypatch.setattr(notify, "_send", fake_send)
    biz, cust = _business(), _customer()
    when = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    doctor = SimpleNamespace(name="Dr. Sharma")
    claim = SimpleNamespace(insurer_or_tpa_name="Star Health", status=ClaimStatus.approved)
    commitment = SimpleNamespace(
        bug_fix_notified_at=None, description="Export broken", source_message_ids=[uuid.uuid4()],
    )

    async def run():
        await notify.send_confirmation(None, business=biz, customer=cust, doctor=doctor, starts_at=when)
        await notify.send_reminder(None, business=biz, customer=cust, doctor=doctor, starts_at=when)
        await notify.send_recall_reminder(None, business=biz, customer=cust)
        await notify.send_queue_checkin(None, business=biz, customer=cust, queue_number=12)
        await notify.send_queue_turn_near(None, business=biz, customer=cust, queue_number=12, tokens_ahead=3)
        await notify.send_claim_status_update(None, business=biz, customer=cust, claim=claim)
        await notify.send_cod_confirmation(None, business=biz, customer=cust, order_number="1042", total_paise=149900)
        await notify.send_abandoned_cart_recovery(None, business=biz, customer=cust, checkout_url="https://x/y")
        await notify.send_repeat_purchase_nudge(None, business=biz, customer=cust)
        await notify.send_ndr_reschedule_request(None, business=biz, customer=cust, order_number="1042")
        await notify.send_bug_fixed_notification(_MsgDb(), business=biz, customer=cust, commitment=commitment)
        await notify.send_onboarding_nudge(None, business=biz, customer=cust)
        await notify.send_expansion_nudge(None, business=biz, customer=cust)
        await notify.send_payment_failed_reminder(None, business=biz, customer=cust, invoice_url="https://x/pay")

    asyncio.run(run())

    assert set(sent) == set(rt.BY_NAME), set(rt.BY_NAME) ^ set(sent)
    for name, params in sent.items():
        expected = len(meta.variables_in(rt.BY_NAME[name].body))
        assert len(params) == expected, f"{name}: sender passes {len(params)} values, template has {expected} variables"

    # Buttons: a sender gives a payload to exactly the buttons the template has.
    for name, template in rt.BY_NAME.items():
        got = payloads[name] or []
        assert len(got) == len(template.buttons), (
            f"{name}: template has {len(template.buttons)} buttons, sender sets {len(got)} payloads"
        )
    from shared.care import cod_confirmation

    assert payloads["cod_confirmation"] == [cod_confirmation.CONFIRM_PAYLOAD, cod_confirmation.DECLINE_PAYLOAD]


# ── readiness ───────────────────────────────────────────────────────────────

def _row(name, status, reason=None):
    return SimpleNamespace(name=name, status=status, rejection_reason=reason)


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, *_a, **_k):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))


def _states(rows):
    return asyncio.run(rt.statuses(_Db(rows), uuid.uuid4()))


def test_a_template_nobody_made_is_missing():
    assert _states([])["appointment_reminder"] == ("missing", None)


def test_approved_beats_pending_beats_rejected_across_languages():
    rows = [
        _row("appointment_reminder", TemplateStatus.rejected, "policy"),
        _row("appointment_reminder", TemplateStatus.approved),
        _row("recall_reminder", TemplateStatus.rejected, "no good"),
        _row("recall_reminder", TemplateStatus.pending),
    ]
    states = _states(rows)
    assert states["appointment_reminder"][0] == "approved"
    assert states["recall_reminder"][0] == "pending"


def test_a_rejected_template_carries_metas_reason():
    states = _states([_row("queue_turn_near", TemplateStatus.rejected, "variable at end")])
    assert states["queue_turn_near"] == ("rejected", "variable at end")


# ── creating the missing ones ───────────────────────────────────────────────

@pytest.fixture
def meta_calls(monkeypatch):
    submitted: list[str] = []
    control = {"refuse": set(), "state": {}}

    async def fake_connection(db, business_id):
        return SimpleNamespace(id=uuid.uuid4()), "waba"

    async def fake_submit(db, *, business_id, connection, waba_id, draft, extra=None):
        if draft.name in control["refuse"]:
            raise meta.TemplateError("Meta said no")
        submitted.append(draft.name)

    async def fake_statuses(db, business_id):
        return {name: control["state"].get(name, ("missing", None)) for name in rt.BY_NAME}

    monkeypatch.setattr(template_service, "get_connection", fake_connection)
    monkeypatch.setattr(template_service, "submit", fake_submit)
    monkeypatch.setattr(rt, "statuses", fake_statuses)
    return SimpleNamespace(submitted=submitted, control=control)


def test_only_the_missing_one_click_templates_are_submitted(meta_calls):
    meta_calls.control["state"] = {
        "ndr_reschedule_request": ("pending", None),
        "repeat_purchase_nudge": ("rejected", "x"),
    }
    report = asyncio.run(rt.create_missing(None, uuid.uuid4(), "order_sync"))

    assert report.created == ["abandoned_cart_recovery", "cod_confirmation"]
    # Even a rejected one is left alone: resubmitting the same name is refused,
    # and a rejection needs a person to read why.
    assert sorted(report.already_there) == ["ndr_reschedule_request", "repeat_purchase_nudge"]
    assert report.needs_manual == []
    assert meta_calls.submitted == ["abandoned_cart_recovery", "cod_confirmation"]


def test_a_template_flagged_as_manual_is_reported_and_never_submitted(meta_calls, monkeypatch):
    from dataclasses import replace

    manual = replace(rt.BY_NAME["bug_fixed_notification"], one_click=False, note="Create by hand.")
    monkeypatch.setattr(
        rt, "CATALOGUE", tuple(manual if t.name == manual.name else t for t in rt.CATALOGUE)
    )
    report = asyncio.run(rt.create_missing(None, uuid.uuid4(), "product_feedback"))
    assert report.needs_manual == ["bug_fixed_notification"]
    assert "bug_fixed_notification" not in meta_calls.submitted


def test_no_template_needs_to_be_made_by_hand_today():
    assert all(t.one_click for t in rt.CATALOGUE)


def test_a_failed_payment_without_a_link_never_sends_meta_an_empty_value(monkeypatch):
    sent = {}

    async def fake_send(db, *, business, customer, template_name, body_params, plain_text, button_payloads=None):
        sent["params"] = body_params
        return True

    monkeypatch.setattr(notify, "_send", fake_send)
    asyncio.run(notify.send_payment_failed_reminder(None, business=_business(), customer=_customer(), invoice_url=None))
    assert sent["params"] == ["Asha", "your billing page"] and all(sent["params"])


def test_one_refusal_from_meta_does_not_stop_the_rest(meta_calls):
    meta_calls.control["refuse"] = {"appointment_confirmed"}
    report = asyncio.run(rt.create_missing(None, uuid.uuid4(), "scheduling"))

    assert report.created == ["appointment_reminder"]
    assert report.failed == [{"name": "appointment_confirmed", "error": "Meta said no"}]


def test_a_feature_with_no_templates_creates_nothing(meta_calls):
    report = asyncio.run(rt.create_missing(None, uuid.uuid4(), "quotations"))
    assert report.created == report.already_there == report.needs_manual == []
    assert meta_calls.submitted == []


# ── the endpoints ───────────────────────────────────────────────────────────

def _user(role):
    return SimpleNamespace(id=uuid.uuid4(), role=role, business=uuid.uuid4())


def test_an_agent_cannot_create_templates():
    with pytest.raises(HTTPException) as err:
        asyncio.run(caps_router.create_missing_templates("scheduling", _user("agent"), SimpleNamespace()))
    assert err.value.status_code == 403


def test_creating_templates_for_an_unknown_feature_is_a_404():
    with pytest.raises(HTTPException) as err:
        asyncio.run(caps_router.create_missing_templates("nope", _user("owner"), SimpleNamespace()))
    assert err.value.status_code == 404


def test_without_whatsapp_the_owner_is_told_so(monkeypatch):
    async def not_ready(db, business_id, capability):
        raise template_service.WhatsAppNotReady("Connect WhatsApp before creating templates")

    monkeypatch.setattr(rt, "create_missing", not_ready)
    with pytest.raises(HTTPException) as err:
        asyncio.run(caps_router.create_missing_templates("scheduling", _user("owner"), SimpleNamespace()))
    assert err.value.status_code == 409 and "Connect WhatsApp" in err.value.detail


def test_the_list_shows_each_features_template_state(monkeypatch):
    from tests.capabilities.test_capabilities import _FakeDb, _business, _user as list_user

    async def no_whatsapp(db, business_id):
        raise template_service.WhatsAppNotReady("x")

    monkeypatch.setattr(template_service, "get_connection", no_whatsapp)

    business = _business("local_service")
    db = _FakeDb(business, templates=[_row("appointment_reminder", TemplateStatus.approved)])
    rows = {r.key: r for r in asyncio.run(caps_router.list_capabilities(list_user("owner"), db))}

    needs = {t.name: t for t in rows["scheduling"].templates}
    assert needs["appointment_reminder"].status == "approved"
    assert needs["appointment_confirmed"].status == "missing"
    assert rows["quotations"].templates == []
    assert rows["scheduling"].can_create_templates is False


# ── COD: the buttons, and the payloads that make a tap recognisable ──────────

def test_the_cod_template_is_submitted_with_its_two_quick_reply_buttons():
    components = rt.BY_NAME["cod_confirmation"].draft().to_components()
    buttons = next(c for c in components if c["type"] == "BUTTONS")["buttons"]
    assert buttons == [
        {"type": "QUICK_REPLY", "text": "Confirm Order"},
        {"type": "QUICK_REPLY", "text": "Cancel Order"},
    ]


def test_send_template_attaches_a_payload_to_each_quick_reply_button(monkeypatch):
    from shared.channels.whatsapp.client import WhatsAppClient

    captured = {}

    async def fake_post(self, path, body):
        captured.update(body)
        return {"messages": [{"id": "wamid.1"}]}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    client = WhatsAppClient("token", "12345")
    asyncio.run(
        client.send_template(
            "919876543210", "cod_confirmation", "en",
            body_params=["Asha", "1042", "₹1,499"],
            quick_reply_payloads=["COD_CONFIRM", "COD_DECLINE"],
        )
    )

    components = captured["template"]["components"]
    buttons = [c for c in components if c["type"] == "button"]
    assert buttons == [
        {"type": "button", "sub_type": "quick_reply", "index": "0",
         "parameters": [{"type": "payload", "payload": "COD_CONFIRM"}]},
        {"type": "button", "sub_type": "quick_reply", "index": "1",
         "parameters": [{"type": "payload", "payload": "COD_DECLINE"}]},
    ]


def test_a_send_without_payloads_has_no_button_components(monkeypatch):
    from shared.channels.whatsapp.client import WhatsAppClient

    captured = {}

    async def fake_post(self, path, body):
        captured.update(body)
        return {"messages": [{"id": "wamid.1"}]}

    monkeypatch.setattr(WhatsAppClient, "_post", fake_post)
    asyncio.run(WhatsAppClient("t", "1").send_template("91987", "appointment_reminder", "en", body_params=["a", "b"]))
    assert all(c["type"] != "button" for c in captured["template"]["components"])


def test_a_tap_on_either_button_reaches_the_cod_handler():
    from shared.care import cod_confirmation
    from shared.channels.whatsapp import webhook

    def tapped(payload):
        parsed = webhook.parse({
            "object": "whatsapp_business_account",
            "entry": [{"id": "w", "changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "1"},
                "contacts": [{"wa_id": "919876543210", "profile": {"name": "Asha"}}],
                "messages": [{
                    "from": "919876543210", "id": "wamid.x", "timestamp": "1700000000",
                    "type": "button", "button": {"payload": payload, "text": "whatever the label is"},
                }],
            }}]}],
        })
        return parsed

    for sent, expected in (("COD_CONFIRM", "COD_CONFIRM"), ("COD_DECLINE", "COD_DECLINE")):
        parsed = tapped(sent)
        message = parsed.messages[0]
        stand_in = SimpleNamespace(media=message.media)
        assert cod_confirmation.button_payload(stand_in) == expected

    # And the case that used to happen: a tap that came back as the label only.
    label_only = SimpleNamespace(media={"kind": "button_reply", "payload": "Confirm Order"})
    assert cod_confirmation.button_payload(label_only) is None


def test_the_examples_endpoint_lists_every_template_with_buttons_and_samples():
    from services.api.routers import templates as templates_router

    out = asyncio.run(templates_router.list_template_examples(SimpleNamespace(business=uuid.uuid4())))
    by_name = {e.name: e for e in out}

    assert set(by_name) == set(rt.BY_NAME)
    cod = by_name["cod_confirmation"]
    assert cod.buttons == ["Confirm Order", "Cancel Order"]
    assert cod.examples == {"1": "Asha", "2": "1042", "3": "₹1,499"}
    assert by_name["appointment_reminder"].feature == "Appointments & staff calendar"

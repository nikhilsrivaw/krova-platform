"""
KROVA Buddy and support tickets: what the assistant reads, how its answer is cleaned, the allowance, and tickets.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.api.routers import help as help_router
from shared import help as buddy
from shared.billing import limits, plans
from shared.db.models import SupportTicket, SupportTicketCategory


def test_every_article_is_a_titled_markdown_file():
    files = sorted(buddy.ARTICLES.glob("*.md"))
    assert len(files) >= 10
    for f in files:
        assert f.read_text(encoding="utf-8").startswith("# "), f.name


def test_articles_keep_internal_material_out():
    text = buddy.articles().lower()
    for word in ("docs/new", "nikhil", "competitor research", "bedrock", "ai_live_routes", "markup"):
        assert word not in text, word


def test_prices_and_limits_come_from_code():
    facts = buddy.live_facts()
    for key, label in plans.PLAN_LABELS.items():
        base, _gst, total = plans.plan_charge(key)
        assert f"{label}: Rs {plans.rupees(base)}" in facts and plans.rupees(total) in facts
        assert f"{limits.PLAN_LIMITS[key].team_logins} team logins" in facts
    assert plans.rupees(plans.NUMBER_RENT_PAISE) in facts
    assert "Appointments & staff calendar" in facts


def test_a_ticket_draft_is_cleaned():
    assert buddy._ticket({"subject": "x"}) is None
    t = buddy._ticket({"subject": " Rule not running ", "details": "d", "category": "weird", "page": ""})
    assert t == {"subject": "Rule not running", "category": "other", "details": "d", "page": None}


# ── /help/ask ────────────────────────────────────────────────────────────────

class FakeResult:
    def __init__(self, n):
        self.n = n

    def scalar_one(self):
        return self.n


class FakeDb:
    def __init__(self, count=0):
        self.added, self.commits, self.count = [], 0, count

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1

    async def refresh(self, row):
        row.id = row.id or uuid.uuid4()
        row.created_at = datetime.now(timezone.utc)

    async def execute(self, _q):
        return FakeResult(self.count)

    async def get(self, model, key):
        return SimpleNamespace(name="Sharma Store", vertical="retail", full_name="Ravi", email="r@x.in", phone=None)


USER = SimpleNamespace(business=uuid.uuid4(), id=uuid.uuid4())


def _ask(monkeypatch, result=None, used=0, error=None):
    async def fake_answer(**kwargs):
        if error:
            raise error
        return result

    async def fake_used(db, business_id):
        return used

    monkeypatch.setattr(help_router.buddy, "answer", fake_answer)
    monkeypatch.setattr(help_router, "_asked_this_month", fake_used)
    db = FakeDb()
    body = help_router.AskIn(turns=[{"role": "owner", "text": "template kaise banaun?"}], page="/whatsapp")
    return asyncio.run(help_router.ask(body, USER, db)), db


def test_an_answer_is_metered(monkeypatch):
    out, db = _ask(monkeypatch, buddy.HelpAnswer(answer="WhatsApp page → New Template", offer_ticket=False, cost_paise=40))
    assert out.answer.startswith("WhatsApp") and out.ticket is None and out.used == 1
    assert db.added[0].krova_cost_paise == 40 and db.commits == 1


def test_a_ticket_offer_comes_with_its_draft(monkeypatch):
    draft = {"subject": "Payment page error", "category": "billing", "details": "d", "page": "/billing"}
    out, _ = _ask(monkeypatch, buddy.HelpAnswer(answer="Sorry", offer_ticket=True, ticket=draft))
    assert out.offer_ticket and out.ticket.category == "billing"


def test_the_monthly_allowance(monkeypatch):
    with pytest.raises(HTTPException) as err:
        _ask(monkeypatch, used=help_router.ASK_MONTHLY)
    assert err.value.status_code == 429


def test_model_down_is_a_clear_message(monkeypatch):
    with pytest.raises(HTTPException) as err:
        _ask(monkeypatch, error=RuntimeError("401"))
    assert err.value.status_code == 502


# ── tickets ──────────────────────────────────────────────────────────────────

def _create(monkeypatch, count=0):
    sent = []

    async def fake_mail(ticket, business, user):
        sent.append(ticket)

    monkeypatch.setattr(help_router, "_email_team", fake_mail)
    db = FakeDb(count=count)
    body = help_router.TicketIn(
        subject="Automation not running", category="bug", details="Lead rule did nothing yesterday",
        page="/automations", transcript=[{"role": "owner", "text": "rule nahi chala"}],
    )
    return asyncio.run(help_router.create_ticket(body, USER, db)), db, sent


def test_a_ticket_is_saved_and_the_team_is_told(monkeypatch):
    out, db, sent = _create(monkeypatch)
    ticket = db.added[0]
    assert isinstance(ticket, SupportTicket) and ticket.category == SupportTicketCategory.bug
    assert ticket.transcript == [{"role": "owner", "text": "rule nahi chala"}]
    assert out.number.startswith("KR-") and out.status == "open" and sent == [ticket]


def test_tickets_per_day_are_capped(monkeypatch):
    with pytest.raises(HTTPException) as err:
        _create(monkeypatch, count=help_router.TICKETS_PER_DAY)
    assert err.value.status_code == 429


def test_a_mail_failure_never_loses_the_ticket(monkeypatch):
    from shared.integrations import gmail_smtp

    async def boom(**kwargs):
        raise gmail_smtp.GmailSmtpError("down")

    monkeypatch.setattr(gmail_smtp, "send_email", boom)
    ticket = SupportTicket(
        id=uuid.uuid4(), business_id=uuid.uuid4(), subject="s", category=SupportTicketCategory.other, details="d",
        transcript=[],
    )
    asyncio.run(help_router._email_team(ticket, None, None))


def test_a_plain_answer_has_no_ticket():
    text, offer, ticket = buddy._split(" 1. **Billing page** par jao\n2. Add money ")
    assert text == "1. Billing page par jao\n2. Add money" and not offer and ticket is None


def test_the_ticket_line_is_cut_off_and_read():
    reply = 'Ye dikkat lag rahi hai.\n[[TICKET]] {"subject": "Payment failed", "category": "billing", "details": "UPI failed twice", "page": "/billing"}'
    text, offer, ticket = buddy._split(reply)
    assert text == "Ye dikkat lag rahi hai." and offer
    assert ticket == {"subject": "Payment failed", "category": "billing", "details": "UPI failed twice", "page": "/billing"}


def test_a_broken_ticket_line_still_offers_a_ticket():
    text, offer, ticket = buddy._split("Not sure.\n[[TICKET]] {subject: oops")
    assert text == "Not sure." and offer and ticket is None


def test_the_answer_is_asked_for_as_text_not_a_tool(monkeypatch):
    seen = {}

    async def fake_complete(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(text="Billing page → Wallet", cost_paise=30)

    monkeypatch.setattr(buddy.client, "complete", fake_complete)
    out = asyncio.run(buddy.answer(turns=[buddy.Turn("owner", "wallet?")], page="/billing", business_line="x"))
    assert "tool" not in seen and out.answer == "Billing page → Wallet" and out.cost_paise == 30


def test_thinking_is_never_shown_or_parsed():
    from shared.ai.providers import strip_thinking

    assert strip_thinking("<think>user wants {json}</think>\n Billing page → Wallet") == "Billing page → Wallet"
    assert strip_thinking("<THINK>never closed") == ""
    assert strip_thinking("plain") == "plain"

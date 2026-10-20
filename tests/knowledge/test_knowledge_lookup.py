"""
The opened-when-needed knowledge: a long Catalogue and a Question bank that the AI never reads whole. Only the rows and
answers the customer's latest words point at are shown, next to the conversation; a message that matches nothing adds
nothing. No AI call is involved in matching.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.api.routers import knowledge
from shared.ai import context
from shared.ai import knowledge_reader as kr
from shared.verticals import knowledge_card as kc
from shared.verticals import knowledge_lookup as kl

CATALOGUE = [
    {"name": "Cotton shirt", "price": "Rs 899", "details": "sizes S to XXL, white and blue"},
    {"name": "Linen shirt", "price": "Rs 1,499", "details": "summer wear"},
    {"name": "Denim jeans", "price": "Rs 1,999", "details": "slim and regular fit"},
    {"name": "iPhone 13 screen replacement", "price": "Rs 6,500", "details": "original display, 6 months warranty"},
    {"name": "Monthly gym plan", "price": "Rs 1,200 per month", "details": "includes trainer"},
    {"name": "Paneer tikka", "price": "Rs 280", "details": "veg starter"},
]

BANK = [
    {"question": "Do you offer EMI?", "answer": "Yes, on cards above Rs 3,000."},
    {"question": "Is parking available?", "answer": "Free parking behind the shop."},
    {"question": "Do you deliver to Kanpur?", "answer": "Yes, in 2 days, Rs 99."},
    {"question": "Can I return a shirt after wearing it?", "answer": "No, only unworn with tags within 7 days."},
]


def names(rows):
    return [r["name"] for r in rows]


# ── catalogue matching ───────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("cotton shirt ka price kya hai?", "Cotton shirt"),
    ("shirts kitne ki hai", "Cotton shirt"),
    ("Jeans available?", "Denim jeans"),
    ("iphone 13 ka screen kitne ka lagega", "iPhone 13 screen replacement"),
    ("gym ka monthly plan?", "Monthly gym plan"),
    ("paneer tikka hai?", "Paneer tikka"),
])
def test_the_item_asked_about_is_found(message, expected):
    assert expected in names(kl.match_catalogue(CATALOGUE, message))


def test_the_best_match_comes_first():
    assert names(kl.match_catalogue(CATALOGUE, "linen shirt price"))[0] == "Linen shirt"


@pytest.mark.parametrize("message", [
    "hello", "kya haal hai", "timing kya hai?", "aap kahan ho?", "price batao", "ok thanks", "", "   ",
])
def test_a_message_naming_no_item_shows_nothing(message):
    assert kl.match_catalogue(CATALOGUE, message) == []


def test_a_detail_alone_does_not_pull_a_row():
    # "warranty" is only in a row's details - the customer did not name that item
    assert kl.match_catalogue(CATALOGUE, "warranty milegi?") == []


def test_at_most_a_few_rows_are_shown():
    many = [{"name": f"Shirt {i}", "price": "Rs 500", "details": ""} for i in range(50)]
    assert len(kl.match_catalogue(many, "shirt")) == kl.MATCH_ROWS


# ── question bank matching ───────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("EMI milega?", "Do you offer EMI?"),
    ("emi", "Do you offer EMI?"),
    ("parking hai kya", "Is parking available?"),
    ("kanpur delivery karte ho?", "Do you deliver to Kanpur?"),
    ("shirt pehen ke return kar sakte hai?", "Can I return a shirt after wearing it?"),
])
def test_a_like_question_finds_its_answer(message, expected):
    assert expected in [p["question"] for p in kl.match_bank(BANK, message)]


@pytest.mark.parametrize("message", ["hello", "timing?", "ok", "kitna time lagega"])
def test_unrelated_messages_find_no_answer(message):
    assert kl.match_bank(BANK, message) == []


# ── what the AI is shown ─────────────────────────────────────────────────────

def _items():
    return [
        {"title": "What you do", "kind": "service", "content": "Clothing store.", "topic": "about", "card": None, "rows": []},
        {"title": "Catalogue", "kind": "price_list", "content": "...", "topic": kl.CATALOGUE_TOPIC, "card": None, "rows": CATALOGUE},
        *({"title": b["question"], "kind": "faq", "content": b["answer"], "topic": None, "card": kl.BANK, "rows": []} for b in BANK),
    ]


class _Ctx:
    """Just the parts of an AgentContext the lookup reads."""

    render_lookup = context.AgentContext.render_lookup
    render_conversation = context.AgentContext.render_conversation

    def __init__(self, recent):
        self.knowledge = _items()
        self.recent = recent


def _turn(text, direction="inbound"):
    return {"channel": "whatsapp", "direction": direction, "text": text}


def test_the_catalogue_and_bank_are_never_in_the_always_read_block():
    text = "\n".join(context.render_knowledge(_items()))
    assert "Cotton shirt" not in text and "EMI" not in text
    assert "longer catalogue and more saved answers" in text and "do not guess" in text


def test_matching_rows_and_answers_sit_with_the_conversation():
    ctx = _Ctx([_turn("cotton shirt ka price? aur EMI milega?")])
    convo = ctx.render_conversation()
    assert "From the catalogue" in convo and "Cotton shirt - Rs 899 (sizes S to XXL, white and blue)" in convo
    assert "Q: Do you offer EMI?\nA: Yes, on cards above Rs 3,000." in convo
    assert convo.index("Cotton shirt") < convo.index("The conversation so far:")


def test_a_plain_message_adds_nothing():
    ctx = _Ctx([_turn("hello, kab khule ho?")])
    assert ctx.render_lookup() == ""
    assert ctx.render_conversation().startswith("\n---\n\nThe conversation so far:")


def test_a_follow_up_still_sees_the_item_named_one_message_before():
    ctx = _Ctx([_turn("linen shirt hai?"), _turn("Haan hai", "outbound"), _turn("size kya kya hai?")])
    assert "Linen shirt" in ctx.render_lookup()


def test_only_the_customers_words_are_matched_not_the_business_replies():
    ctx = _Ctx([_turn("hi"), _turn("We have cotton shirts and denim jeans", "outbound")])
    assert ctx.render_lookup() == ""


def test_lookup_items_are_not_other_entries():
    assert not kc.is_other(SimpleNamespace(topic=None, extra={"card": "bank"}))
    assert not kc.is_other(SimpleNamespace(topic="catalogue", extra={}))


# ── storage ──────────────────────────────────────────────────────────────────

def test_rows_are_trimmed_and_nameless_rows_dropped():
    rows = kl.clean_rows([{"name": "x" * 200, "price": "1"}, {"name": "  ", "price": "2"}, "junk", {"name": "Tea"}])
    assert len(rows) == 2 and len(rows[0]["name"]) == kl.CATALOGUE_LIMITS["name"]
    assert rows[1] == {"name": "Tea", "price": "", "details": ""}


def test_a_catalogue_is_capped():
    rows = kl.clean_rows([{"name": f"Item {i}"} for i in range(kl.CATALOGUE_ROWS_LIMIT + 20)])
    assert len(rows) == kl.CATALOGUE_ROWS_LIMIT


class _Db:
    def __init__(self, rows=None):
        self.rows, self.added, self.deleted = list(rows or []), [], []

    async def execute(self, *_):
        rows = self.rows
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            scalar_one_or_none=lambda: next((r for r in rows if getattr(r, "topic", None) == kl.CATALOGUE_TOPIC), None),
        )

    def add(self, item):
        self.added.append(item)

    async def delete(self, item):
        self.deleted.append(item)

    async def flush(self):
        pass


USER = SimpleNamespace(business=uuid.uuid4())


def test_saving_a_catalogue_costs_no_plan_budget():
    db = _Db()
    out = asyncio.run(knowledge.save_catalogue(
        knowledge.CatalogueIn(rows=[knowledge.CatalogueRow(name="Tea", price="Rs 20")]), current_user=USER, db=db,
    ))
    item = db.added[0]
    assert item.topic == kl.CATALOGUE_TOPIC and item.token_estimate == 0
    assert item.extra == {"catalogue": [{"name": "Tea", "price": "Rs 20", "details": ""}]}
    assert [r.name for r in out.rows] == ["Tea"]


def test_an_empty_catalogue_removes_it():
    existing = SimpleNamespace(topic=kl.CATALOGUE_TOPIC, extra={"catalogue": [{"name": "Tea"}]})
    db = _Db([existing])
    asyncio.run(knowledge.save_catalogue(knowledge.CatalogueIn(rows=[]), current_user=USER, db=db))
    assert db.deleted == [existing]


def test_a_bank_answer_costs_no_plan_budget_and_the_bank_is_capped():
    db = _Db()
    asyncio.run(knowledge.add_to_bank(knowledge.BankIn(question="EMI?", answer="Yes"), current_user=USER, db=db))
    assert db.added[0].extra == {"card": kl.BANK} and db.added[0].token_estimate == 0

    full = _Db([SimpleNamespace(topic=None, extra={"card": kl.BANK}) for _ in range(kl.BANK_LIMIT)])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(knowledge.add_to_bank(knowledge.BankIn(question="Q", answer="A"), current_user=USER, db=full))
    assert exc.value.status_code == 422


# ── a file read fills them too ───────────────────────────────────────────────

def test_a_file_read_returns_catalogue_rows_and_more_answers(monkeypatch):
    answer = {
        "about": "We sell shirts.", "faqs": [],
        "catalogue": [{"name": "Cotton shirt", "price": "Rs 899", "details": "S-XXL"}, {"name": "", "price": "1", "details": ""}],
        "more_questions": [{"question": "EMI?", "answer": "Yes"}, {"question": "Parking?", "answer": "Yes"}],
    }

    async def complete(**kwargs):
        assert kwargs["max_tokens"] == kr.MAX_OUTPUT_TOKENS
        return SimpleNamespace(tool_input=answer, cost_paise=4)

    monkeypatch.setattr(kr.client, "complete", complete)
    reading = asyncio.run(kr.read_into_card(raw=b"price list", filename="a.txt", content_type=None, vertical="general", faq_room=0, bank_room=1))
    assert reading.catalogue == [{"name": "Cotton shirt", "price": "Rs 899", "details": "S-XXL"}]
    assert reading.bank == [("EMI?", "Yes")]


# ── the fee list (shared/payments/fee_list.py) ───────────────────────────────

def test_the_fee_list_is_opened_only_when_a_fee_is_asked_about():
    from shared.payments import fee_list

    item = SimpleNamespace(
        title="Fees / Price list", kind=SimpleNamespace(value="price_list"), content="CT scan - ₹2,500", topic=fee_list.TOPIC,
        extra={"fees": [{"id": "ct", "name": "CT scan", "amount_paise": 250000}]},
    )
    as_dict = context.knowledge_dict(item)
    assert as_dict["rows"] == [{"name": "CT scan", "price": "₹2,500", "details": ""}]

    ctx = _Ctx([_turn("CT scan kitne ka hai?")])
    ctx.knowledge = [as_dict]
    assert "CT scan - ₹2,500" in ctx.render_lookup()
    assert "CT scan" not in "\n".join(context.render_knowledge([as_dict]))
    assert not kc.is_other(SimpleNamespace(topic=fee_list.TOPIC, extra={}))


def test_saving_the_fee_list_costs_no_plan_budget_and_keeps_amounts_as_numbers(monkeypatch):
    from shared.payments import fee_list

    async def switched_on(db, business_id):
        return None

    monkeypatch.setattr(fee_list, "require_enabled", switched_on)
    db = _Db()
    out = asyncio.run(knowledge.save_fees(
        knowledge.FeesIn(rows=[knowledge.FeeRow(name="X-ray", amount_paise=40000)]), current_user=USER, db=db,
    ))
    item = db.added[0]
    assert item.topic == fee_list.TOPIC and item.token_estimate == 0
    assert item.extra["fees"][0]["amount_paise"] == 40000 and out.rows[0].id


def test_the_fee_list_is_refused_and_hidden_from_the_ai_while_switched_off():
    from shared.payments import fee_list

    off = SimpleNamespace(id=uuid.uuid4(), vertical="general", settings={})
    on = SimpleNamespace(id=off.id, vertical="general", settings={"capability_overrides": {fee_list.CAPABILITY: True}})

    class _BizDb(_Db):
        def __init__(self, business):
            super().__init__()
            self.business = business

        async def get(self, model, key):
            return self.business

    with pytest.raises(HTTPException) as err:
        asyncio.run(knowledge.get_fees(current_user=USER, db=_BizDb(off)))
    assert err.value.status_code == 403 and "Price list & payments" in err.value.detail

    fees, card = SimpleNamespace(topic=fee_list.TOPIC), SimpleNamespace(topic="about")
    assert fee_list.usable([fees, card], off) == [card]
    assert fee_list.usable([fees, card], on) == [fees, card]

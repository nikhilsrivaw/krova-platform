"""
Knowledge as two short cards (Business Card and Team Card) plus at most ten top questions, the same for every business
type; the gaps loop that keeps them small; the showroom type; and what the agent is allowed to read.
No Postgres here, so the routes run against a scripted fake session.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from services.api.routers import knowledge
from shared import verticals
from shared.ai import context
from shared.db.models import KnowledgeKind
from shared.verticals import knowledge_card as kc

ALL_TYPES = sorted(verticals.keys())
BIZ = uuid.uuid4()


# ── the card is the same for every type; types only reword it ────────────────

@pytest.mark.parametrize("key", ALL_TYPES)
def test_every_type_has_exactly_the_same_fields(key):
    assert [f["key"] for f in kc.fields_for(key)] == [f["key"] for f in kc.CARD_FIELDS]


@pytest.mark.parametrize("key", ALL_TYPES)
def test_a_types_wording_block_only_rewords_known_fields(key):
    wording = verticals.get(key).get("knowledge_card") or {}
    assert set(wording) <= kc.FIELD_KEYS
    for attrs in wording.values():
        assert set(attrs) <= {"title", "question", "placeholder"}
    assert "knowledge_profile" not in verticals.get(key)


def test_wording_follows_the_business_type():
    title = {k: next(f["title"] for f in kc.fields_for(k) if f["key"] == "offerings") for k in ("general", "programs", "b2b", "showroom_vehicle")}
    assert title == {
        "general": "Top offerings and prices", "programs": "Top plans and fees",
        "b2b": "Top products and rates", "showroom_vehicle": "Models and prices",
    }


def test_the_whole_card_stays_small_enough_to_read_on_every_message():
    business_chars = sum(f["limit"] for f in kc.CARD_FIELDS)
    team_chars = kc.TEAM_PEOPLE_LIMIT * sum(kc.TEAM_LIMITS.values()) + sum(kc.TEAM_TEXT_LIMITS.values())
    faq_chars = kc.FAQ_LIMIT * (kc.FAQ_QUESTION_LIMIT + kc.FAQ_ANSWER_LIMIT)
    tokens = (business_chars + team_chars + faq_chars) // 4
    assert tokens <= 3000  # the smallest plan allows 6,000; a full card uses about half, an ordinary one far less


@pytest.mark.parametrize("key", ALL_TYPES)
def test_placeholders_are_fill_ins_never_facts(key):
    for f in kc.fields_for(key):
        assert "..." in f["placeholder"], f"{key}/{f['key']}"


def test_limits_and_links_are_checked():
    offerings = next(f for f in kc.CARD_FIELDS if f["key"] == "offerings")
    link = next(f for f in kc.CARD_FIELDS if f["key"] == "full_list")
    assert kc.check_value(offerings, "x" * offerings["limit"]) is None
    assert "characters" in kc.check_value(offerings, "x" * (offerings["limit"] + 1))
    assert kc.check_value(link, "https://shop.example.com/menu") is None
    assert "link" in kc.check_value(link, "see our instagram")


@pytest.mark.parametrize("gap,field", [
    ("Sunday opening hours", "when"),
    ("EMI options for the bike", "payment"),
    ("doctor ka number", "team"),
    ("refund kab milega", "changes"),
    ("deliver to Lucknow?", "where"),
    ("something unrelated", None),
])
def test_a_gap_suggests_the_field_it_belongs_to(gap, field):
    assert kc.suggest_field(gap) == field


def test_the_team_card_reads_as_short_plain_text():
    text = kc.render_team({
        "contact": "WhatsApp ... for sales", "languages": "Hindi, English", "response_note": "within an hour, 10am-7pm",
        "people": [{"name": "Riya", "role": "Front desk", "handles": "bookings and billing", "languages": "Hindi"}, {"name": ""}],
    })
    assert text.splitlines() == [
        "How to reach us: WhatsApp ... for sales",
        "Who customers can ask for:",
        "- Riya, Front desk - bookings and billing (speaks Hindi)",
        "Languages we reply in: Hindi, English",
        "When we reply: within an hour, 10am-7pm",
    ]
    assert kc.render_team({}) == ""


# ── the showroom type ─────────────────────────────────────────────────────────

def test_the_showroom_type_is_offered_at_signup():
    entry = next(v for v in verticals.available() if v["key"] == "showroom_vehicle")
    assert entry["label"] == "Car or bike showroom (dealership)"
    assert {"Quotations", "Appointments & staff calendar"} <= set(entry["features"])


def test_the_showroom_rules_forbid_the_risky_promises():
    t = verticals.get("showroom_vehicle")
    text = t["policies"].lower()
    for must in ("on-road price", "discount", "exchange value", "loan", "delivery date", "refund"):
        assert must in text, must
    assert all(k == k.lower() for k in t["escalate_keywords"]) and "last price" in t["escalate_keywords"]
    assert {w["kind"] for w in t["watch_for"]} <= {"payment", "delivery", "callback", "document", "meeting", "other"}


# ── what the agent reads ──────────────────────────────────────────────────────

def test_switched_off_or_expired_knowledge_is_not_read():
    sql = str(context.live_knowledge_query(uuid.uuid4()).compile(dialect=postgresql.dialect()))
    assert "knowledge_items.active" in sql and "valid_until IS NULL" in sql and "valid_until >= now()" in sql


def test_only_the_template_list_and_the_owners_handovers_are_do_not_answer_rules():
    dna = SimpleNamespace(known_gaps={"from_template": ["Any clinical question"], "owner_rules": ["Bulk discount"],
                                      "learned": ["Sunday opening hours"]})
    assert context.standing_gaps(dna) == ["Any clinical question", "Bulk discount"]
    assert context.standing_gaps(None) == [] and context.standing_gaps(SimpleNamespace(known_gaps=None)) == []


# ── routes ────────────────────────────────────────────────────────────────────

def who():
    return SimpleNamespace(business=BIZ, id=uuid.uuid4(), role="owner")


def item(topic=None, content="old", title="T", kind=KnowledgeKind.other, active=True, valid_until=None, extra=None):
    return SimpleNamespace(
        id=uuid.uuid4(), business_id=BIZ, title=title, kind=kind, source=SimpleNamespace(value="typed"), content=content,
        filename=None, token_estimate=len(content) // 4, active=active, resolves_gap=None,
        created_at=datetime(2026, 10, 17, tzinfo=timezone.utc), topic=topic, valid_until=valid_until, extra=extra or {},
    )


class FakeDb:
    """Answers each select in turn (a list of rows, or a number for a sum), remembers adds and deletes."""

    def __init__(self, *answers, vertical="programs", dna=None, messages=None, item_by_id=None):
        self.answers, self.added, self.deleted = list(answers), [], []
        self.business = SimpleNamespace(id=BIZ, vertical=vertical, plan="trial")
        self.dna, self.messages, self.item_by_id = dna, messages or {}, item_by_id

    async def execute(self, _stmt):
        rows = self.answers.pop(0) if self.answers else []
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows, first=lambda: rows[0] if rows else None),
            scalar_one=lambda: rows if not isinstance(rows, list) else 0,
            scalar_one_or_none=lambda: rows[0] if rows else None,
        )

    async def get(self, model, key):
        name = model.__name__
        if name == "Business":
            return self.business
        if name == "BusinessDNA":
            return self.dna
        if name == "KnowledgeItem":
            return self.item_by_id
        return self.messages.get(key)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        for o in self.added:
            if getattr(o, "created_at", None) is None:
                o.created_at = datetime(2026, 10, 17, tzinfo=timezone.utc)
            if getattr(o, "id", None) is None:
                o.id = uuid.uuid4()


def run(coro):
    return asyncio.run(coro)


def test_the_card_shows_this_types_wording_the_answers_team_and_top_questions():
    rows = [
        item(topic="when", content="Mon-Sat 10-8"),
        item(topic="team", content="Who customers can ask for:\n- Riya", extra={"team_card": {"people": [{"name": "Riya"}], "contact": "", "languages": "", "response_note": ""}}),
        item(kind=KnowledgeKind.faq, title="Do you have parking?", content="Yes, free.", extra={"card": "faq"}),
        item(content="an old long document"),  # not part of the cards
    ]
    out = run(knowledge.card(who(), FakeDb(rows)))
    when = next(f for f in out.fields if f.key == "when")
    assert when.value == "Mon-Sat 10-8" and next(f for f in out.fields if f.key == "offerings").title == "Top plans and fees"
    assert out.team.people[0].name == "Riya" and out.faqs[0].question == "Do you have parking?"
    assert out.coverage.required_done == 1 and out.coverage.team_done == 1 and out.coverage.faqs == 1
    assert out.tokens == sum(r.token_estimate for r in rows[:3])


def test_saving_a_field_creates_one_item_then_updates_it_then_removes_it_when_empty():
    db = FakeDb([])
    run(knowledge.save_card_field("when", knowledge.CardFieldIn(content="  Mon-Sat 10-8 "), who(), db))
    created = db.added[0]
    assert created.topic == "when" and created.content == "Mon-Sat 10-8" and created.kind == KnowledgeKind.hours

    run(knowledge.save_card_field("when", knowledge.CardFieldIn(content="Daily 9-9"), who(), FakeDb([created])))
    assert created.content == "Daily 9-9"

    db3 = FakeDb([created])
    assert run(knowledge.save_card_field("when", knowledge.CardFieldIn(content=" "), who(), db3)) is None
    assert db3.deleted == [created]


def test_a_field_over_its_limit_or_a_bad_link_is_refused():
    with pytest.raises(HTTPException) as e:
        run(knowledge.save_card_field("about", knowledge.CardFieldIn(content="x" * 301), who(), FakeDb([])))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException):
        run(knowledge.save_card_field("full_list", knowledge.CardFieldIn(content="our insta page"), who(), FakeDb([])))
    with pytest.raises(HTTPException) as e:
        run(knowledge.save_card_field("not_a_field", knowledge.CardFieldIn(content="x"), who(), FakeDb([])))
    assert e.value.status_code == 404


def test_only_the_offers_field_keeps_an_end_date():
    end = datetime.now(timezone.utc) + timedelta(days=3)
    db = FakeDb([])
    run(knowledge.save_card_field("offers", knowledge.CardFieldIn(content="Diwali offer", valid_until=end), who(), db))
    assert db.added[0].valid_until == end
    db2 = FakeDb([])
    run(knowledge.save_card_field("about", knowledge.CardFieldIn(content="We are ...", valid_until=end), who(), db2))
    assert db2.added[0].valid_until is None


def test_answering_a_gap_through_a_field_closes_the_gap():
    dna = SimpleNamespace(known_gaps={"learned": ["Sunday opening hours", "Parking"]})
    db = FakeDb([], dna=dna)
    run(knowledge.save_card_field("when", knowledge.CardFieldIn(content="Sunday closed", resolves_gap="sunday opening hours"), who(), db))
    assert dna.known_gaps["learned"] == ["Parking"]


def test_the_team_card_is_saved_as_one_short_item_with_its_rows():
    db = FakeDb([])
    body = knowledge.TeamCardIn(contact="Call ... ", people=[knowledge.TeamPerson(name="Riya", role="Front desk")], languages="Hindi")
    out = run(knowledge.save_team_card(body, who(), db))
    saved = db.added[0]
    assert saved.topic == "team" and "Riya, Front desk" in saved.content and saved.extra["team_card"]["people"][0]["name"] == "Riya"
    assert out.people[0].name == "Riya"

    db2 = FakeDb([saved])
    run(knowledge.save_team_card(knowledge.TeamCardIn(), who(), db2))
    assert db2.deleted == [saved]


def test_top_questions_stop_at_ten():
    faqs = [item(kind=KnowledgeKind.faq, extra={"card": "faq"}) for _ in range(kc.FAQ_LIMIT)]
    with pytest.raises(HTTPException) as e:
        run(knowledge.add_faq(knowledge.FaqIn(question="Q?", answer="A."), who(), FakeDb(faqs)))
    assert e.value.status_code == 422
    db = FakeDb(faqs[:3])
    out = run(knowledge.add_faq(knowledge.FaqIn(question="Parking?", answer="Free parking."), who(), db))
    assert out.question == "Parking?" and db.added[0].extra == {"card": "faq"} and db.added[0].kind == KnowledgeKind.faq


def test_a_top_question_answer_must_stay_short():
    with pytest.raises(Exception):
        knowledge.FaqIn(question="Q?", answer="x" * (kc.FAQ_ANSWER_LIMIT + 1))


# ── gaps ──────────────────────────────────────────────────────────────────────

def draft(gap, minutes_ago=0):
    return SimpleNamespace(gap=gap, created_at=datetime(2026, 10, 17, tzinfo=timezone.utc) - timedelta(minutes=minutes_ago), in_reply_to_id=None)


def test_gaps_include_voice_ones_count_text_asks_and_suggest_a_field():
    dna = SimpleNamespace(known_gaps={"learned": ["Sunday opening hours", "Something odd"]})
    out = run(knowledge.list_gaps(who(), FakeDb([draft("sunday opening hours", 1), draft("Sunday opening hours?", 2)], dna=dna)))
    assert [g.gap for g in out] == ["Sunday opening hours", "Something odd"]
    assert out[0].times_asked == 2 and out[0].suggested_field == "when"
    assert out[1].times_asked == 1 and out[1].last_asked is None and out[1].suggested_field is None


def test_always_hand_this_to_me_moves_a_gap_into_the_owners_rules():
    dna = SimpleNamespace(known_gaps={"from_template": ["x"], "learned": ["Bulk discount?", "Parking"]})
    run(knowledge.handover_gap(knowledge.GapDismissIn(gap="bulk discount"), who(), FakeDb(dna=dna)))
    assert dna.known_gaps["owner_rules"] == ["bulk discount"] and dna.known_gaps["learned"] == ["Parking"]
    run(knowledge.handover_gap(knowledge.GapDismissIn(gap="Bulk discount?"), who(), FakeDb(dna=dna)))
    assert dna.known_gaps["owner_rules"] == ["bulk discount"]  # not twice


def test_a_dismissed_gap_stays_gone():
    dna = SimpleNamespace(known_gaps={"learned": ["Sunday opening hours", "Parking"]})
    run(knowledge.dismiss_gap(knowledge.GapDismissIn(gap="sunday opening hours?"), who(), FakeDb(dna=dna)))
    out = run(knowledge.list_gaps(who(), FakeDb([draft("Sunday opening hours")], dna=dna)))
    assert [g.gap for g in out] == ["Parking"]


def test_no_gaps_is_an_empty_list():
    assert run(knowledge.list_gaps(who(), FakeDb([], dna=None))) == []


# ── edit and switch off ───────────────────────────────────────────────────────

def test_an_item_can_be_edited_and_switched_off_but_not_another_businesses():
    it = item(content="short")
    run(knowledge.update_knowledge(it.id, knowledge.KnowledgePatch(content="a longer text " * 5, active=False), who(), FakeDb(item_by_id=it)))
    assert it.active is False and it.content.startswith("a longer text")
    other = item()
    other.business_id = uuid.uuid4()
    with pytest.raises(HTTPException) as e:
        run(knowledge.update_knowledge(other.id, knowledge.KnowledgePatch(active=False), who(), FakeDb(item_by_id=other)))
    assert e.value.status_code == 404


def test_the_showroom_collects_exchange_and_finance_details_before_handing_over():
    t = verticals.get("showroom_vehicle")
    policies = t["policies"].lower()
    for detail in ("kilometres driven", "number of owners", "fuel type", "photos", "salaried or self-employed", "down payment"):
        assert detail in policies, detail
    assert "never ask for a pan, aadhaar or bank account number" in policies
    assert "never value an old vehicle" in policies and "never promise that a loan will be approved" in policies
    # the hand-over now comes after the details, not before them
    assert all("once" in e for e in t["escalate_immediately"] if "old vehicle" in e or "loan" in e)
    # and those words still hold a reply for a person before it is sent
    assert {"exchange value", "loan approve"} <= set(t["escalate_keywords"])


def test_a_demo_vehicle_can_be_its_own_calendar():
    labels = verticals.get("showroom_vehicle")["labels"]["scheduling"]
    assert "demo vehicle" in labels["provider"] and "demo vehicles" in labels["provider_plural"]


def test_the_booking_checklist_is_tracked_item_by_item():
    notes = {w["kind"]: w["note"].lower() for w in verticals.get("showroom_vehicle")["watch_for"]}
    for doc in ("id proof", "address proof", "loan or income papers", "rc", "insurance policy", "registration"):
        assert doc in notes["document"], doc
    for step in ("booking amount", "down payment", "balance before delivery"):
        assert step in notes["payment"], step


def test_up_to_eight_calendars_are_shown_the_same_ones_every_time():
    import inspect
    assert context.MAX_DOCTORS_IN_CONTEXT == 8
    src = inspect.getsource(context)
    assert src.count(".order_by(Doctor.name)\n                .limit(MAX_DOCTORS_IN_CONTEXT)") == 2

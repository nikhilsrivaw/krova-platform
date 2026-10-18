"""
The four healthcare sub-types (docs/new/healthcare-research.md): what each switches on, the rules every one of them
carries (no medical advice, the NMC advertising guidelines, data care), and that they never ask patients for reviews.
"""

import asyncio
from types import SimpleNamespace

import pytest

from shared import verticals
from shared.ai import auto_send_gate
from shared.scheduling import recall

HEALTH = ["health_clinic", "health_hospital", "health_lab", "health_therapy"]


@pytest.mark.parametrize("key", HEALTH)
def test_each_is_offered_at_signup_under_healthcare(key):
    entry = next(v for v in verticals.available() if v["key"] == key)
    assert entry["group"] == "Healthcare"
    assert "Appointments & staff calendar" in entry["features"]


def test_the_main_market_types_have_no_group():
    assert all(v["group"] is None for v in verticals.available() if not v["key"].startswith("health_"))


def test_what_each_sub_type_switches_on():
    caps = {k: set(verticals.get(k)["capabilities"]) for k in HEALTH}
    assert {"scheduling", "opd_queue", "care_recall"} <= caps["health_clinic"]
    assert "tpa_claim_tracking" in caps["health_hospital"] and "tpa_claim_tracking" not in caps["health_clinic"]
    assert "opd_queue" not in caps["health_therapy"]


def test_the_legacy_clinic_stays_loadable_but_hidden():
    assert verticals.get("clinic")["key"] == "clinic"
    assert "clinic" not in {v["key"] for v in verticals.available()}


@pytest.mark.parametrize("key", HEALTH)
def test_every_sub_type_carries_the_medical_and_nmc_rules(key):
    p = verticals.get(key)["policies"].lower()
    for rule in (
        "never give medical advice", "never interpret a report", "never name a specific medical condition",
        "never recommend, sell or promote a medicine", "never offer, invent or hint at a discount",
        "never ask a patient for a review, rating or testimonial", "never ask for an id number",
        "never confirm a slot that is not free",
    ):
        assert rule in p, rule


@pytest.mark.parametrize("key", HEALTH)
def test_the_card_has_notices_not_offers(key):
    card = verticals.get(key)["knowledge_card"]
    assert card["offers"]["title"] == "Notices" and "no discounts or offers" in card["offers"]["question"].lower()
    assert "no patient reviews or testimonials" in card["trust"]["question"].lower()


# ── emergency words hand over; ordinary words do not ─────────────────────────

@pytest.mark.parametrize("key", HEALTH)
@pytest.mark.parametrize("message", [
    "papa ko chest pain ho raha hai", "bahut bleeding ho rahi hai", "wo behosh ho gaye", "accident hua hai",
    "is dawai ka side effect hai kya", "dose badha du?",
])
def test_emergency_and_medicine_messages_always_hold_for_a_person(key, message):
    assert auto_send_gate._contains_any(message, verticals.get(key)["escalate_keywords"])


@pytest.mark.parametrize("key", HEALTH)
@pytest.mark.parametrize("message", [
    "what are the benefits of the package", "particular test ka price?", "is the payment secure?",
    "report kab milega", "fasting chahiye kya", "kal ka session shift kar do", "token number kya hai",
    "doctor ki timing", "home collection Gomti Nagar mein hai?",
])
def test_ordinary_questions_do_not_trip_the_emergency_words(key, message):
    assert auto_send_gate._contains_any(message, verticals.get(key)["escalate_keywords"]) is None


def test_hospital_and_lab_specific_hand_overs():
    assert auto_send_gate._contains_any("icu bed milega?", verticals.get("health_hospital")["escalate_keywords"])
    assert auto_send_gate._contains_any("mera report normal hai kya", verticals.get("health_lab")["escalate_keywords"])


# ── no review requests for healthcare ────────────────────────────────────────

@pytest.mark.parametrize("key", HEALTH)
def test_healthcare_never_asks_for_reviews(key):
    assert verticals.get(key)["review_requests"] is False
    assert recall._asks_for_reviews(SimpleNamespace(vertical=key)) is False


def test_other_types_still_can():
    assert recall._asks_for_reviews(SimpleNamespace(vertical="local_service")) is True
    assert recall._asks_for_reviews(SimpleNamespace(vertical="no_such_type")) is True


def test_the_sweep_stamps_a_clinic_visit_but_sends_nothing(monkeypatch):
    clinic = SimpleNamespace(vertical="health_clinic", settings={"google_review_url": "https://g.page/x"})
    salon = SimpleNamespace(vertical="local_service", settings={"google_review_url": "https://g.page/y"})
    visits = [SimpleNamespace(business_id="c", customer_id="p1", review_requested_at=None),
              SimpleNamespace(business_id="s", customer_id="p2", review_requested_at=None)]
    sent_to = []

    class Db:
        def __init__(self):
            self.calls = 0

        async def execute(self, *_):
            self.calls += 1
            rows = [] if self.calls == 1 else visits  # first query: queue entries; second: appointments
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

        async def get(self, model, key):
            return {"c": clinic, "s": salon}.get(key) or SimpleNamespace(id=key)

    async def send(db, *, business, customer, review_url):
        sent_to.append(business.vertical)
        return True

    monkeypatch.setattr(recall.notify, "send_review_request", send)
    assert asyncio.run(recall.send_review_requests(Db())) == 1
    assert sent_to == ["local_service"]
    assert all(v.review_requested_at is not None for v in visits)


def test_leads_and_forms_are_off_by_default_for_healthcare_and_on_for_the_rest():
    from shared.verticals.capability_info import SWITCHABLE
    assert "lead_capture" in SWITCHABLE
    for key in verticals.keys():
        has = "lead_capture" in verticals.get(key).get("capabilities", [])
        assert has == (not key.startswith("health_")), key

"""Institutions: schools, colleges, universities, open and distance learning - its own business type."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared import verticals  # noqa: E402
from shared.ai import auto_send_gate  # noqa: E402
from shared.payments import fee_list  # noqa: E402
from shared.verticals import knowledge_card, labels  # noqa: E402

T = verticals.get("institution")


def test_it_can_be_chosen_on_its_own():
    row = next(v for v in verticals.available() if v["key"] == "institution")
    assert row["group"] is None and row["label"].startswith("Institution")
    assert "Bookings" in " ".join(row["features"]) or row["features"]


def test_what_it_starts_with():
    business = SimpleNamespace(vertical="institution", settings={})
    caps = verticals.capabilities_for(business)
    assert {"scheduling", "lead_capture", "care_recall"} <= set(caps)
    assert not fee_list.enabled(business)  # institutions take fees on their own portal; a business can switch it on
    assert "order_sync" not in caps


def test_it_speaks_of_counsellors_not_demo_classes():
    wording = labels.scheduling_labels(SimpleNamespace(vertical="institution", settings={}))
    assert wording["provider"] == "Counsellor" and wording["booking_noun"] == "counselling session"


def test_the_card_asks_what_an_admission_office_is_asked():
    fields = {f["key"]: f for f in knowledge_card.fields_for("institution")}
    assert fields["how_to_buy"]["title"] == "How to apply, eligibility and documents"
    assert "ABC-ID" in fields["how_to_buy"]["question"]
    assert fields["trust"]["title"] == "Recognition and approvals"
    assert "fee payment page" in fields["full_list"]["question"]


@pytest.mark.parametrize("words", ["admission", "placement", "scholarship", "UGC-DEB", "ABC-ID", "Aadhaar", "refund"])
def test_the_rules_cover_what_goes_wrong_in_admissions(words):
    assert words.lower() in T["policies"].lower()


def test_no_date_or_percentage_is_written_into_the_rules():
    """Last dates and refund percentages change every year - they come from the institution's own card."""
    import re

    assert not re.search(r"\d+\s*%|\b20\d\d\b|\bSeptember\b|\bOctober\b", T["policies"])


@pytest.mark.parametrize("message", [
    "fees wapas chahiye", "mujhe refund do", "admission cancel karna hai", "college mein ragging ho rahi hai",
    "ye fake admission lag raha hai", "फर्जी कॉलेज है क्या", "certificate nahi de rahe", "fees maaf kar do",
    "last date badha do", "agent ne extra paise liye",
])
def test_sensitive_messages_hold_for_a_person(message):
    assert auto_send_gate._contains_any(message, T["escalate_keywords"])


@pytest.mark.parametrize("message", [
    "BA ki fees kitni hai", "last date kya hai", "kaunse documents chahiye", "hostel hai kya",
    "counsellor se baat karni hai", "ABC ID kaise banaye", "exam kab hai", "aage kya karna hai", "scholarship milti hai kya",
    "college kitne baje khulta hai", "marksheet kaha jama karni hai",
])
def test_ordinary_admission_questions_do_not_trip_them(message):
    assert auto_send_gate._contains_any(message, T["escalate_keywords"]) is None

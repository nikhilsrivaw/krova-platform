"""
The 'People join and stay with you' sub-types: coaching, membership, stay (PG/hostel/library/coworking) and service
contracts, built on the programs template, with the old programs type kept as the group's catch-all.
"""

import pytest

from shared import verticals
from shared.ai import auto_send_gate

SUBTYPES = ["programs_coaching", "programs_membership", "programs_stay", "programs_service_contract"]
GROUP = "People join and stay with you"


def test_the_group_lists_the_subtypes_with_the_catch_all_last():
    group = [v["key"] for v in verticals.available() if v["group"] == GROUP]
    assert group[-1] == "programs" and set(group[:-1]) == set(SUBTYPES)
    assert verticals.get("programs")["label"] == "Something else that runs over time"


@pytest.mark.parametrize("key", SUBTYPES)
def test_every_subtype_keeps_the_programs_rules(key):
    t = verticals.get(key)
    assert t["policies"].startswith(verticals.get("programs")["policies"])
    assert "lead_capture" in t["capabilities"] and "scheduling" in t["capabilities"]
    assert "never promise or imply a result" in t["policies"].lower()


def test_what_each_subtype_switches_on():
    caps = {k: set(verticals.get(k)["capabilities"]) for k in SUBTYPES}
    assert "care_recall" in caps["programs_membership"] and "care_recall" in caps["programs_service_contract"]
    assert "quotations" in caps["programs_service_contract"]
    assert verticals.get("programs_coaching")["labels"]["scheduling"]["booking_noun"] == "demo class"
    assert verticals.get("programs_service_contract")["labels"]["scheduling"]["booking_noun"] == "service visit"


def test_coaching_follows_the_coaching_guidelines():
    p = verticals.get("programs_coaching")["policies"]
    assert "Ministry of Education's 2024 coaching guidelines" in p and "rank" in p
    assert "never promise a refund" in p


@pytest.mark.parametrize("key,message", [
    ("programs_stay", "room mein aag lag gayi"), ("programs_stay", "gas ki smell aa rahi hai"),
    ("programs_service_contract", "lift atak gayi andar log hai"), ("programs_service_contract", "AC se sparking ho rahi"),
    ("programs_membership", "gym mein chot lagi"), ("programs_coaching", "fees wapas chahiye"),
])
def test_safety_and_refund_messages_hold_for_a_person(key, message):
    assert auto_send_gate._contains_any(message, verticals.get(key)["escalate_keywords"])


@pytest.mark.parametrize("key", SUBTYPES + ["programs"])
@pytest.mark.parametrize("message", [
    "aage kya karna hai", "it is quite good", "batch timing kya hai", "library ka seat chahiye", "demo class kab hai",
])
def test_ordinary_messages_do_not_trip_them(key, message):
    assert auto_send_gate._contains_any(message, verticals.get(key)["escalate_keywords"]) is None

"""
What the AI is actually shown from the owner's knowledge: the Business Card as one block of exact facts in the card's
order, the Team Card, the top questions, other entries - on every channel's context, including scripted campaign calls.
"""

from types import SimpleNamespace

from shared.ai import context
from shared.db.models import KnowledgeKind
from shared.verticals import knowledge_card as kc


def _k(title, content, kind="other", topic=None, card=None):
    return {"title": title, "kind": kind, "content": content, "topic": topic, "card": card}


ITEMS = [
    _k("Opening hours", "Mon-Sat 10am to 7pm", "hours", topic="when"),
    _k("What you do", "Mobile repair shop in Lucknow.", "service", topic="about"),
    _k("Full list link", "https://example.com/prices.pdf", topic="full_list"),
    _k("Top offerings and prices", "Screen - Rs 1,500\nBattery - Rs 900", "price_list", topic="offerings"),
    _k("Our team", "How to reach us: call 98xxxxxx10", topic=kc.TEAM_TOPIC),
    _k("Do you do home pickup?", "Yes, within 5 km.", "faq", card="faq"),
    _k("Parking", "Free parking behind the shop.", "other"),
]


def test_card_is_one_block_in_card_order_with_exact_fact_rules():
    text = "\n".join(context.render_knowledge(ITEMS))
    assert "Quote prices, addresses, phone numbers, timings and links exactly" in text
    assert "do not guess" in text
    assert text.index("What you do:") < text.index("Opening hours:") < text.index("Top offerings and prices:") < text.index("Full list link:")
    assert "- Top offerings and prices: Screen - Rs 1,500\n  Battery - Rs 900" in text
    assert "send the full list link" in text


def test_team_faqs_and_other_entries_each_have_their_place():
    text = "\n".join(context.render_knowledge(ITEMS))
    assert "Team - who customers can reach" in text and "call 98xxxxxx10" in text
    assert "never say a person is free right now" in text
    assert "Q: Do you do home pickup?\nA: Yes, within 5 km." in text
    assert "Parking (use as reference):\nFree parking behind the shop." in text
    # card fields and FAQs are not repeated as loose entries
    assert "What you do (" not in text and "Do you do home pickup? (" not in text


def test_nothing_known_renders_nothing():
    assert context.render_knowledge([]) == []


def test_no_full_list_line_without_a_link():
    text = "\n".join(context.render_knowledge([_k("What you do", "Repairs.", topic="about")]))
    assert "full list link" not in text


def test_knowledge_dict_carries_topic_and_card():
    item = SimpleNamespace(title="Q", kind=KnowledgeKind.faq, content="A", topic=None, extra={"card": "faq"})
    assert context.knowledge_dict(item) == {"title": "Q", "kind": "faq", "content": "A", "topic": None, "card": "faq"}
    field = SimpleNamespace(title="Payment", kind=KnowledgeKind.policy, content="UPI", topic="payment", extra=None)
    assert context.knowledge_dict(field)["topic"] == "payment"


class _Business:
    """Just enough of an AgentContext for its business block: the knowledge, and nothing else set."""

    business_name = "Fix It"
    knowledge = ITEMS

    def __getattr__(self, name):
        return None


def test_the_customer_reply_context_shows_the_card():
    # the same business block text replies, voice calls, the web widget and copilot all read (render_business)
    text = "\n".join(context.AgentContext._business_lines(_Business()))
    assert text.startswith("You are answering on behalf of Fix It.")
    assert "- What you do: Mobile repair shop in Lucknow." in text
    assert "Q: Do you do home pickup?" in text


def test_a_scripted_campaign_call_can_answer_from_the_card():
    scripted = context.ScriptedContext(
        business_name="Fix It", purpose="survey", questions=["Which day suits you?"], knowledge=ITEMS,
    )
    text = scripted.render()
    assert "Which day suits you?" in text
    assert "answer briefly from these facts only" in text and "Screen - Rs 1,500" in text


def test_a_scripted_call_without_knowledge_is_unchanged():
    scripted = context.ScriptedContext(business_name="Fix It", purpose="survey", questions=["Q1"])
    assert "facts only" not in scripted.render()

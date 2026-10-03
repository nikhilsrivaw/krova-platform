"""
Compare a live-route model against Claude on labelled customer messages, for
the signals task (complaint, praise, churn risk, competitor mention, or none).

Run inside the app container:
    python scripts/signals_compare.py [provider:model]

Default candidate is bedrock:minimax.minimax-m2.5. Claude is the baseline on
the production model (speed="deep"). Score is the exact category match.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.ai import client, providers  # noqa: E402

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:minimax.minimax-m2.5"

CATEGORIES = ["complaint", "praise", "churn_risk", "competitor_mention", "none"]

TOOL = {
    "name": "record_signal",
    "description": "Record the single most important signal in the customer's message",
    "input_schema": {
        "type": "object",
        "properties": {"category": {"type": "string", "enum": CATEGORIES}},
        "required": ["category"],
    },
}

SYSTEM = (
    "A customer wrote to a small business. Classify the message into ONE category:\n"
    "- complaint: the customer is unhappy with a service, product, delay, or staff\n"
    "- praise: the customer is happy or thanks the business\n"
    "- churn_risk: the customer says they will stop using or going elsewhere\n"
    "- competitor_mention: the customer compares with another business or its price\n"
    "- none: a routine question, booking, or acknowledgement with no signal\n"
    "Use the tool."
)

CASES = [
    ("Bahut kharab service thi, paisa waapas chahiye", "complaint"),
    ("Bhai kya mast cut kiya, thanks!", "praise"),
    ("Agle baar kisi aur salon jaaunga, yeh mehenga hai", "churn_risk"),
    ("Kal kya timing hai?", "none"),
    ("Wahan Lakme mein 300 mein hota hai, aap 500 kyun le rahe ho", "competitor_mention"),
    ("Delivery 3 din late hai, ab tak kuch nahi aaya", "complaint"),
    ("Aap log best ho, family ko bhi bata diya", "praise"),
    ("Ab mujhe aapse koi kaam nahi karwana", "churn_risk"),
    ("Ok theek hai", "none"),
    ("Price list bhej do", "none"),
    ("Staff ne bahut rude baat ki", "complaint"),
    ("Competitor ka rate 200 hai aapse sasta", "competitor_mention"),
    ("Thank you so much, very happy", "praise"),
    ("Ab wapas nahi aaunga", "churn_risk"),
]


async def ask_claude(text: str) -> str | None:
    answer = await client.complete(
        system=SYSTEM, messages=[{"role": "user", "content": text}],
        speed="deep", max_tokens=200, task="signals_compare_claude", tool=TOOL,
    )
    return (answer.tool_input or {}).get("category")


async def ask_candidate(text: str) -> str | None:
    answer = await providers.call(
        CANDIDATE,
        {"system": SYSTEM, "messages": [{"role": "user", "content": text}], "max_tokens": 300, "tools": [TOOL],
         "tool_choice": {"type": "tool", "name": TOOL["name"]}},
    )
    return (answer.tool_input or {}).get("category")


async def main() -> None:
    claude_ok = candidate_ok = candidate_answered = 0
    print(f"\nCandidate: {CANDIDATE}\n")
    for text, expected in CASES:
        c = await ask_claude(text)
        m = await ask_candidate(text)
        claude_ok += c == expected
        candidate_answered += m is not None
        candidate_ok += m == expected
        print(f"- {text}")
        print(f"    expected={expected}  claude={c}  candidate={m}  {'OK' if m == expected else 'CHECK'}")

    n = len(CASES)
    print(f"\nScore (out of {n})")
    print(f"  claude     category {claude_ok}/{n}")
    print(f"  candidate  category {candidate_ok}/{n}  answered {candidate_answered}/{n}")


if __name__ == "__main__":
    asyncio.run(main())

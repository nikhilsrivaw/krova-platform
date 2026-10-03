"""
Compare a live-route model against Claude for escalation categorisation.

Run inside the app container:
    python scripts/categorize_compare.py [provider:model]

Default candidate is bedrock:minimax.minimax-m2.5. Claude is the baseline on the
production model (speed="fast") with the production SYSTEM prompt. Scoring mirrors
production: the reply is lowercased and stripped, and anything outside CATEGORIES
counts as "other".
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.ai import client, providers  # noqa: E402
from shared.ai.escalation_categorize import CATEGORIES, SYSTEM  # noqa: E402

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:minimax.minimax-m2.5"

# (escalation reason as the agent wrote it, expected category)
CASES = [
    ("Customer wants a refund for a service that was not delivered, I cannot process refunds", "billing"),
    ("Asked to move the appointment to Friday but no slots were shown to me", "booking"),
    ("Customer very angry about a damaged delivery, insists on talking to a manager", "complaint"),
    ("Could not understand the customer's request after three attempts", "technical"),
    ("Customer says they feel unsafe and mentions chest pain, asking if they should come in now", "urgent"),
    ("Customer asked for a discount on a bulk order of 200 units", "billing"),
    ("Booking calendar integration failed, the slot could not be confirmed", "technical"),
    ("Customer wants to cancel today's order and get the money back", "billing"),
    ("Customer says the staff was rude during the last visit and wants it addressed", "complaint"),
    ("Needs to reschedule the haircut appointment from 3pm to 5pm", "booking"),
    ("Asked about a political party, nothing to do with the business", "other"),
    ("Payment was received but the invoice still shows as pending", "billing"),
    ("Customer is threatening legal action over a charge on their card", "complaint"),
    ("Agent kept repeating the same reply and the customer gave up mid-conversation", "technical"),
    ("Customer wants a callback to discuss the quote for the kitchen renovation", "billing"),
    ("Wants to know if the clinic is open on Sunday for a same-day booking", "booking"),
]


async def ask_claude(reason: str) -> str:
    answer = await client.complete(
        system=SYSTEM, messages=[{"role": "user", "content": reason}],
        speed="fast", max_tokens=20, task="categorize_compare_claude",
    )
    return answer.text


async def ask_candidate(reason: str) -> str:
    answer = await providers.call(
        CANDIDATE,
        {"system": SYSTEM, "messages": [{"role": "user", "content": reason}], "max_tokens": 300},
    )
    return answer.text


def normalise(raw: str) -> str:
    cleaned = raw.strip().lower()
    return cleaned if cleaned in CATEGORIES else "other"


async def main() -> None:
    claude_ok = candidate_ok = candidate_invalid = 0
    print(f"\nCandidate: {CANDIDATE}\n")
    for reason, expected in CASES:
        c_raw = await ask_claude(reason)
        m_raw = await ask_candidate(reason)
        c, m = normalise(c_raw), normalise(m_raw)
        claude_ok += c == expected
        candidate_ok += m == expected
        candidate_invalid += m_raw.strip().lower() not in CATEGORIES
        print(f"- {reason}")
        print(f"    expected={expected}  claude={c}  candidate={m} ({m_raw.strip()!r})  "
              f"{'OK' if m == expected else 'CHECK'}")

    n = len(CASES)
    print(f"\nScore (out of {n})")
    print(f"  claude     correct {claude_ok}/{n}")
    print(f"  candidate  correct {candidate_ok}/{n}  invalid-word replies {candidate_invalid}/{n}")


if __name__ == "__main__":
    asyncio.run(main())

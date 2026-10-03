"""
Compare a live-route model against Claude for quotation extraction: did the
BUSINESS name a price in this message, and if so, what amount and item?

Run inside the app container:
    python scripts/quotation_compare.py [provider:model]

Default candidate is bedrock:minimax.minimax-m2.5. Claude is the baseline on
the production model (speed="deep"). A case is right when presence matches, and
when a price is present the amount matches too.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.ai import client, providers  # noqa: E402

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:minimax.minimax-m2.5"

TOOL = {
    "name": "record_quote",
    "description": "Record a price the business quoted to the customer. Set quoted=false if no price was quoted.",
    "input_schema": {
        "type": "object",
        "properties": {
            "quoted": {"type": "boolean"},
            "amount_rupees": {"type": "integer"},
            "item": {"type": "string"},
        },
        "required": ["quoted"],
    },
}

SYSTEM = (
    "A business replied to a customer. Decide whether the BUSINESS quoted a price in its message. "
    "quoted=true only when the business itself states a price or charge for something. "
    "A price the customer asks about, or a price the customer states, is quoted=false. "
    "If quoted=true, give the amount in rupees and the item. Use the tool."
)

# (business message, quoted, amount or None)
CASES = [
    ("Haircut 350 rupaye ka hai, color alag se", True, 350),
    ("Aapka order confirm ho gaya, kal aayega", False, None),
    ("Facial ka rate 1200 hai", True, 1200),
    ("Kitna lagega aapko?", False, None),
    ("Delivery charge 40 rupaye extra lagega", True, 40),
    ("Ji, samajh gaya, dhanyavaad", False, None),
    ("Pure set ka 4500 hoga", True, 4500),
    ("Kal 3 baje aa jaiye", False, None),
    ("Hamara consultation fee 500 hai", True, 500),
    ("Aap ka budget kitna hai?", False, None),
    ("Sofa cleaning 1800 mein hogi", True, 1800),
    ("Payment ka reminder bhej diya hai", False, None),
]


async def ask_claude(text: str) -> dict | None:
    answer = await client.complete(
        system=SYSTEM, messages=[{"role": "user", "content": text}],
        speed="deep", max_tokens=200, task="quotation_compare_claude", tool=TOOL,
    )
    return answer.tool_input


async def ask_candidate(text: str) -> dict | None:
    answer = await providers.call(
        CANDIDATE,
        {"system": SYSTEM, "messages": [{"role": "user", "content": text}], "max_tokens": 300, "tools": [TOOL],
         "tool_choice": {"type": "tool", "name": TOOL["name"]}},
    )
    return answer.tool_input


def correct(result: dict | None, quoted: bool, amount: int | None) -> bool:
    if not result or "quoted" not in result:
        return False
    if bool(result["quoted"]) != quoted:
        return False
    if quoted:
        return result.get("amount_rupees") == amount
    return True


async def main() -> None:
    claude_ok = candidate_ok = candidate_answered = 0
    print(f"\nCandidate: {CANDIDATE}\n")
    for text, quoted, amount in CASES:
        c = await ask_claude(text)
        m = await ask_candidate(text)
        claude_ok += correct(c, quoted, amount)
        candidate_answered += m is not None
        candidate_ok += correct(m, quoted, amount)
        print(f"- {text}")
        print(f"    expected quoted={quoted} amount={amount}")
        print(f"    claude:    {c}  {'OK' if correct(c, quoted, amount) else 'CHECK'}")
        print(f"    candidate: {m}  {'OK' if correct(m, quoted, amount) else 'CHECK'}")

    n = len(CASES)
    print(f"\nScore (out of {n})")
    print(f"  claude     correct {claude_ok}/{n}")
    print(f"  candidate  correct {candidate_ok}/{n}  answered {candidate_answered}/{n}")


if __name__ == "__main__":
    asyncio.run(main())

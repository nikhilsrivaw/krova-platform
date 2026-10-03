"""
Compare a live-route model against Claude on labelled commitment sentences.

Run inside the app container:
    python scripts/route_compare.py [provider:model]

Default candidate is bedrock:minimax.minimax-m2.5. Claude is the baseline,
run on the same production model (speed="deep") it uses today. Each sentence
is scored on amount, currency, status, and direction where the label is clear.
"""

import asyncio
import sys

from shared.ai import client, providers

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:minimax.minimax-m2.5"

TOOL = {
    "name": "record_commitment",
    "description": "Record the money commitment described in the message",
    "input_schema": {
        "type": "object",
        "properties": {
            "amount_rupees": {"type": "integer"},
            "currency": {"type": "string"},
            "direction": {"type": "string", "enum": ["they_owe", "we_owe"]},
            "status": {"type": "string", "enum": ["pending", "already_paid"]},
        },
        "required": ["amount_rupees", "currency", "direction", "status"],
    },
}

SYSTEM = (
    "A business owner and a customer chat about money. Extract ONE commitment from the message. "
    "direction: they_owe = the customer owes the business; we_owe = the business owes the customer. "
    "status: already_paid if the money has already moved, otherwise pending. Use the tool."
)

# (sentence, amount, status, direction or None when the direction is not clear from the text)
CASES = [
    ("Ramesh ne kal 500 rupaye dene hain", 500, "pending", "they_owe"),
    ("Priya 1200 Rs. Friday tak bhejegi", 1200, "pending", "they_owe"),
    ("Main tujhe 75 rupaye kal de dunga", 75, "pending", "we_owe"),
    ("Maine 500 transfer kar diya aaj", 500, "already_paid", "we_owe"),
    ("Customer ne 1500 cash diya", 1500, "already_paid", "they_owe"),
    ("Dukan ka rent 12000 dene hain 1 tarikh ko", 12000, "pending", "we_owe"),
    ("Usne 250 abhi tak nahi diye", 250, "pending", "they_owe"),
    ("Payment ho gaya 999 ka", 999, "already_paid", None),
    ("Do sau rupaye de dena kal", 200, "pending", None),
    ("Kal 50 extra dena", 50, "pending", None),
    ("Mujhe 300 ka refund chahiye", 300, "pending", "we_owe"),
    ("Advance 2000 de diya, baki kal", 2000, "already_paid", None),
    ("Aaj 800 bhej dunga", 800, "pending", "we_owe"),
    ("Sharma ji ne 600 ka order diya, payment baad mein", 600, "pending", "they_owe"),
]


async def ask_claude(sentence: str) -> dict | None:
    answer = await client.complete(
        system=SYSTEM, messages=[{"role": "user", "content": sentence}],
        speed="deep", max_tokens=300, task="route_compare_claude", tool=TOOL,
    )
    return answer.tool_input


async def ask_candidate(sentence: str) -> dict | None:
    answer = await providers.call(
        CANDIDATE,
        {"system": SYSTEM, "messages": [{"role": "user", "content": sentence}], "max_tokens": 300, "tools": [TOOL],
         "tool_choice": {"type": "tool", "name": TOOL["name"]}},
    )
    data = answer.tool_input or None
    if data and "currency" in data:
        data["currency"] = client._normalise_currency(data["currency"])
    return data


def score(result: dict | None, amount: int, status: str, direction: str | None) -> dict[str, bool]:
    if not result:
        return {"amount": False, "status": False, "direction": False if direction else True, "answered": False}
    return {
        "amount": result.get("amount_rupees") == amount,
        "status": result.get("status") == status,
        "direction": True if direction is None else result.get("direction") == direction,
        "answered": True,
    }


async def main() -> None:
    totals = {"claude": {}, "candidate": {}}
    rows = []
    for sentence, amount, status, direction in CASES:
        c_res = await ask_claude(sentence)
        m_res = await ask_candidate(sentence)
        c_score = score(c_res, amount, status, direction)
        m_score = score(m_res, amount, status, direction)
        for label, sc in (("claude", c_score), ("candidate", m_score)):
            for key, ok in sc.items():
                totals[label][key] = totals[label].get(key, 0) + (1 if ok else 0)
        rows.append((sentence, c_res, m_res, c_score, m_score))

    print(f"\nCandidate: {CANDIDATE}\n")
    for sentence, c_res, m_res, c_score, m_score in rows:
        print(f"- {sentence}")
        print(f"    claude:    {c_res}  {'OK' if all(c_score.values()) else 'CHECK'}")
        print(f"    candidate: {m_res}  {'OK' if all(m_score.values()) else 'CHECK'}")

    n = len(CASES)
    print("\nScore (out of", n, ")")
    for label in ("claude", "candidate"):
        t = totals[label]
        print(f"  {label:10} amount {t.get('amount', 0)}/{n}  status {t.get('status', 0)}/{n}  "
              f"direction {t.get('direction', 0)}/{n}  answered {t.get('answered', 0)}/{n}")


if __name__ == "__main__":
    asyncio.run(main())

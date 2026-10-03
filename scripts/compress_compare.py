"""
Compare a live-route model against Claude for customer profile compression.

Run inside the app container:
    python scripts/compress_compare.py [provider:model]

Default candidate is bedrock:minimax.minimax-m2.5. Claude is the baseline on the
production path (compression.compress, speed="deep"). Each case has a hand-set
health-score band and phrases the summary must not claim (invented patterns) or
must mention (outstanding items). The summaries are printed too, because wording
quality matters and no string check captures it.
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.ai import client, compression, providers  # noqa: E402

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:minimax.minimax-m2.5"
NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
BUSINESS = "Beauty salon in Lajpat Nagar, Delhi. Haircut, facial, colour, monthly packages."


def msg(direction: str, days_ago: int, text: str, channel: str = "whatsapp") -> dict:
    return {
        "id": uuid.uuid4(),
        "direction": direction,
        "occurred_at": NOW - timedelta(days=days_ago),
        "channel": channel,
        "text": text,
    }


CASES = [
    {
        "customer": "Priya",
        "first_seen": NOW - timedelta(days=200),
        "messages": [
            msg("inbound", 170, "Hi, facial ka time milega?"),
            msg("outbound", 170, "Ji, kal 4 baje aa jaiye"),
            msg("inbound", 120, "Facial bahut achha laga, next month bhi"),
            msg("outbound", 120, "Thanks Priya!"),
            msg("inbound", 60, "Monthly package ka rate kya hai?"),
            msg("outbound", 60, "Package 2400 ka hai, 4 sitting"),
            msg("inbound", 58, "Done, payment kar diya"),
            msg("inbound", 30, "Next week ka slot chahiye"),
            msg("outbound", 30, "Ji, Thursday 11 baje"),
        ],
        "commitments": [],
        "band": (70, 100),
        "forbidden": ["unpaid", "overdue"],
        "must": [],
    },
    {
        "customer": "Ramesh",
        "first_seen": NOW - timedelta(days=150),
        "messages": [
            msg("inbound", 140, "Bhai order chahiye, 1500 ka"),
            msg("outbound", 140, "Ji, 1500 ka bill bhej raha hoon"),
            msg("inbound", 120, "Kal de dunga"),
            msg("outbound", 110, "Ramesh ji, 1500 abhi bhi pending hai"),
            msg("inbound", 100, "Agle hafte pakka"),
            msg("outbound", 80, "Aap ne 1500 ka promise kiya tha"),
            msg("inbound", 75, "Haan, dekhta hoon, paisa tight hai"),
            msg("inbound", 40, "Kal pakka de dunga"),
            msg("outbound", 20, "Payment ka reminder bhej diya"),
            msg("inbound", 19, "Abhi nahi ho paayega"),
        ],
        "commitments": [
            {"direction": "they_owe", "description": "Ramesh's order payment", "amount": "1500 rupees",
             "due": "30 days ago", "status": "open"},
        ],
        "band": (0, 35),
        "forbidden": ["pays on time", "always pays", "is reliable"],
        "must": ["1500"],
    },
    {
        "customer": None,
        "first_seen": NOW - timedelta(days=2),
        "messages": [
            msg("inbound", 2, "Hi, timing kya hai?"),
            msg("outbound", 2, "Hum 10 se 8 tak open hain"),
            msg("inbound", 1, "Achha, kal aata hoon"),
            msg("outbound", 1, "Ji, welcome"),
        ],
        "commitments": [],
        "band": (40, 60),
        "forbidden": ["always", "loyal", "reliable", "pays"],
        "must": [],
    },
    {
        "customer": "Sharma ji",
        "first_seen": NOW - timedelta(days=90),
        "messages": [
            msg("inbound", 60, "Bahut kharab cut kiya, mujhe pasand nahi aaya"),
            msg("outbound", 60, "Sorry, hum theek karenge, kal aao"),
            msg("inbound", 59, "Theek hai"),
            msg("outbound", 59, "Free touch-up kar diya, ab satisfied?"),
            msg("inbound", 58, "Haan, ab theek hai, thanks"),
            msg("inbound", 20, "Next month ka appointment book karna hai"),
            msg("outbound", 20, "Ji, 15 tarikh ka slot khali hai"),
            msg("inbound", 19, "Done"),
        ],
        "commitments": [],
        "band": (45, 75),
        "forbidden": ["difficult"],
        "must": [],
    },
    {
        "customer": "Anita",
        "first_seen": NOW - timedelta(days=120),
        "messages": [
            msg("inbound", 110, "Kitna lagega haircut?"),
            msg("outbound", 110, "Haircut 350"),
            msg("inbound", 109, "400 mein karoge?"),
            msg("outbound", 109, "Ji nahi, 350 hi"),
            msg("inbound", 80, "Chalo theek, 350 pe aata hoon"),
            msg("inbound", 50, "Color bhi discount mein?"),
            msg("outbound", 50, "Color alag hai, 800"),
            msg("inbound", 49, "Pehle 500 bolo"),
            msg("inbound", 30, "Paid, abhi transfer kiya"),
        ],
        "commitments": [],
        "band": (40, 70),
        "forbidden": ["bad customer", "always pays"],
        "must": [],
    },
    {
        "customer": "Meena",
        "first_seen": NOW - timedelta(days=60),
        "messages": [
            msg("inbound", 40, "Mera monthly package cancel karna hai"),
            msg("outbound", 40, "Ji, cancel kar diya"),
            msg("inbound", 38, "Paise kab waapas milenge? 1200 ka tha"),
            msg("outbound", 38, "Refund 2 din mein bhej denge"),
            msg("inbound", 25, "Abhi tak nahi aaya"),
            msg("outbound", 24, "Sorry, aaj hi bhejte hain"),
        ],
        "commitments": [
            {"direction": "we_owe", "description": "Refund for cancelled monthly package",
             "amount": "1200 rupees", "due": "", "status": "open"},
        ],
        "band": (25, 60),
        "forbidden": ["always pays", "is reliable"],
        "must": ["refund"],
    },
    {
        "customer": "Vikram",
        "first_seen": NOW - timedelta(days=40),
        "messages": [
            msg("inbound", 30, "Order ke 900 bhej diye hain"),
            msg("outbound", 30, "Ok, check karta hoon"),
            msg("inbound", 29, "Dekho, 900 transfer kar diya"),
            msg("inbound", 10, "Kab delivery hogi?"),
            msg("outbound", 10, "Kal tak"),
        ],
        "commitments": [
            {"direction": "they_owe", "description": "Vikram's order payment", "amount": "900 rupees",
             "due": "", "status": "open"},
        ],
        "band": (35, 70),
        "forbidden": ["payment received", "payment came through", "has paid", "paid in full"],
        "must": [],
    },
    {
        "customer": "Neha",
        "first_seen": NOW - timedelta(days=180),
        "messages": [
            msg("inbound", 170, "Order 1200 ka chahiye"),
            msg("outbound", 170, "Bill bhej diya, UPI se kar dijiye"),
            msg("inbound", 168, "Kar diya"),
            msg("outbound", 168, "Payment mil gaya, thanks!"),
            msg("inbound", 90, "Next order bhi 1500 ka"),
            msg("outbound", 90, "Bill bhej diya"),
            msg("inbound", 88, "Paid"),
            msg("outbound", 88, "Confirm ho gaya"),
            msg("inbound", 20, "Ek aur order de do"),
        ],
        "commitments": [],
        "band": (70, 95),
        "forbidden": ["unpaid", "overdue"],
        "must": [],
    },
    {
        "customer": None,
        "first_seen": NOW - timedelta(days=5),
        "messages": [
            msg("inbound", 5, "Price list bhej do"),
            msg("outbound", 5, "Bheja hai"),
            msg("inbound", 4, "Thanks"),
            msg("inbound", 4, "Dekhta hoon"),
        ],
        "commitments": [],
        "band": (40, 60),
        "forbidden": ["always", "loyal", "reliable"],
        "must": [],
    },
    {
        "customer": "Deepak",
        "first_seen": NOW - timedelta(days=100),
        "messages": [
            msg("inbound", 95, "Bill mein 300 extra kyun laga?"),
            msg("outbound", 95, "Service charge hai"),
            msg("inbound", 94, "Pehle kabhi nahi tha, bahut overcharge kiya"),
            msg("inbound", 60, "Dobara nahi aaunga agar yahi rate rahega"),
            msg("outbound", 58, "Sorry, next time check karenge"),
            msg("inbound", 57, "Theek hai"),
        ],
        "commitments": [],
        "band": (10, 35),
        "forbidden": ["loyal", "good customer"],
        "must": ["overcharg"],
    },
    {
        "customer": "Kavita",
        "first_seen": NOW - timedelta(days=300),
        "messages": [
            msg("inbound", 260, "Wedding package ke liye deposit kitna hai?"),
            msg("outbound", 260, "5000 deposit, baaki event se pehle"),
            msg("inbound", 255, "Deposit de diya"),
            msg("outbound", 255, "Mil gaya, thanks"),
            msg("inbound", 40, "Event cancel ho gaya, deposit wapas chahiye"),
            msg("outbound", 38, "Ok, refund process karte hain"),
            msg("inbound", 20, "Abhi tak nahi aaya"),
        ],
        "commitments": [
            {"direction": "we_owe", "description": "Deposit refund for cancelled wedding package",
             "amount": "5000 rupees", "due": "", "status": "open"},
        ],
        "band": (25, 55),
        "forbidden": ["always pays"],
        "must": ["deposit"],
    },
    {
        "customer": "Arjun",
        "first_seen": NOW - timedelta(days=80),
        "messages": [
            msg("outbound", 60, "Offer hai, 20% off is hafte"),
            msg("outbound", 45, "Follow up: offer abhi bhi hai"),
            msg("outbound", 30, "Kya hua? Koi reply nahi"),
            msg("outbound", 15, "Last reminder, offer khatam ho raha hai"),
        ],
        "commitments": [],
        "band": (5, 30),
        "forbidden": ["loyal", "reliable"],
        "must": [],
    },
    {
        "customer": "Sunita",
        "first_seen": NOW - timedelta(days=60),
        "messages": [
            msg("inbound", 58, "Hi, do you do bridal makeup?"),
            msg("outbound", 58, "Yes, starting 8000"),
            msg("inbound", 40, "Can I book for 15 Nov?"),
            msg("outbound", 40, "Yes, slot available"),
            msg("inbound", 35, "Advance 3000 sent on UPI, screenshot shared"),
            msg("outbound", 35, "Received, thanks"),
        ],
        "commitments": [],
        "band": (60, 85),
        "forbidden": ["unpaid", "owes"],
        "must": [],
    },
    {
        "customer": "Tara",
        "first_seen": NOW - timedelta(days=50),
        "messages": [
            msg("inbound", 45, "Maine 500 diye the, record mein kyun nahi?"),
            msg("outbound", 45, "Check karke batata hoon"),
            msg("inbound", 40, "Abhi tak check nahi hua?"),
            msg("outbound", 38, "Abhi dekh rahe hain"),
        ],
        "commitments": [],
        "band": (25, 50),
        "forbidden": ["payment received", "confirmed payment", "has paid"],
        "must": [],
    },
]


def build_prompt(case: dict) -> str:
    since = case["first_seen"].strftime("%B %Y")
    usable = [m for m in case["messages"] if (m.get("text") or "").strip()]
    return (
        f"Business: {BUSINESS}\n"
        f"Customer: {case['customer'] or 'name unknown'}\n"
        f"First contact: {since}\n"
        f"Today: {NOW.strftime('%d %B %Y')}\n"
        f"Messages exchanged: {len(usable)}\n\n"
        f"{compression._render(usable, case['commitments'], NOW)}\n\n"
        "Write the note."
    )


async def ask_claude(case: dict) -> tuple[str, int | None]:
    profile = await compression.compress(
        messages=case["messages"], commitments=case["commitments"], customer_name=case["customer"],
        first_seen=case["first_seen"], business_context=BUSINESS, now=NOW,
    )
    if profile is None:
        return "", None
    return profile.summary, profile.health_score


async def ask_candidate(case: dict) -> tuple[str, int | None]:
    answer = await providers.call(
        CANDIDATE,
        {"system": compression.SYSTEM, "messages": [{"role": "user", "content": build_prompt(case)}],
         "max_tokens": 1024, "tools": [compression.SUMMARY_TOOL],
         "tool_choice": {"type": "tool", "name": compression.SUMMARY_TOOL["name"]}},
    )
    data = answer.tool_input or {}
    summary = (data.get("summary") or "").strip()
    try:
        score = int(data.get("health_score"))
    except (TypeError, ValueError):
        score = None
    return summary, score


def check(case: dict, summary: str, score: int | None) -> tuple[bool, list[str]]:
    lowered = summary.lower()
    problems: list[str] = []
    if not summary:
        return False, ["no summary"]
    lo, hi = case["band"]
    if score is None or not (lo <= score <= hi):
        problems.append(f"score {score} outside {lo}-{hi}")
    problems += [f"claims '{w}'" for w in case["forbidden"] if w in lowered]
    problems += [f"missing '{w}'" for w in case["must"] if w not in lowered]
    return not problems, problems


async def main() -> None:
    claude_ok = candidate_ok = candidate_answered = 0
    print(f"\nCandidate: {CANDIDATE}\n")
    for case in CASES:
        c_sum, c_score = await ask_claude(case)
        m_sum, m_score = await ask_candidate(case)
        c_ok, c_problems = check(case, c_sum, c_score)
        m_ok, m_problems = check(case, m_sum, m_score)
        claude_ok += c_ok
        candidate_ok += m_ok
        candidate_answered += bool(m_sum)
        print(f"- {case['customer'] or 'unnamed'} (band {case['band'][0]}-{case['band'][1]})")
        print(f"    claude:    score={c_score} {'OK' if c_ok else 'CHECK ' + '; '.join(c_problems)}")
        print(f"               {c_sum}")
        print(f"    candidate: score={m_score} {'OK' if m_ok else 'CHECK ' + '; '.join(m_problems)}")
        print(f"               {m_sum}")

    n = len(CASES)
    print(f"\nScore (out of {n})")
    print(f"  claude     ok {claude_ok}/{n}")
    print(f"  candidate  ok {candidate_ok}/{n}  answered {candidate_answered}/{n}")


if __name__ == "__main__":
    asyncio.run(main())

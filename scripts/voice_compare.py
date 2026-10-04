"""
Compare a live-route model against Haiku for the voice reply decision.

Run inside the app container:
    python scripts/voice_compare.py [provider:model]

Default candidate is bedrock:minimax.minimax-m2.5. Baseline is Haiku, the fast
Claude model voice uses in production (speed="fast"), with agent.SYSTEM_STREAM.
Both are timed as a full response, not first token, so the comparison is fair
but the numbers are an upper bound on time to the caller's first word.
"""

import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.ai import agent, client, providers  # noqa: E402

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:minimax.minimax-m2.5"

BUSINESS = (
    "Glow Salon, Lajpat Nagar. Open 10 AM to 8 PM every day. "
    "Haircut Rs 350, facial Rs 1200, colour Rs 800. Bookings are taken on the phone or WhatsApp."
)

# (customer's latest words on the call, expected action)
CASES = [
    ("Kal 5 baje ka slot milega?", "reply"),
    ("Haircut ka rate kya hai?", "reply"),
    ("Mera paisa wapas do warna main legal notice bhejunga", "escalate"),
    ("Thanks, bye", "no_action"),
    ("Facial ke baad skin pe rash aa gaya, bahut jalan ho rahi hai", "escalate"),
    ("Ok", "no_action"),
    ("Kya aap Sunday ko khule ho?", "reply"),
    ("Mujhe refund chahiye, aapne galat service di", "escalate"),
    ("Achha theek hai, dhanyavaad", "no_action"),
    ("Kal ka appointment confirm hai na?", "escalate"),
    ("Mera kal ka appointment cancel kar do", "escalate"),
    ("Payment mil gaya na mera?", "escalate"),
    ("Mera order kab aayega?", "escalate"),
]

ACTIONS = {"REPLY": "reply", "ESCALATE": "escalate", "NOACTION": "no_action"}


def build_messages(customer_text: str) -> list[dict]:
    prompt = (
        f"{BUSINESS}\n\n"
        f"Today is {agent.ctx.now_line()}.\n\n"
        f"Conversation:\nCustomer: {customer_text}\n\n"
        "Decide how to handle the customer's most recent message."
    )
    return [{"role": "user", "content": prompt}]


def parse(text: str) -> tuple[str | None, str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return None, ""
    word = lines[0].upper().rstrip(".:")
    action = ACTIONS.get(word)
    reply = " ".join(lines[1:])
    return action, reply


async def ask_haiku(customer_text: str) -> tuple[str | None, str, float]:
    start = time.perf_counter()
    answer = await client.complete(
        system=agent.SYSTEM_STREAM, messages=build_messages(customer_text),
        speed="fast", max_tokens=200, task="voice_compare_claude",
    )
    elapsed = time.perf_counter() - start
    action, reply = parse(answer.text)
    return action, reply, elapsed


async def ask_candidate(customer_text: str) -> tuple[str | None, str, float]:
    start = time.perf_counter()
    answer = await providers.call(
        CANDIDATE,
        {"system": agent.SYSTEM_STREAM, "messages": build_messages(customer_text), "max_tokens": 300},
    )
    elapsed = time.perf_counter() - start
    action, reply = parse(answer.text)
    return action, reply, elapsed


async def main() -> None:
    haiku_ok = cand_ok = cand_answered = 0
    haiku_times: list[float] = []
    cand_times: list[float] = []
    print(f"\nCandidate: {CANDIDATE}\n")
    for text, expected in CASES:
        h_action, h_reply, h_t = await ask_haiku(text)
        try:
            m_action, m_reply, m_t = await ask_candidate(text)
        except Exception as exc:  # noqa: BLE001 - a failed call is a result, not a crash
            m_action, m_reply, m_t = None, f"ERROR {type(exc).__name__}: {exc}", 0.0
        haiku_ok += h_action == expected
        cand_ok += m_action == expected
        cand_answered += m_action is not None
        haiku_times.append(h_t)
        if m_t:
            cand_times.append(m_t)
        print(f"- {text}  (expected {expected})")
        print(f"    haiku:     {h_action} {h_t:.2f}s  {'OK' if h_action == expected else 'CHECK'}  {h_reply}")
        print(f"    candidate: {m_action} {m_t:.2f}s  {'OK' if m_action == expected else 'CHECK'}  {m_reply}")

    n = len(CASES)
    print(f"\nScore (out of {n})")
    print(f"  haiku      action {haiku_ok}/{n}  median {statistics.median(haiku_times):.2f}s")
    med = statistics.median(cand_times) if cand_times else float("nan")
    print(f"  candidate  action {cand_ok}/{n}  answered {cand_answered}/{n}  median {med:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())

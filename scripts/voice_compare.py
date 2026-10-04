"""
Compare a live-route model against Haiku for the voice reply decision.

Run inside the app container:
    python scripts/voice_compare.py [provider:model]

Baseline is Haiku, the fast Claude model voice uses in production (speed="fast"),
with agent.SYSTEM_STREAM. The candidate runs buffered, through the voice guard in
shared/ai/voice_guard.py: if its reply states a fact the business never gave,
the reply is replaced by Haiku's. Both are timed as a full response, not first
token, so the numbers are an upper bound on time to the caller's first word.
"""

import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.ai import agent, client, providers, voice_guard  # noqa: E402

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:minimax.minimax-m2.5"

BUSINESS = (
    "Glow Salon, Lajpat Nagar. Open 10 AM to 8 PM every day. "
    "Haircut Rs 350, facial Rs 1200, colour Rs 800. Bookings are taken on the phone or WhatsApp."
)

NOT_PROVIDED = (
    "\nNot provided to you on this call: slot availability, any customer's booking, order or payment record, "
    "the salon's address, parking, and payment methods. "
    "Never state a slot, booking, order, payment status or any of these details unless written above."
)

# (customer's latest words on the call, expected action)
CASES = [
    # Grounded facts: answer them
    ("Haircut ka rate kya hai?", "reply"),
    ("Facial kitne ka hai?", "reply"),
    ("Colour ka kya charge hai?", "reply"),
    ("Subah kab khulte ho?", "reply"),
    ("Raat ko kitne baje tak khule ho?", "reply"),
    ("Kya aap Sunday ko khule ho?", "reply"),
    ("Booking kaise karni hai?", "reply"),
    ("Bhaiya haircut ka kitna lagega?", "reply"),
    ("Ji, facial book karni hai", "reply"),
    ("Hello? sun rahe ho?", "reply"),
    # Not in the facts: escalate
    ("Aap log kahan ho?", "escalate"),
    ("Kya aap card accept karte ho?", "escalate"),
    ("Parking hai kya?", "escalate"),
    # Slots, bookings, payments, orders: escalate, never invent
    ("Kal 5 baje ka slot milega?", "escalate"),
    ("Aaj shaam ka slot khali hai kya?", "escalate"),
    ("Kal ka appointment confirm hai na?", "escalate"),
    ("Mera kal ka appointment cancel kar do", "escalate"),
    ("Mere naam se booking hai kya?", "escalate"),
    ("Payment mil gaya na mera?", "escalate"),
    ("Maine kal 500 transfer kiye the, mila?", "escalate"),
    ("Mera order kab aayega?", "escalate"),
    ("Order ka status batao", "escalate"),
    # Refund, legal, complaint, safety: escalate
    ("Mujhe refund chahiye, aapne galat service di", "escalate"),
    ("Mera paisa wapas do warna main legal notice bhejunga", "escalate"),
    ("Facial ke baad skin pe rash aa gaya, bahut jalan ho rahi hai", "escalate"),
    ("Staff ne bahut badtameezi se baat ki", "escalate"),
    # Pleasantries: no reply needed
    ("Thanks, bye", "no_action"),
    ("Ok", "no_action"),
    ("Achha theek hai, dhanyavaad", "no_action"),
    ("Shukriya", "no_action"),
]

ACTIONS = {"REPLY": "reply", "ESCALATE": "escalate", "NOACTION": "no_action"}

EXAMPLE = (
    "Example of a wrong answer to avoid: customer asks for a slot, you say "
    "\"Tomorrow 5 PM khatam ho gaya\" - no slot data was given, so that is a fabrication. "
    "The right output is ESCALATE with the missing fact.\n\n"
)


def build_messages(customer_text: str) -> list[dict]:
    prompt = (
        f"{BUSINESS}{NOT_PROVIDED}\n\n"
        f"Today is {agent.ctx.now_line()}.\n\n"
        f"{EXAMPLE}"
        f"Conversation:\nCustomer: {customer_text}\n\n"
        "Decide how to handle the customer's most recent message."
    )
    return [{"role": "user", "content": prompt}]


def parse(text: str) -> tuple[str | None, str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return None, ""
    word = lines[0].upper().rstrip(".:")
    return ACTIONS.get(word), " ".join(lines[1:])


async def ask_haiku(customer_text: str) -> tuple[str | None, str, float]:
    start = time.perf_counter()
    answer = await client.complete(
        system=agent.SYSTEM_STREAM, messages=build_messages(customer_text),
        speed="fast", max_tokens=200, task="voice_compare_claude",
    )
    elapsed = time.perf_counter() - start
    action, reply = parse(answer.text)
    return action, reply, elapsed


async def ask_candidate(customer_text: str) -> tuple[str | None, str, float, bool]:
    start = time.perf_counter()
    answer = await providers.call(
        CANDIDATE,
        {"system": agent.SYSTEM_STREAM, "messages": build_messages(customer_text), "max_tokens": 300},
    )
    action, reply = parse(answer.text)
    if action == "reply" and voice_guard.ungrounded_claims(reply, BUSINESS):
        fb_action, fb_reply, _ = await ask_haiku(customer_text)
        return fb_action, f"[guard -> haiku] {fb_reply}", time.perf_counter() - start, True
    return action, reply, time.perf_counter() - start, False


async def main() -> None:
    haiku_ok = cand_ok = cand_answered = fallbacks = 0
    haiku_times: list[float] = []
    cand_times: list[float] = []
    print(f"\nCandidate: {CANDIDATE} (buffered, with guard)\n")
    for text, expected in CASES:
        h_action, h_reply, h_t = await ask_haiku(text)
        try:
            m_action, m_reply, m_t, fired = await ask_candidate(text)
        except Exception as exc:  # noqa: BLE001 - a failed call is a result, not a crash
            m_action, m_reply, m_t, fired = None, f"ERROR {type(exc).__name__}: {exc}", 0.0, False
        haiku_ok += h_action == expected
        cand_ok += m_action == expected
        cand_answered += m_action is not None
        fallbacks += fired
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
    print(f"  candidate  action {cand_ok}/{n}  answered {cand_answered}/{n}  "
          f"median {med:.2f}s  guard fallbacks {fallbacks}")


if __name__ == "__main__":
    asyncio.run(main())

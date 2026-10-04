"""
Voice answers grounded in a business knowledge base: should it answer or escalate?

Run inside the app container:
    python scripts/knowledge_voice_compare.py [provider:model]

The business details here are a summary of Aqirox's own knowledge base. A question the
knowledge answers should get a REPLY; one it does not answer should get an ESCALATE.
Haiku is the baseline. Score is the action only.
"""

import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.ai import agent, client, providers  # noqa: E402

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:moonshotai.kimi-k2.5"

KNOWLEDGE = (
    "Aqirox. Certified Meta Technical Partner. Services: Websites and web apps (marketing sites, "
    "client portals, e-commerce, AI chat widgets, WhatsApp and CRM lead capture). Mobile apps for "
    "iOS and Android (push notifications, offline sync). AI automation (WhatsApp and Instagram "
    "agents). Voice agents (phone calls that answer and book for a business). Contact us through "
    "the website for a consultation."
)

CASES = [
    ("Aqirox kya kya services deta hai?", "reply"),
    ("Aap websites banate ho?", "reply"),
    ("Voice agent kya hota hai?", "reply"),
    ("Kya aap mobile app bana sakte ho?", "reply"),
    ("Aapka office kahan hai?", "escalate"),
    ("Mere account ka balance kitna hai?", "escalate"),
]

ACTIONS = {"REPLY": "reply", "ESCALATE": "escalate", "NOACTION": "no_action"}


def build_messages(customer_text: str) -> list[dict]:
    prompt = (
        f"{KNOWLEDGE}\n\n"
        f"Today is {agent.ctx.now_line()}.\n\n"
        f"Conversation:\nCustomer: {customer_text}\n\n"
        "Decide how to handle the customer's most recent message."
    )
    return [{"role": "user", "content": prompt}]


def parse(text: str) -> str | None:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return ACTIONS.get(lines[0].upper().rstrip(".:")) if lines else None


async def ask_haiku(text: str) -> tuple[str | None, str, float]:
    start = time.perf_counter()
    answer = await client.complete(
        system=agent.SYSTEM_STREAM, messages=build_messages(text),
        speed="fast", max_tokens=200, task="knowledge_voice_compare_claude",
    )
    return parse(answer.text), answer.text, time.perf_counter() - start


async def ask_candidate(text: str) -> tuple[str | None, str, float]:
    start = time.perf_counter()
    answer = await providers.call(
        CANDIDATE, {"system": agent.SYSTEM_STREAM, "messages": build_messages(text), "max_tokens": 300},
    )
    return parse(answer.text), answer.text, time.perf_counter() - start


async def main() -> None:
    h_ok = c_ok = 0
    h_times: list[float] = []
    c_times: list[float] = []
    print(f"\nCandidate: {CANDIDATE}\n")
    for text, expected in CASES:
        h_action, h_raw, h_t = await ask_haiku(text)
        c_action, c_raw, c_t = await ask_candidate(text)
        h_ok += h_action == expected
        c_ok += c_action == expected
        h_times.append(h_t)
        c_times.append(c_t)
        print(f"- {text}  (expected {expected})")
        print(f"    haiku:     {h_action} {h_t:.2f}s  {h_raw.strip()[:160]!r}")
        print(f"    candidate: {c_action} {c_t:.2f}s  {c_raw.strip()[:160]!r}")
    n = len(CASES)
    print(f"\nScore (out of {n})")
    print(f"  haiku      action {h_ok}/{n}  median {statistics.median(h_times):.2f}s")
    print(f"  candidate  action {c_ok}/{n}  median {statistics.median(c_times):.2f}s")


if __name__ == "__main__":
    asyncio.run(main())

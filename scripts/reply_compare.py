"""
Compare a live-route model against Claude Sonnet for WhatsApp/Instagram text replies.

Run inside the app container:
    python scripts/reply_compare.py [provider:model]

Baseline is Claude on the production path for reply_text (speed="deep", the
respond tool, agent.SYSTEM). The candidate gets the same system prompt and the
same respond tool, forced. Scored on the action, and on whether a reply
message states a fact the business never gave (voice_guard). Uses the cases
from voice_compare.py so both channels are judged on the same questions.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.ai import agent, client, providers, voice_guard  # noqa: E402
from voice_compare import BUSINESS, CASES, NOT_PROVIDED  # noqa: E402

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:moonshotai.kimi-k2.5"

FACTS = BUSINESS


def build_prompt(customer_text: str) -> str:
    return (
        f"{BUSINESS}{NOT_PROVIDED}\n\n"
        f"Today is {agent.ctx.now_line()}.\n\n"
        f"Conversation:\nCustomer: {customer_text}\n\n"
        "Decide how to handle the customer's most recent message."
    )


def score(result: dict | None, expected: str) -> tuple[bool, bool]:
    """(action correct, reply grounded). Grounded is True when there is no claim without a source."""
    if not result:
        return False, False
    action = result.get("action")
    message = result.get("message") or ""
    grounded = action != "reply" or not voice_guard.ungrounded_claims(message, FACTS)
    return action == expected, grounded


async def ask_claude(customer_text: str) -> dict | None:
    answer = await client.complete(
        system=agent.SYSTEM,
        messages=[{"role": "user", "content": [{"type": "text", "text": build_prompt(customer_text)}]}],
        speed="deep", max_tokens=1024, task="reply_compare_claude", tool=agent.REPLY_TOOL,
    )
    return answer.tool_input


async def ask_candidate(customer_text: str) -> dict | None:
    answer = await providers.call(
        CANDIDATE,
        {"system": agent.SYSTEM, "messages": [{"role": "user", "content": build_prompt(customer_text)}],
         "max_tokens": 1024, "tools": [agent.REPLY_TOOL],
         "tool_choice": {"type": "tool", "name": agent.REPLY_TOOL["name"]}},
    )
    return answer.tool_input


async def main() -> None:
    claude_ok = cand_ok = claude_grounded = cand_grounded = cand_answered = 0
    print(f"\nCandidate: {CANDIDATE}\n")
    for text, expected in CASES:
        c = await ask_claude(text)
        try:
            m = await ask_candidate(text)
        except Exception as exc:  # noqa: BLE001 - a failed call is a result
            m = None
            print(f"    candidate ERROR {type(exc).__name__}: {exc}")
        c_ok, c_g = score(c, expected)
        m_ok, m_g = score(m, expected)
        claude_ok += c_ok
        claude_grounded += c_g
        cand_ok += m_ok
        cand_grounded += m_g
        cand_answered += m is not None
        print(f"- {text}  (expected {expected})")
        print(f"    claude:    {(c or {}).get('action')} {'OK' if c_ok else 'CHECK'}"
              f"{'' if c_g else ' UNGROUNDED'}  {(c or {}).get('message', '')}")
        print(f"    candidate: {(m or {}).get('action')} {'OK' if m_ok else 'CHECK'}"
              f"{'' if m_g else ' UNGROUNDED'}  {(m or {}).get('message', '')}")

    n = len(CASES)
    print(f"\nScore (out of {n})")
    print(f"  claude     action {claude_ok}/{n}  grounded {claude_grounded}/{n}")
    print(f"  candidate  action {cand_ok}/{n}  grounded {cand_grounded}/{n}  answered {cand_answered}/{n}")


if __name__ == "__main__":
    asyncio.run(main())

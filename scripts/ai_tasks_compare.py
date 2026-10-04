"""
Compare a live-route model against Claude Haiku for the Haiku-direct text tasks:
call_summary, call_script_extract, outbound_opener, carousel_draft, feature_request_dedup.

Run inside the app container:
    python scripts/ai_tasks_compare.py [provider:model]

Each task uses its module's real SYSTEM prompt and tool. The user prompts here are
simplified versions of the production ones, so the scores measure the model on the
same instructions, not the exact production text. Baseline is Haiku via the direct
Anthropic path, as in production today.
"""

import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.ai import call_script_extract, call_summary, carousel_draft, client, outbound_opener, providers  # noqa: E402
from shared.care import feature_request_dedup  # noqa: E402

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:minimax.minimax-m2.5"

TRANSCRIPT_CALL = (
    "Agent: Namaste, Glow Salon se bol rahi hoon.\n"
    "Customer: Haan, kal ka appointment cancel karna hai.\n"
    "Agent: Theek hai, kis time ka tha?\n"
    "Customer: 5 baje ka. Aur sun, mujhe Sunday ke baare mein bhi batao.\n"
    "Agent: Sunday ko hum 10 AM se 8 PM tak khule hain.\n"
    "Customer: Accha, dhanyavaad. Bye."
)

SCRIPT_CALL = (
    "Agent: Aapka naam kya hai?\n"
    "Customer: Priya.\n"
    "Agent: Aap kis service ke liye aana chahti hain?\n"
    "Customer: Abhi decide nahi kiya.\n"
    "Agent: Kya aap Saturday ko aa sakti hain?\n"
    "Customer: Haan, Saturday theek hai."
)

CAROUSEL_BRIEF = "Glow Salon ke teen services: haircut Rs 350, facial Rs 1200, colour Rs 800. Hinglish mein."

OPENER_CASES = [
    "Reason: remind customer their monthly facial is due this week. Customer: Priya, last visit 5 weeks ago.",
    "Reason: confirm tomorrow's 5 PM haircut. Customer: Ramesh.",
]

DEDUP_CASES = [
    ("Dark mode chahiye app mein", None, ["Export to Excel", "Dark theme add karo", "Bulk SMS"], 2),
    ("Excel mein export karne ka option", None, ["Dark theme add karo", "Export to Excel", "Bulk SMS"], 2),
    ("WhatsApp par voice note bhejne ka option", None, ["Export to Excel", "Bulk SMS", "Dark theme add karo"], None),
]


def valid(tool_input: dict | None, tool: dict) -> bool:
    if not isinstance(tool_input, dict):
        return False
    schema = tool["input_schema"]
    if any(key not in tool_input for key in schema.get("required", [])):
        return False
    for key, spec in schema.get("properties", {}).items():
        if "enum" in spec and key in tool_input and tool_input[key] not in spec["enum"]:
            return False
    return True


async def run_tool(system: str, prompt: str, tool: dict, *, candidate: bool) -> tuple[dict | None, float]:
    start = time.perf_counter()
    if candidate:
        answer = await providers.call(
            CANDIDATE,
            {"system": system, "messages": [{"role": "user", "content": prompt}], "max_tokens": 800,
             "tools": [tool], "tool_choice": {"type": "tool", "name": tool["name"]}},
        )
        data = answer.tool_input
    else:
        answer = await client.complete(
            system=system, messages=[{"role": "user", "content": prompt}],
            speed="fast", max_tokens=800, task="ai_tasks_compare_claude", tool=tool,
        )
        data = answer.tool_input
    return data, time.perf_counter() - start


async def run_text(system: str, prompt: str, *, candidate: bool) -> tuple[str, float]:
    start = time.perf_counter()
    if candidate:
        answer = await providers.call(
            CANDIDATE, {"system": system, "messages": [{"role": "user", "content": prompt}], "max_tokens": 400},
        )
    else:
        answer = await client.complete(
            system=system, messages=[{"role": "user", "content": prompt}],
            speed="fast", max_tokens=400, task="ai_tasks_compare_claude",
        )
    return answer.text or "", time.perf_counter() - start


def report(name: str, rows: list[tuple[bool, bool, float]]) -> None:
    n = len(rows)
    valid_n = sum(r[0] for r in rows)
    correct_n = sum(r[1] for r in rows)
    median = statistics.median(r[2] for r in rows) if rows else float("nan")
    print(f"  {name:24} valid {valid_n}/{n}  correct {correct_n}/{n}  median {median:.2f}s")


async def main() -> None:
    print(f"\nCandidate: {CANDIDATE}  (baseline: Haiku direct)\n")
    results: dict[str, dict[str, list]] = {}

    def bucket(task: str, who: str) -> list:
        return results.setdefault(task, {}).setdefault(who, [])

    for who, cand in (("haiku", False), ("candidate", True)):
        # call_summary
        data, t = await run_tool(
            call_summary.SYSTEM, f"Transcript:\n{TRANSCRIPT_CALL}\n\nSummarize this call.",
            call_summary.SUMMARIZE_TOOL, candidate=cand,
        )
        bucket("call_summary", who).append((valid(data, call_summary.SUMMARIZE_TOOL), bool(data and (data.get("summary") or "").strip()), t))

        # call_script_extract
        data, t = await run_tool(
            call_script_extract.SYSTEM, f"Transcript:\n{SCRIPT_CALL}\n\nRecord the answers.",
            call_script_extract.EXTRACT_TOOL, candidate=cand,
        )
        bucket("call_script_extract", who).append((valid(data, call_script_extract.EXTRACT_TOOL), valid(data, call_script_extract.EXTRACT_TOOL), t))

        # carousel_draft
        data, t = await run_tool(
            carousel_draft.SYSTEM, f"Brief: {CAROUSEL_BRIEF}\nDraft 3 cards.",
            carousel_draft.DRAFT_TOOL, candidate=cand,
        )
        cards = next((v for v in (data or {}).values() if isinstance(v, list)), [])
        bucket("carousel_draft", who).append((valid(data, carousel_draft.DRAFT_TOOL), len(cards) == 3, t))

        # feature_request_dedup
        for new_title, _, candidates, expected in DEDUP_CASES:
            numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(candidates))
            data, t = await run_tool(
                feature_request_dedup.SYSTEM,
                f"New request: {new_title}\nExisting requests:\n{numbered}\nWhich existing request matches?",
                feature_request_dedup.MATCH_TOOL, candidate=cand,
            )
            if expected is None:
                continue
            found = next((v for v in (data or {}).values() if isinstance(v, int)), None)
            bucket("feature_request_dedup", who).append((valid(data, feature_request_dedup.MATCH_TOOL), found == expected, t))

        # outbound_opener (text)
        for case in OPENER_CASES:
            text, t = await run_text(outbound_opener.SYSTEM, case, candidate=cand)
            ok = 20 <= len(text.strip()) <= 400
            bucket("outbound_opener", who).append((ok, ok, t))

    for task in ("call_summary", "call_script_extract", "carousel_draft", "feature_request_dedup", "outbound_opener"):
        print(task)
        for who in ("haiku", "candidate"):
            rows = results.get(task, {}).get(who, [])
            report(who, rows)
        print()


if __name__ == "__main__":
    asyncio.run(main())

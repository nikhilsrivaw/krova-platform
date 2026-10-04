"""
Check the three tasks that were moved off Claude: copilot suggestion, outbound opener
and image describe. Each uses the module's real SYSTEM prompt.

Run inside the app container:
    python scripts/tasks3_compare.py [provider:model] [path/to/image.jpg]

Without an image path the image check is skipped. Haiku is not called, because it
needs Anthropic credits. Output is printed for a person to read; the only automatic
checks are "non-empty" and "within the length limit".
"""

import asyncio
import base64
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.ai import copilot_suggest, outbound_opener, providers  # noqa: E402

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "bedrock:moonshotai.kimi-k2.5"
IMAGE_PATH = sys.argv[2] if len(sys.argv) > 2 else None

CALL_TRANSCRIPT = (
    "Customer: Kal ka slot kya hai? Aur haircut ka kitna lagega?\n"
    "Agent: Kal ka slot abhi check karna hoga, haircut Rs 350 hai."
)

OPENER_REASON = "Remind the customer that their monthly facial is due this week."
OPENER_CONTEXT = "Customer: Priya. Business: Glow Salon. Facial Rs 1200. Last visit 5 weeks ago."


async def check_copilot() -> None:
    start = time.perf_counter()
    answer = await providers.call(
        CANDIDATE,
        {"system": copilot_suggest.SYSTEM,
         "messages": [{"role": "user", "content": f"Live call so far:\n{CALL_TRANSCRIPT}\n\nWhat should the owner say next?"}],
         "max_tokens": 300},
    )
    print("copilot_suggest")
    print(f"  {time.perf_counter() - start:.2f}s  {(answer.text or '').strip()[:300]!r}\n")


async def check_opener() -> None:
    start = time.perf_counter()
    answer = await providers.call(
        CANDIDATE,
        {"system": outbound_opener.SYSTEM,
         "messages": [{"role": "user", "content": f"Reason: {OPENER_REASON}\n{OPENER_CONTEXT}"}],
         "max_tokens": 200},
    )
    text = (answer.text or "").strip()
    ok = 20 <= len(text) <= 400
    print("outbound_opener (streamed prompt)")
    print(f"  {time.perf_counter() - start:.2f}s  within length: {ok}  {text[:300]!r}\n")


async def check_image(path: str) -> None:
    with open(path, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    media_type = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    start = time.perf_counter()
    answer = await providers.call(
        CANDIDATE,
        {"system": "Describe this image for a business owner in two short sentences.",
         "messages": [{"role": "user", "content": [
             {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}},
             {"type": "text", "text": "What does this image show?"},
         ]}],
         "max_tokens": 300},
    )
    print("image_describe")
    print(f"  {time.perf_counter() - start:.2f}s  {(answer.text or '').strip()[:400]!r}\n")


async def main() -> None:
    print(f"\nCandidate: {CANDIDATE}\n")
    await check_copilot()
    await check_opener()
    if IMAGE_PATH:
        await check_image(IMAGE_PATH)
    else:
        print("image_describe: skipped (no image path given)")


if __name__ == "__main__":
    asyncio.run(main())

"""
Which model should run the knowledge pick? Asks one business's real list the same questions with several models and
prints what each picked, its cost and how long it took, next to Mistral Large 3 (the model live today).

Read-only: nothing is saved, no reply is sent, no usage is recorded. Each run costs about Rs 15 of model calls.

Run on the server, from ~/krova-platform (the file is read from the host, so no rebuild is needed):

    docker compose -f docker-compose.prod.yml exec -T app python - agency < scripts/pick_model_test.py

The argument is the business name as it appears in the businesses table.
"""

import asyncio
import statistics
import sys
import time

from sqlalchemy import select

from shared.ai import context as ctx
from shared.ai import knowledge_pick as kp
from shared.ai import providers
from shared.db.models import Business
from shared.db.session import AsyncSessionLocal

REFERENCE = "bedrock:mistral.mistral-large-3-675b-instruct"
MODELS = [
    REFERENCE,
    "bedrock:mistral.ministral-3-14b-instruct",
    "bedrock:mistral.ministral-3-8b-instruct",
    "bedrock:google.gemma-3-27b-it",
    "bedrock:google.gemma-3-12b-it",
    "bedrock:google.gemma-3-4b-it",
    "bedrock:openai.gpt-oss-120b",
]
SHORT = {m: m.split(":", 1)[1].split(".", 1)[1][:22] for m in MODELS}

# What a student writes. A tuple is a conversation: the last line is the question, earlier lines alternate
# student / college so "iski" has something to point back to.
QUESTIONS = [
    "BCA ki fees kitni hai?",
    "MBA admission fee?",
    "M.A. Hindi kitne saal ka hai",
    "बी.ए. में प्रवेश के लिए योग्यता क्या है?",
    "bsc me admission ke liye kya chahiye",
    "MCA hai kya aapke yahan",
    "What courses do you offer after 12th?",
    "admission ki last date kya hai",
    "admission form ka link bhejo",
    "kaun kaun se documents lagenge admission me",
    "fees refund hoti hai kya agar admission cancel karu",
    "scholarship milti hai kya SC ST ko",
    "exam kab hote hai",
    "study centre kaha hai gorakhpur me",
    "B.Ed ki fees",
    "diploma courses kaun se hai",
    "kya online classes hoti hai",
    "hostel ki suvidha hai?",
    "parking hai campus me?",
    "placement kitna hota hai",
    ("BCA hai kya?", "Haan, BCA (Bachelor of Computer Application) hai.", "iski fees kitni hai"),
    ("MBA ke baare me batao", "MBA do saal ka programme hai.", "eligibility kya hai"),
    "hello",
    "meri fees jama hui ya nahi",
    "mujhe kisi se baat karni hai",
]


def _turns(question) -> list[dict]:
    lines = question if isinstance(question, tuple) else (question,)
    return [
        {"channel": "web", "direction": "inbound" if i % 2 == 0 else "outbound", "text": text}
        for i, text in enumerate(lines)
    ]


def _shown(picked: dict | None, raw: str) -> str:
    if picked is None:
        return "UNREADABLE: " + " ".join(raw.split())[:60]
    if picked["missing"]:
        return f"MISSING: {picked['missing']}"
    if picked["everything"]:
        return "ALL"
    names = [r.get("name", "") for r in picked["rows"]] + [a.get("question", "") for a in picked["answers"]]
    names += [p.get("name", "") for p in picked["products"]]
    return " | ".join(n[:38] for n in names) if names else "NONE"


async def _ask(model: str, prompt: str, menu: dict) -> tuple[str, float, float]:
    started = time.monotonic()
    try:
        answer = await asyncio.wait_for(
            providers.call(model, {"system": kp.SYSTEM, "messages": [{"role": "user", "content": prompt}], "max_tokens": 60}),
            30,
        )
    except Exception as exc:  # noqa: BLE001 - a failure is a result here
        return f"FAILED: {type(exc).__name__}", (time.monotonic() - started) * 1000, 0.0
    ms = (time.monotonic() - started) * 1000
    return _shown(kp._read(answer.text, menu), answer.text or ""), ms, _rupees(model, answer.usage)


def _rupees(model: str, usage) -> float:
    """Exact cost from the token counts (the provider's own figure is rounded to whole paise, too coarse here)."""
    name, ref = providers.parse_ref(model)
    spec = dict(providers.PROVIDERS[name].models)[ref]
    fresh, cached, out = spec.pricing
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (usage.input_tokens * fresh + read * cached + usage.output_tokens * out) / 1_000_000


async def main(name: str) -> None:
    async with AsyncSessionLocal() as db:
        business = (await db.execute(select(Business).where(Business.name == name))).scalars().first()
        if business is None:
            print(f"No business named {name!r}")
            return
        base = await ctx.build_anonymous(business.id, [], db)

    menu = kp._menu(*ctx.lookup_items(base))
    known = base.known_facts()
    print(f"{business.name} ({business.vertical}): {len(menu)} items on the list\n")

    stats = {m: {"ms": [], "rs": 0.0, "same": 0, "bad": 0} for m in MODELS}
    for question in QUESTIONS:
        prompt = kp._prompt(menu, _turns(question), known)
        results = await asyncio.gather(*(_ask(m, prompt, menu) for m in MODELS))
        reference = results[0][0]
        print("Q:", question[-1] if isinstance(question, tuple) else question)
        for model, (shown, ms, rs) in zip(MODELS, results):
            same = shown == reference
            s = stats[model]
            s["ms"].append(ms)
            s["rs"] += rs
            s["same"] += same
            s["bad"] += shown.startswith(("FAILED", "UNREADABLE"))
            mark = " " if model == REFERENCE else ("=" if same else "*")
            print(f"  {mark} {SHORT[model]:<22} {ms:5.0f}ms Rs{rs:.3f}  {shown}")
        print()

    n = len(QUESTIONS)
    print("Summary (= same as Mistral Large 3, * different - read those lines above to judge who is right)")
    print(f"  {'model':<22} {'same':>6} {'failed':>7} {'avg ms':>7} {'slowest':>8} {'Rs/pick':>8}")
    for model in MODELS:
        s = stats[model]
        print(f"  {SHORT[model]:<22} {s['same']:>3}/{n:<2} {s['bad']:>7} {statistics.mean(s['ms']):>7.0f} "
              f"{max(s['ms']):>8.0f} {s['rs'] / n:>8.3f}")
    print("\nOn a call the pick has 1.2 s (knowledge_pick.CALL_TIMEOUT_S); past it, word matching answers instead.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "agency"))

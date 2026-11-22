"""
Why does the live knowledge pick (Mistral Large 3) sometimes pick the wrong course? Asks the questions it got wrong
three times as it runs today and three times at temperature 0, and prints the model's raw answer each time.

Read-only: nothing is saved, no reply is sent, no usage is recorded. Costs about Rs 16 of model calls.

    docker compose -f docker-compose.prod.yml exec -T app python - agency < scripts/pick_raw_test.py
"""

import asyncio
import sys

from sqlalchemy import select

from shared.ai import context as ctx
from shared.ai import knowledge_pick as kp
from shared.ai import providers
from shared.db.models import Business
from shared.db.session import AsyncSessionLocal

MODEL = "bedrock:mistral.mistral-large-3-675b-instruct"
RUNS = 3
QUESTIONS = [
    "MBA admission fee?",
    ("BCA hai kya?", "Haan, BCA (Bachelor of Computer Application) hai.", "iski fees kitni hai"),
    "admission ki last date kya hai",
    "admission form ka link bhejo",
    "kaun kaun se documents lagenge admission me",
    "study centre kaha hai gorakhpur me",
    "BCA ki fees kitni hai?",
    "diploma courses kaun se hai",
]

_translate = providers._translate
_cold = {"on": False}


def _with_temperature(request, model, provider):
    body = _translate(request, model, provider)
    if _cold["on"]:
        body["temperature"] = 0
    return body


providers._translate = _with_temperature


def _turns(question) -> list[dict]:
    lines = question if isinstance(question, tuple) else (question,)
    return [{"channel": "web", "direction": "inbound" if i % 2 == 0 else "outbound", "text": t} for i, t in enumerate(lines)]


def _names(picked) -> str:
    if picked is None:
        return "UNREADABLE"
    if picked["missing"]:
        return f"MISSING: {picked['missing']}"
    if picked["everything"]:
        return "ALL"
    names = [r.get("name", "") for r in picked["rows"]] + [a.get("question", "") for a in picked["answers"]]
    return " | ".join(n[:40] for n in names) or "NONE"


async def main(name: str) -> None:
    async with AsyncSessionLocal() as db:
        business = (await db.execute(select(Business).where(Business.name == name))).scalars().first()
        base = await ctx.build_anonymous(business.id, [], db)
    menu = kp._menu(*ctx.lookup_items(base))
    known = base.known_facts()
    for item_id in ("1", "2", "3"):
        group, item = menu[item_id]
        print(f"item {item_id}: {item.get('name') or item.get('question')}")
    print()
    for question in QUESTIONS:
        prompt = kp._prompt(menu, _turns(question), known)
        asked = question[-1] if isinstance(question, tuple) else question
        print("Q:", asked)
        for cold in (False, True):
            _cold["on"] = cold
            for _ in range(RUNS):
                answer = await providers.call(MODEL, {"system": kp.SYSTEM, "messages": [{"role": "user", "content": prompt}], "max_tokens": kp.PICK_TOKENS})
                raw = " / ".join((answer.text or "").split("\n"))
                print(f"  {'temp 0 ' if cold else 'as live'}  raw: {raw[:70]:<70}  -> {_names(kp._read(answer.text, menu, asked))}")
        print()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "agency"))

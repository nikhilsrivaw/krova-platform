"""
Run the 12 synthetic Type 2 conversations (made-up chats, no customer data)
through the real commitment and signal extractor prompts on another
provider - to check it answers KROVA's forced-tool format at all, and how
its answers compare with the harness's expected ones, before any real
traffic is shadowed to it.

    docker compose -f docker-compose.prod.yml exec app python -m scripts.ai_provider_check sarvam:sarvam-105b

Approves the provider for this one process only; real traffic still needs
AI_APPROVED_PROVIDERS. Real spend, a few rupees.
"""
import asyncio
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from shared.ai import client, commitments, signals, providers
from shared.config.settings import settings

src = open("scripts/type2_extraction_check.py", encoding="utf-8").read().replace("asyncio.run(main())", "")
ns: dict = {"__name__": "harness"}
exec(compile(src, "h", "exec"), ns)
CASES, NOW = ns["CASES"], ns["NOW"]

REF = sys.argv[1] if len(sys.argv) > 1 else "sarvam:sarvam-105b"
settings.ai_approved_providers = providers.parse_ref(REF)[0]   # this process only, synthetic data only
captured = {}


async def via_provider(**kw):
    request = {"model": "x", "max_tokens": kw.get("max_tokens", 2048), "system": kw["system"],
               "messages": kw["messages"], "tools": [kw["tool"]],
               "tool_choice": {"type": "tool", "name": kw["tool"]["name"]}}
    ans = await providers.call(REF, request)
    captured.setdefault("cost", 0)
    captured["cost"] += ans.cost_paise
    captured["out"] = captured.get("out", 0) + ans.usage.output_tokens
    return client.Completion(text=ans.text, tool_input=ans.tool_input, input_tokens=ans.usage.input_tokens,
                             output_tokens=ans.usage.output_tokens, cost_paise=ans.cost_paise, model=REF)

client.complete = via_provider


async def main():
    for group, name, bctx, msgs, expected in CASES:
        c = await commitments.extract(messages=msgs, business_context=bctx, now=NOW)
        s = await signals.extract(messages=msgs, business_context=bctx, now=NOW, include_product=False)
        cs = [(x.direction, x.kind, x.amount_paise, str(x.due_at)[:10], round(x.confidence, 2)) for x in c.commitments]
        ss = [(x.kind, x.severity) for x in s.signals]
        print(f"{name:<28} expected: {expected}\n   commitments={cs} rejected={c.rejected}\n   signals={ss}")
    print(f"\nsarvam total cost: Rs {captured['cost'] / 100:.2f}, output tokens {captured['out']}")

asyncio.run(main())

"""
Live check against the real Claude API - real spend, roughly Rs 20. Run it
where a valid ANTHROPIC_API_KEY is set (production):

    docker compose -f docker-compose.prod.yml exec app python -m scripts.ai_live_check


1. Does the prompt cache actually land? The same extraction twice, and
   the same reply twice, with cache on - the second call must show
   cache_read > 0.
2. A mini shadow test: the 12 Type 2 conversations through the commitment
   and signal extractors on Sonnet 5 (today's model) and Haiku 4.5, compared
   with the same rules scripts/ai_shadow_report.py uses in production.
"""
import asyncio
import dataclasses
import json
import os

from shared.ai import client, commitments, signals, agent
from shared.ai import context as ctx
from shared.config.settings import settings
from scripts.ai_shadow_report import compare

# Load the Type 2 harness's cases without running it (it calls asyncio.run at import).
src = open("scripts/type2_extraction_check.py", encoding="utf-8").read()
src = src.replace("asyncio.run(main())", "")
ns: dict = {"__name__": "harness"}
exec(compile(src, "type2_extraction_check.py", "exec"), ns)
CASES, NOW = ns["CASES"], ns["NOW"]

captured: list[client.Completion] = []
_orig = client.complete


async def recording(**kw):
    c = await _orig(**kw)
    captured.append(c)
    return c

client.complete = recording


def to_dict(obj):
    return json.loads(json.dumps(dataclasses.asdict(obj), default=str))


async def cache_test():
    print("== 1. cache check")
    _, name, bctx, msgs, _ = CASES[0]
    for i in range(2):
        captured.clear()
        await commitments.extract(messages=msgs, business_context=bctx, now=NOW, cache=True)
        c = captured[-1]
        print(f"  extract_commitments call {i + 1}: in={c.input_tokens} cache_write={c.cache_creation_input_tokens} "
              f"cache_read={c.cache_read_input_tokens} cost={c.cost_paise}p")

    values = {f.name: None for f in dataclasses.fields(ctx.AgentContext)
              if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
    values.update(business_name="Vidya Classes", dna_summary="NEET coaching in Lucknow.", known_gaps=[],
                  escalate_immediately=[], identities=[], customer_name="Rohit", tone="Polite Hinglish.")
    c = ctx.AgentContext(**values)
    fees = "\n".join(f"- Batch {i}: NEET {2026 + i % 2} target, fee Rs {40000 + i * 1500}, 3 instalments, "
                     f"timings {7 + i % 5}am-{10 + i % 5}am, faculty panel {i % 7}" for i in range(60))
    c.knowledge = [{"title": "Fee structure", "kind": "price_list", "content": fees}]
    c.recent = [{"direction": "inbound", "channel": "whatsapp", "text": "Batch 12 ki fees kitni hai?"}]
    for i in range(2):
        captured.clear()
        d = await agent.draft_reply(c, cache=True)
        r = captured[-1]
        print(f"  reply_text call {i + 1}: in={r.input_tokens} cache_write={r.cache_creation_input_tokens} "
              f"cache_read={r.cache_read_input_tokens} cost={r.cost_paise}p -> {d.action}: {(d.message or '')[:70]}")


async def run_model(model: str):
    settings.claude_deep_model = model
    out = []
    for group, name, bctx, msgs, expected in CASES:
        captured.clear()
        c = await commitments.extract(messages=msgs, business_context=bctx, now=NOW)
        s = await signals.extract(messages=msgs, business_context=bctx, now=NOW, include_product=False)
        cost = sum(x.cost_paise for x in captured)
        out.append({
            "name": name, "expected": expected, "cost": cost,
            "commitments": {"commitments": [to_dict(x) for x in c.commitments]},
            "signals": {"signals": [to_dict(x) for x in s.signals]},
        })
    return out


async def shadow_test():
    print("\n== 2. mini shadow test: Sonnet 5 vs Haiku 4.5 on the 12 Type 2 conversations")
    sonnet = await run_model("claude-sonnet-5")
    haiku = await run_model("claude-haiku-4-5")
    agree_c = agree_s = crit = 0
    for p, h in zip(sonnet, haiku):
        ac, cc, nc = compare("extract_commitments", p["commitments"], h["commitments"])
        as_, cs, ns_ = compare("extract_signals", p["signals"], h["signals"])
        agree_c += ac; agree_s += as_; crit += cc + cs
        mark = "OK " if ac and as_ else ("CRIT" if cc or cs else "diff")
        print(f"  [{mark}] {p['name']:<28} expected: {p['expected']}")
        if not ac:
            print(f"         commitments: {nc}")
        if not as_:
            print(f"         signals: {ns_}")
    n = len(sonnet)
    print(f"\n  commitments agree {agree_c}/{n}, signals agree {agree_s}/{n}, critical {crit}")
    print(f"  cost: sonnet Rs {sum(x['cost'] for x in sonnet) / 100:.2f}, haiku Rs {sum(x['cost'] for x in haiku) / 100:.2f}")
    json.dump({"sonnet": sonnet, "haiku": haiku}, open(os.environ.get("OUT", "/tmp/ai_live_check.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)


async def main():
    await cache_test()
    await shadow_test()

asyncio.run(main())

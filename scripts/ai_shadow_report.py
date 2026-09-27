"""
Compare the primary and the shadow model on every call stored in
ai_shadow_runs (shared/ai/router.py), per task, against the pass bars in
docs/new/ai-router-rules.md §5 - and write every disagreement out for a
person to read.

    docker compose -f docker-compose.prod.yml exec app \
        python -m scripts.ai_shadow_report --days 10 --out /tmp/shadow.md
    docker compose -f docker-compose.prod.yml cp app:/tmp/shadow.md ./shadow.md

Comparisons are deliberately strict and structural - what would change in
the ledger, the inbox or the customer's phone, not wording:

  extract_commitments  same promises: direction, kind, amount, due date.
                       A promise the primary found that the shadow missed
                       is a MISS; a missed payment/delivery promise with an
                       amount or a date is CRITICAL (bar: zero).
  extract_signals      same kind + severity.
  extract_quotation    same found / total amount.
  reply_text           same action and booking; every number in the
                       primary's message also in the shadow's.
  image_*              the same digits read off the image.

Only commitments at or above the ledger's confirm threshold count - below
it the ledger already holds them for a person to confirm.
"""

import argparse
import asyncio
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from shared.ai.commitments import CONFIRM_THRESHOLD
from shared.db.models import AiShadowRun
from shared.db.session import AsyncSessionLocal

_NUM = re.compile(r"\d[\d,\.]*")


def _nums(text: str | None) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(text or "")}


def _commitment_keys(output: dict) -> list[tuple]:
    keys = []
    for c in output.get("commitments") or []:
        if not isinstance(c, dict):
            continue
        try:
            if float(c.get("confidence", 1)) < CONFIRM_THRESHOLD:
                continue
        except (TypeError, ValueError):
            pass
        keys.append((c.get("direction"), c.get("kind"), c.get("amount_paise"), (c.get("due_at") or "")[:10]))
    return sorted(keys, key=str)


def compare(task: str, primary: dict, shadow: dict) -> tuple[bool, bool, str]:
    """(agree, critical, note) for one call."""
    if task == "extract_commitments":
        p, s = _commitment_keys(primary), _commitment_keys(shadow)
        if p == s:
            return True, False, ""
        s_loose = {(d, k) for d, k, _, _ in s}
        missed = [c for c in p if (c[0], c[1]) not in s_loose]
        critical = any(
            kind in ("payment", "delivery") and (amount or due) for _, kind, amount, due in missed
        )
        return False, critical, f"missed={missed} primary={p} shadow={s}"
    if task == "extract_signals":
        key = lambda o: sorted((x.get("kind"), x.get("severity")) for x in o.get("signals") or [] if isinstance(x, dict))
        p, s = key(primary), key(shadow)
        missed = [x for x in p if x not in s]
        critical = any(sev == "critical" for _, sev in missed)
        return p == s, critical, "" if p == s else f"primary={p} shadow={s}"
    if task == "extract_quotation":
        p = (bool(primary.get("found")), primary.get("total_amount"))
        s = (bool(shadow.get("found")), shadow.get("total_amount"))
        return p == s, False, "" if p == s else f"primary={p} shadow={s}"
    if task == "reply_text":
        fields = ("action", "book_slot", "book_doctor", "book_token", "book_property")
        diff = [f for f in fields if (primary.get(f) or None) != (shadow.get(f) or None)]
        extra_nums = _nums(shadow.get("message")) - _nums(primary.get("message"))
        if extra_nums:
            diff.append(f"numbers only in shadow: {sorted(extra_nums)}")
        # Booking the wrong slot, or a number the primary never said, is
        # what would reach a customer wrongly.
        critical = any(d.startswith("book") or d.startswith("numbers") for d in diff)
        return not diff, critical, "; ".join(diff)
    if task.startswith("image_"):
        p, s = _nums(primary.get("text")), _nums(shadow.get("text"))
        return p == s, bool(p - s), "" if p == s else f"digits primary={sorted(p)} shadow={sorted(s)}"
    same = json.dumps(primary, sort_keys=True) == json.dumps(shadow, sort_keys=True)
    return same, False, ""


async def main(days: int, out: str | None) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(AiShadowRun).where(AiShadowRun.created_at >= since).order_by(AiShadowRun.created_at)
            )
        ).scalars().all()

    if not rows:
        print("no shadow runs in the window")
        return

    stats = defaultdict(lambda: {"n": 0, "agree": 0, "critical": 0, "p_cost": 0, "s_cost": 0})
    disagreements = []
    for r in rows:
        agree, critical, note = compare(r.task, r.primary_output or {}, r.shadow_output or {})
        st = stats[(r.task, r.shadow_model)]
        st["n"] += 1
        st["agree"] += agree
        st["critical"] += critical
        st["p_cost"] += r.primary_cost_paise
        st["s_cost"] += r.shadow_cost_paise
        if not agree:
            disagreements.append((r, critical, note))

    print(f"{'task':24} {'shadow model':22} {'calls':>6} {'agree':>7} {'critical':>9} {'primary Rs':>11} {'shadow Rs':>10}")
    for (task, model), st in sorted(stats.items()):
        print(f"{task:24} {model:22} {st['n']:>6} {st['agree'] / st['n']:>7.1%} {st['critical']:>9} "
              f"{st['p_cost'] / 100:>11.2f} {st['s_cost'] / 100:>10.2f}")
    print("\nPass bars (ai-router-rules.md §5): critical = 0 for commitments and replies; "
          "reply agreement >= 97%; image digits >= 99%.")

    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"# Shadow disagreements - last {days} days\n\n")
            for r, critical, note in disagreements:
                f.write(f"## {r.task} {'**CRITICAL**' if critical else ''} - {r.created_at:%d %b %H:%M}\n\n")
                f.write(f"{note}\n\n")
                f.write("**Input (tail):**\n\n```\n" + (r.input_excerpt or "")[-1500:] + "\n```\n\n")
                f.write(f"**{r.primary_model}:**\n\n```json\n{json.dumps(r.primary_output, ensure_ascii=False, indent=1)}\n```\n\n")
                f.write(f"**{r.shadow_model}:**\n\n```json\n{json.dumps(r.shadow_output, ensure_ascii=False, indent=1)}\n```\n\n")
        print(f"\n{len(disagreements)} disagreements written to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    asyncio.run(main(args.days, args.out))

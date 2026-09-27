"""
Sum the `ai_usage` log lines (shared/ai/client.py::_log_usage) per task.

The measurement step of docs/new/ai-cost-reduction.md: which task spends
what, how many tokens each call really sends, whether cache reads ever
land, and whether output tokens are far above what a tool answer needs
(a sign of hidden thinking).

    docker compose -f docker-compose.prod.yml logs --no-log-prefix --since 72h \
        | docker compose -f docker-compose.prod.yml exec -T app python scripts/ai_usage_report.py

(every service, since the workers - worker-respond, worker-analyse... - run
as their own containers and make most of the calls)
"""

import re
import sys
from collections import defaultdict

LINE = re.compile(
    r"ai_usage task=(\S+) model=(\S+) in=(\d+) out=(\d+) "
    r"cache_read=(\d+) cache_write=(\d+) cost_paise=(\d+)"
)


def main() -> None:
    rows: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for line in sys.stdin:
        m = LINE.search(line)
        if not m:
            continue
        task, model = m.group(1), m.group(2)
        acc = rows[(task, model)]
        acc[0] += 1
        for i, value in enumerate(m.groups()[2:], start=1):
            acc[i] += int(value)

    if not rows:
        print("no ai_usage lines found")
        return

    total_paise = sum(acc[5] for acc in rows.values())
    print(f"{'task':28} {'model':28} {'calls':>6} {'avg in':>8} {'avg out':>8} "
          f"{'cache rd':>9} {'cost Rs':>9} {'share':>6}")
    for (task, model), (calls, tin, tout, cread, _cwrite, paise) in sorted(
        rows.items(), key=lambda kv: -kv[1][5]
    ):
        print(f"{task:28} {model:28} {calls:>6} {tin // calls:>8} {tout // calls:>8} "
              f"{cread // calls:>9} {paise / 100:>9.2f} {paise / total_paise:>6.0%}")
    print(f"\ntotal: Rs {total_paise / 100:.2f}")


if __name__ == "__main__":
    main()

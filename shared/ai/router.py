"""
Which model answers which task - and, while a cheaper model is being
proven, which model answers it silently alongside.

Every call to client.complete names its task (reply_text,
extract_commitments, ...). Today every task is `live` on the model its
speed maps to - exactly the behaviour before this module existed. The one
other mode wired up so far is `shadow`:

  shadow - the primary model answers as normal and its answer is the only
           one ever used. A candidate model is sent the identical request
           in the background, after the primary has returned (so nothing
           waits on it), and both answers are stored in ai_shadow_runs for
           scripts/ai_shadow_report.py to compare.

That comparison is the gate in docs/new/ai-router-rules.md §5: a task
moves to a cheaper model only after its shadow run shows no loss. The
cascade rules in that document (promise markers, price/time checks) come
after, built on what the shadow data shows.

Shadow mode is configured by AI_SHADOW_ROUTES (settings.ai_shadow_routes),
never in code, so a test starts and stops with an env change and a
restart, and the default - empty - costs nothing:

    AI_SHADOW_ROUTES={"extract_signals": {"model": "claude-haiku-4-5", "rate": 1.0}}

`model` may name another provider as `provider:model`
("sarvam:sarvam-105b") - only one listed in AI_APPROVED_PROVIDERS; see
shared/ai/providers.py for why that list exists. `rate` is the share of calls also sent to the shadow model (0..1). A
malformed value is logged and ignored - a typo in an env var must never
break a live reply.
"""

import asyncio
import json
import random
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# How much of the request's final text block to keep for a person to judge
# two answers by: the conversation and the customer half, not the whole
# knowledge base in front of it.
EXCERPT_CHARS = 6000


@dataclass(frozen=True, slots=True)
class ShadowRoute:
    model: str
    rate: float


@lru_cache(maxsize=1)
def _shadow_routes(raw: str) -> dict[str, ShadowRoute]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        routes = {}
        for task, spec in parsed.items():
            model = str(spec["model"]).strip()
            rate = min(1.0, max(0.0, float(spec.get("rate", 1.0))))
            if model and rate > 0:
                routes[str(task)] = ShadowRoute(model=model, rate=rate)
        return routes
    except (ValueError, TypeError, KeyError, AttributeError):
        logger.error("AI_SHADOW_ROUTES is not valid JSON of {task: {model, rate}} - ignored")
        return {}


def shadow_for(task: str) -> ShadowRoute | None:
    """The shadow route for this call, if one is configured and this call is sampled."""
    route = _shadow_routes(settings.ai_shadow_routes).get(task)
    if route is None or random.random() >= route.rate:
        return None
    return route


def _excerpt(messages: list[dict[str, Any]]) -> str | None:
    if not messages:
        return None
    content = messages[-1].get("content")
    if isinstance(content, str):
        text = content
    else:
        texts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        text = texts[-1] if texts else ""
    return text[-EXCERPT_CHARS:] or None


def _answer(content: list[Any]) -> dict:
    """The comparable part of a response: its tool input, else its text."""
    for block in content:
        if getattr(block, "type", None) == "tool_use" and isinstance(block.input, dict):
            return block.input
    return {"text": "".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")}


# Background tasks must be referenced until done, or the event loop may
# drop them mid-flight.
_pending: set[asyncio.Task] = set()


def start_shadow(
    *,
    route: ShadowRoute,
    task: str,
    request: dict[str, Any],
    primary_model: str,
    primary_content: list[Any],
    primary_cost_paise: int,
) -> None:
    """Fire the shadow call in the background; never raises, never blocks."""
    job = asyncio.create_task(
        _run_shadow(route, task, request, primary_model, primary_content, primary_cost_paise)
    )
    _pending.add(job)
    job.add_done_callback(_pending.discard)


async def _run_shadow(
    route: ShadowRoute,
    task: str,
    request: dict[str, Any],
    primary_model: str,
    primary_content: list[Any],
    primary_cost_paise: int,
) -> None:
    # Imported here: client imports this module, and the DB layer has no
    # business being loaded by every AI call that never shadows.
    from shared.ai import client, providers
    from shared.db.models import AiShadowRun
    from shared.db.session import AsyncSessionLocal

    try:
        provider, model = providers.parse_ref(route.model)
        if provider == "anthropic":
            response = await client._get_client().messages.create(**{**request, "model": model})
            usage = response.usage
            cost = client._cost_paise(
                model,
                usage.input_tokens,
                usage.output_tokens,
                cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
            shadow_answer = _answer(response.content)
        else:
            # Refuses (and this whole shadow is dropped) unless the provider
            # is approved - see providers.py.
            answer = await providers.call(route.model, request)
            usage, cost = answer.usage, answer.cost_paise
            shadow_answer = answer.tool_input if answer.tool_input is not None else {"text": answer.text}
        client._log_usage(f"{task}:shadow", route.model, usage, cost)
        async with AsyncSessionLocal() as db:
            db.add(
                AiShadowRun(
                    task=task,
                    primary_model=primary_model,
                    shadow_model=route.model,
                    primary_output=_answer(primary_content),
                    shadow_output=shadow_answer,
                    input_excerpt=_excerpt(request.get("messages") or []),
                    primary_cost_paise=primary_cost_paise,
                    shadow_cost_paise=cost,
                )
            )
            await db.commit()
    except providers.ProviderRefused as exc:
        logger.warning("shadow refused task=%s model=%s: %s", task, route.model, exc)
    except Exception:  # noqa: BLE001 - a shadow failure must never surface anywhere
        logger.warning("shadow call failed task=%s model=%s", task, route.model, exc_info=True)

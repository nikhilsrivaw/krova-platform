"""
Talking to Claude.

Two models, chosen by what the caller is waiting on:

  fast  - anything a human is waiting through. A caller on the phone hears
          every millisecond, so this path uses Haiku and never thinks.
  deep  - overnight analysis, where nobody is waiting and being right matters
          more than being quick.

Confusing the two is the most common way an agent product ends up feeling
slow, so the choice is a parameter here rather than a decision scattered
through the callers.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

from anthropic import AsyncAnthropic, APIError, APIStatusError

from shared.ai import providers, router
from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

Speed = Literal["fast", "deep"]

# USD per million tokens, by model-id prefix - Anthropic's list prices. Keyed
# by model, not by speed: CLAUDE_DEEP_MODEL / CLAUDE_FAST_MODEL are env
# settings, and a rate tied to "deep" silently goes wrong the day the model
# behind it changes (it did - "deep" was priced as Sonnet 4.6's $3/$15 long
# after it became Sonnet 5 at $2/$10, overstating every deep call by 1.5x).
# Longest matching prefix wins; an unknown model falls back to the most
# expensive known rate so spend is never under-reported.
_PRICING_USD: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
}
_FALLBACK_USD = max(_PRICING_USD.values())
# Rough rupee rate for the per-tenant cost ledger. Approximate by design - the
# point is knowing which businesses and which tasks cost what, not accounting.
_INR_PER_USD = 88.0


class AIError(Exception):
    """Claude could not be reached, or answered unusably."""


@dataclass(slots=True)
class Completion:
    text: str
    tool_input: dict | None
    input_tokens: int
    output_tokens: int
    cost_paise: int
    model: str
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise AIError("ANTHROPIC_API_KEY is not set")
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _model_for(speed: Speed) -> str:
    return settings.claude_fast_model if speed == "fast" else settings.claude_deep_model


def _rates_inr(model: str) -> tuple[float, float]:
    """(input, output) INR per million tokens for this model id."""
    match = max((p for p in _PRICING_USD if model.startswith(p)), key=len, default=None)
    usd_in, usd_out = _PRICING_USD[match] if match else _FALLBACK_USD
    return usd_in * _INR_PER_USD, usd_out * _INR_PER_USD


def _cost_paise(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> int:
    # Anthropic prices a cache write at 1.25x normal input (default 5m TTL,
    # the only TTL this codebase uses) and a cache read at 0.1x - both are
    # counted separately from input_tokens in the API's own usage response,
    # so leaving them out here would silently undercount real spend on any
    # call using cache_control (see TextStream.__aiter__).
    rate_in, rate_out = _rates_inr(model)
    rupees = (
        input_tokens * rate_in
        + output_tokens * rate_out
        + cache_creation_input_tokens * rate_in * 1.25
        + cache_read_input_tokens * rate_in * 0.1
    ) / 1_000_000
    return round(rupees * 100)


def _log_usage(task: str, model: str, usage: Any, cost_paise: int) -> None:
    """
    One structured line per Claude call - the measurement channel for the
    cost work in docs/new/ai-cost-reduction.md. `task` names what the call
    was for (reply_text, extract_commitments...), so tokens can be summed
    per task from the logs without guessing from call sites. A cache_read
    that stays 0 on a task that sets cache_control means the breakpoint is
    not landing; an output_tokens far above what the tool JSON needs means
    the model is spending tokens on thinking.
    """
    logger.info(
        "ai_usage task=%s model=%s in=%s out=%s cache_read=%s cache_write=%s cost_paise=%s",
        task, model, usage.input_tokens, usage.output_tokens,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
        getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cost_paise,
    )



_RUPEE_WORDS = {"inr", "rs", "rs.", "rupee", "rupees", "rupaye", "rupya", "rupya", "₹", "rupaiya", "rupaiye"}


def _normalise_currency(value: Any) -> str:
    """Models write the same currency many ways ("rupees", "Rs.", "₹"). Everything here means INR."""
    if not isinstance(value, str):
        return "INR"
    key = value.strip().lower()
    return "INR" if (key in _RUPEE_WORDS or not key) else value.strip().upper()


# Voice never leaves Claude: a live route for these is ignored, not honoured.
_VOICE_TASK_PREFIXES = ("reply_voice", "reply_owner_voice", "reply_scripted_voice")


async def _complete_via_provider(
    ref: str, task: str, system: str, messages: list[dict[str, Any]],
    max_tokens: int, tool: dict | None,
) -> Completion | None:
    """
    Answer through a non-Anthropic provider. Returns None on any failure, so the
    caller falls back to Claude: a cheaper model that is down must never mean
    a reply that is never sent.
    """
    from shared.ai import providers

    request: dict[str, Any] = {"system": system, "messages": messages, "max_tokens": max_tokens}
    if tool is not None:
        request["tools"] = [tool]
        request["tool_choice"] = {"type": "tool", "name": tool["name"]}
    try:
        answer = await providers.call(ref, request)
    except Exception:  # noqa: BLE001 - any provider failure falls back to Claude
        logger.warning("live route %s failed for task=%s, falling back to Claude", ref, task, exc_info=True)
        return None

    if tool is not None and answer.tool_input is None:
        # A tool-driven task with no tool call is an empty answer, not a result.
        logger.warning("live route %s gave no tool call for task=%s, falling back to Claude", ref, task)
        return None

    if isinstance(answer.tool_input, dict) and "currency" in answer.tool_input:
        answer.tool_input["currency"] = _normalise_currency(answer.tool_input["currency"])

    _log_usage(task, ref, answer.usage, answer.cost_paise)
    return Completion(
        text=answer.text,
        tool_input=answer.tool_input,
        input_tokens=answer.usage.input_tokens,
        output_tokens=answer.usage.output_tokens,
        cost_paise=answer.cost_paise,
        model=ref,
    )


async def complete(
    *,
    system: str,
    messages: list[dict[str, Any]],
    speed: Speed = "deep",
    max_tokens: int = 2048,
    tool: dict | None = None,
    task: str = "unnamed",
    cache_system: bool = False,
) -> Completion:
    """
    Ask Claude something.

    When `tool` is given, the model is forced to answer through it. That is
    how structured output is obtained reliably - asking for JSON in a prompt
    and hoping produces valid JSON most of the time, and "most of the time" is
    a bug that appears in production at 2am.
    """
    live = router.live_for(task)
    if live is not None and not task.startswith(_VOICE_TASK_PREFIXES):
        answer = await _complete_via_provider(live, task, system, messages, max_tokens, tool)
        if answer is not None:
            return answer

    client = _get_client()
    model = _model_for(speed)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        # A breakpoint on the system prompt caches tools + system together
        # (tools render first). Callers opt in via shared/ai/cache_policy -
        # a write nobody reads back costs 25% more than no cache at all.
        "system": (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if cache_system
            else system
        ),
        "messages": messages,
    }
    if tool is not None:
        kwargs["tools"] = [tool]
        kwargs["tool_choice"] = {"type": "tool", "name": tool["name"]}

    try:
        response = await client.messages.create(**kwargs)
    except APIStatusError as exc:
        logger.error("claude %s returned %s: %s", model, exc.status_code, str(exc)[:300])
        raise AIError(f"Claude returned {exc.status_code}") from exc
    except APIError as exc:
        logger.error("claude request failed: %s", str(exc)[:300])
        raise AIError("Could not reach Claude") from exc

    text_parts: list[str] = []
    tool_input: dict | None = None
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_input = block.input if isinstance(block.input, dict) else None

    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cost = _cost_paise(
        model,
        usage.input_tokens,
        usage.output_tokens,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
    )
    _log_usage(task, model, usage, cost)

    # A candidate model being proven on this task answers the same request
    # in the background; its answer is stored for comparison, never used.
    shadow = router.shadow_for(task)
    if shadow is not None and shadow.model != model:
        router.start_shadow(
            route=shadow, task=task, request=kwargs, primary_model=model,
            primary_content=response.content, primary_cost_paise=cost,
        )

    return Completion(
        text="".join(text_parts),
        tool_input=tool_input,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_paise=cost,
        model=model,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


class TextStream:
    """
    An in-progress Claude text generation.

    Iterate it directly to get each text delta as it arrives. `cost_paise`
    reads 0 until the stream is fully consumed, then holds the real cost -
    an attribute rather than a return value because an async generator
    cannot cleanly hand back a value alongside its yields. Instance state,
    not module-level: a bare function attribute here would have one call's
    cost overwritten by whichever of several concurrent voice calls
    finished last, since many calls run at once on this platform.
    """

    def __init__(
        self, *, system: str, messages: list[dict[str, Any]], speed: Speed, max_tokens: int,
        task: str = "unnamed",
    ):
        self._task = task
        self._system = system
        self._messages = messages
        self._speed = speed
        self._max_tokens = max_tokens
        self.cost_paise = 0

    async def __aiter__(self):
        client = _get_client()
        model = _model_for(self._speed)

        # Every real caller of stream_text (agent.py's stream_reply,
        # stream_owner_reply, the scripted-call variant) passes one of a
        # handful of fixed, sizeable system-prompt constants - never one
        # built per-call - on the one path a live caller is waiting
        # through. A cache breakpoint here costs slightly more on the
        # first request in a 5-minute window and is cheap (and faster to
        # first token) on every request after, platform-wide, since the
        # text is identical across calls. complete() is deliberately left
        # untouched - its callers' system prompts are more heterogeneous
        # and not audited for this.
        system: str | list[dict] = [
            {"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}
        ]

        try:
            async with client.messages.stream(
                model=model,
                max_tokens=self._max_tokens,
                system=system,
                messages=self._messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
        except APIStatusError as exc:
            logger.error("claude %s returned %s: %s", model, exc.status_code, str(exc)[:300])
            raise AIError(f"Claude returned {exc.status_code}") from exc
        except APIError as exc:
            logger.error("claude streaming request failed: %s", str(exc)[:300])
            raise AIError("Could not reach Claude") from exc

        usage = final.usage
        self.cost_paise = _cost_paise(
            model,
            usage.input_tokens,
            usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        )
        _log_usage(self._task, model, usage, self.cost_paise)


class _ProviderTextStream:
    """A whole non-Claude reply, presented as the one-delta stream TextStream gives."""

    def __init__(self, route: str, *, system: str, messages: list[dict[str, Any]], max_tokens: int, task: str):
        self._route = route
        self._system = system
        self._messages = messages
        self._max_tokens = max_tokens
        self._task = task
        self.cost_paise = 0

    def __aiter__(self):
        return self._once()

    async def _once(self):
        answer = await providers.call(
            self._route,
            {"system": self._system, "messages": self._messages, "max_tokens": self._max_tokens},
        )
        self.cost_paise = answer.cost_paise
        _log_usage(self._task, self._route, answer.usage, answer.cost_paise)
        yield answer.text or ""


def stream_text(
    *, system: str, messages: list[dict[str, Any]], speed: Speed = "fast", max_tokens: int = 300,
    task: str = "unnamed",
) -> TextStream:
    """
    Stream plain text as Claude generates it.

    Deliberately no `tool` parameter, unlike complete(): a live call needs
    to start speaking a sentence the instant it is generated, not wait for
    a whole JSON object to close - Anthropic streams tool-call JSON as raw
    text deltas that only become valid JSON once complete, which would mean
    parsing an incrementally-growing, structurally-invalid document just to
    find where the "message" field's value is. Plain text sidesteps that
    entirely, at the cost of the caller (agent.stream_reply) needing its own
    much simpler convention for action/gap instead of a JSON schema.
    """
    live = router.live_for(task)
    if live is not None and not task.startswith(_VOICE_TASK_PREFIXES):
        return _ProviderTextStream(live, system=system, messages=messages, max_tokens=max_tokens, task=task)
    return TextStream(system=system, messages=messages, speed=speed, max_tokens=max_tokens, task=task)

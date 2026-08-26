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

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

Speed = Literal["fast", "deep"]

# Rough INR per million tokens, for the per-tenant cost ledger. Approximate by
# design - the point is knowing which businesses cost what, not accounting.
_PRICING = {
    "fast": {"in": 88.0, "out": 440.0},
    "deep": {"in": 264.0, "out": 1320.0},
}


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


def _cost_paise(speed: Speed, input_tokens: int, output_tokens: int) -> int:
    rates = _PRICING[speed]
    rupees = (input_tokens * rates["in"] + output_tokens * rates["out"]) / 1_000_000
    return round(rupees * 100)


async def complete(
    *,
    system: str,
    messages: list[dict[str, Any]],
    speed: Speed = "deep",
    max_tokens: int = 2048,
    tool: dict | None = None,
) -> Completion:
    """
    Ask Claude something.

    When `tool` is given, the model is forced to answer through it. That is
    how structured output is obtained reliably - asking for JSON in a prompt
    and hoping produces valid JSON most of the time, and "most of the time" is
    a bug that appears in production at 2am.
    """
    client = _get_client()
    model = _model_for(speed)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
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
    return Completion(
        text="".join(text_parts),
        tool_input=tool_input,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_paise=_cost_paise(speed, usage.input_tokens, usage.output_tokens),
        model=model,
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

    def __init__(self, *, system: str, messages: list[dict[str, Any]], speed: Speed, max_tokens: int):
        self._system = system
        self._messages = messages
        self._speed = speed
        self._max_tokens = max_tokens
        self.cost_paise = 0

    async def __aiter__(self):
        client = _get_client()
        model = _model_for(self._speed)

        try:
            async with client.messages.stream(
                model=model,
                max_tokens=self._max_tokens,
                system=self._system,
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
        self.cost_paise = _cost_paise(self._speed, usage.input_tokens, usage.output_tokens)


def stream_text(
    *, system: str, messages: list[dict[str, Any]], speed: Speed = "fast", max_tokens: int = 300
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
    return TextStream(system=system, messages=messages, speed=speed, max_tokens=max_tokens)

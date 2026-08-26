"""
The Claude side of the conversation.

Two things here are specific to voice rather than chat:

1. Latency is the product. Thinking is disabled and effort pinned low,
   because a caller hears every millisecond of silence.

2. We emit speakable chunks as they form, not the whole reply at the
   end. Telnyx starts speaking sentence one while Claude is still
   writing sentence two, which removes seconds of dead air. The first
   chunk flushes early and aggressively - it is the one that ends the
   silence - while later chunks wait for a proper sentence so the
   speech does not sound clipped.
"""

import logging
import re
from collections.abc import AsyncIterator, Callable

from anthropic import AsyncAnthropic

from app.config import settings

logger = logging.getLogger(__name__)

client = AsyncAnthropic(api_key=settings.anthropic_api_key)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_SOFT_BREAK = re.compile(r"(?<=,)\s+")

_MIN_CHUNK_FIRST = 12
_MIN_CHUNK = 25
_SOFT_AT_FIRST = 40
_SOFT_AT = 90


def _split_ready(
    buffer: str, min_chunk: int, soft_at: int
) -> tuple[list[str], str]:
    """
    Pull complete, speakable chunks out of the buffer.

    A boundary is only used if the text before it is long enough to be
    worth speaking alone - otherwise "Hi." becomes its own audio clip
    and the speech sounds chopped.
    """
    chunks: list[str] = []

    while True:
        emitted = False
        for match in _SENTENCE_END.finditer(buffer):
            candidate = buffer[: match.end()].strip()
            if len(candidate) >= min_chunk:
                chunks.append(candidate)
                buffer = buffer[match.end():]
                emitted = True
                break
        if not emitted:
            break

    if len(buffer) >= soft_at:
        for match in _SOFT_BREAK.finditer(buffer):
            candidate = buffer[: match.end()].strip()
            if len(candidate) >= min_chunk:
                chunks.append(candidate)
                buffer = buffer[match.end():]
                break

    return chunks, buffer


def _latency_params(model: str) -> dict:
    """
    Per-model knobs for lowest latency.

    Sonnet 5 runs adaptive thinking if `thinking` is omitted, which adds
    seconds a caller hears as silence - so it must be disabled
    explicitly. Haiku 4.5 accepts neither parameter and 400s if sent.
    """
    if model.startswith("claude-haiku"):
        return {}
    return {
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low"},
    }


async def stream_reply(
    system_prompt: str,
    history: list[dict],
    model: str | None = None,
    on_usage: Callable[[str, int, int], None] | None = None,
) -> AsyncIterator[str]:
    """
    Stream Claude's reply as speakable chunks.

    `on_usage(model, input_tokens, output_tokens)` fires once the reply
    completes, so the caller can bill it. It does not fire if the caller
    interrupted - we only charge for what was actually generated, which
    the API reports on the final message.
    """
    model = model or settings.model

    async with client.messages.stream(
        model=model,
        max_tokens=settings.max_reply_tokens,
        **_latency_params(model),
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=history,
    ) as stream:
        buffer = ""
        is_first = True
        async for token in stream.text_stream:
            buffer += token
            ready, buffer = _split_ready(
                buffer,
                _MIN_CHUNK_FIRST if is_first else _MIN_CHUNK,
                _SOFT_AT_FIRST if is_first else _SOFT_AT,
            )
            for chunk in ready:
                is_first = False
                yield chunk

        if buffer.strip():
            yield buffer.strip()

        if on_usage is not None:
            try:
                final = await stream.get_final_message()
                on_usage(
                    model,
                    final.usage.input_tokens,
                    final.usage.output_tokens,
                )
            except Exception:
                logger.warning("could not read usage for call", exc_info=True)

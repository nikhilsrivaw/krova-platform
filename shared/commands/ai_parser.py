"""
Mode A: a model turns a command the pattern matcher did not recognise into one
tool call. It sends the owner's text to a non-Anthropic provider, so it runs
only when that provider is in AI_APPROVED_PROVIDERS (see shared/ai/providers.py).
Until then it refuses before any data leaves KROVA.

The model's answer is JSON {"tool": ..., "args": {...}}. It is checked against
the tool's schema, and a result that does not validate is refused, never run.
"""

import json
from typing import Any

from shared.ai import providers
from shared.commands.tools import TOOLS, ToolRefused, parse_input
from shared.config.settings import settings

MAX_TEXT_CHARS = 500


class ParserUnavailable(Exception):
    """Mode A cannot run: no approved provider, or the model is not configured."""


def _tool_catalogue() -> str:
    lines = []
    for t in TOOLS.values():
        schema = json.dumps(t.input_model.model_json_schema().get("properties", {}), ensure_ascii=False)
        lines.append(f"- {t.name}: {t.description}. Fields: {schema}")
    return "\n".join(lines)


async def parse(text: str) -> dict[str, Any]:
    clean = text.strip()[:MAX_TEXT_CHARS]
    if not clean:
        raise ToolRefused("Command khaali hai")
    system = (
        "You turn one business owner's command into exactly one tool call.\n"
        "Reply with JSON only: {\"tool\": \"<name>\", \"args\": {...}}. No other text.\n"
        "If the command is not covered by a tool, reply {\"tool\": null}.\n"
        "Never invent customers or ids. Dates are YYYY-MM-DD, times HH:MM, 24-hour.\n\n"
        f"Tools:\n{_tool_catalogue()}"
    )
    try:
        answer = await providers.call(
            settings.owner_command_model,
            {"system": system, "messages": [{"role": "user", "content": clean}], "max_tokens": 300},
        )
    except providers.ProviderRefused as exc:
        raise ParserUnavailable(
            "Ye command samajh nahi aayi. Ek saral command try karein, jaise 'kal 2 se 4 band karo'."
        ) from exc

    try:
        data = json.loads(_strip_fence(answer.text))
    except (ValueError, TypeError) as exc:
        raise ToolRefused("Command samajh nahi aayi, dobara likhein") from exc
    if not isinstance(data, dict) or not data.get("tool"):
        raise ToolRefused("Ye command abhi support nahi hoti")
    name = str(data["tool"])
    if name not in TOOLS:
        raise ToolRefused("Ye command abhi support nahi hoti")
    args = data.get("args") or {}
    parse_input(name, args)  # schema check; a bad result is refused, not run
    return {"tool": name, "args": args}


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    return t.strip()

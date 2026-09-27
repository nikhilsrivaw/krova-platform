"""
Non-Anthropic model providers, behind one adapter - and the allow-list that
decides which of them may ever receive a customer's conversation.

A model is named `provider:model` ("sarvam:sarvam-105b"). A bare name
("claude-haiku-4-5") means Anthropic, which is always allowed. Every other
provider must be listed in AI_APPROVED_PROVIDERS before the router will
send it anything - and it goes on that list only after a person has
confirmed, for that provider (docs/new/ai-multi-provider-router.md §2):

  1. a paid plan whose terms say our data is not used for training
     (Sarvam's public policy trains on content by default - the opt-out
     has to be switched off in the account),
  2. a signed DPA,
  3. it is named as a sub-processor in KROVA's privacy policy.

That list is DPDP in code: a route pointing at an unapproved provider is
refused and logged, so a typo in a route can never send a customer's chat
somewhere nobody vetted.

Every provider here speaks the OpenAI chat-completions format, so one
adapter covers them: the Anthropic-shaped request client.complete builds
(system, content blocks, one forced tool) is translated on the way out and
the answer translated back, so callers never know which model answered.
"""

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

_INR_PER_USD = 88.0


@dataclass(frozen=True, slots=True)
class Provider:
    base_url: str
    key_setting: str
    # Header name -> value template; "{key}" is replaced with the API key.
    headers: tuple[tuple[str, str], ...]
    vision: bool
    # INR per million tokens by model-id prefix: (input, cached input, output).
    pricing: tuple[tuple[str, tuple[float, float, float]], ...]
    # Extra body fields every request to this provider carries.
    extra_body: tuple[tuple[str, Any], ...] = ()


PROVIDERS: dict[str, Provider] = {
    # docs.sarvam.ai: POST /v1/chat/completions, OpenAI-style tools and
    # tool_choice, no image input. Prices in INR from its pricing page
    # (2026-08). Hosted in India. sarvam-105b is a reasoning model -
    # reasoning tokens bill as output, so effort is kept low.
    "sarvam": Provider(
        base_url="https://api.sarvam.ai/v1",
        key_setting="sarvam_api_key",
        headers=(("api-subscription-key", "{key}"), ("Authorization", "Bearer {key}")),
        vision=False,
        pricing=(("sarvam-105b", (29.28, 10.98, 73.2)),),
        extra_body=(("reasoning_effort", "low"),),
    ),
    # Gemini's OpenAI-compatible endpoint (paid tier only - the free tier
    # trains on content). USD list prices converted.
    "gemini": Provider(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_setting="gemini_api_key",
        headers=(("Authorization", "Bearer {key}"),),
        vision=True,
        pricing=(
            ("gemini-3.5-flash-lite", (0.30 * _INR_PER_USD, 0.03 * _INR_PER_USD, 2.50 * _INR_PER_USD)),
            ("gemini-3.8-flash", (0.75 * _INR_PER_USD, 0.075 * _INR_PER_USD, 3.75 * _INR_PER_USD)),
        ),
    ),
}


class ProviderRefused(Exception):
    """The route names a provider that is unknown, unapproved, or can't do this request."""


def parse_ref(ref: str) -> tuple[str, str]:
    """"sarvam:sarvam-105b" -> ("sarvam", "sarvam-105b"); a bare name is Anthropic."""
    provider, sep, model = ref.partition(":")
    return (provider.strip(), model.strip()) if sep else ("anthropic", ref.strip())


def approved() -> set[str]:
    listed = {p.strip() for p in (settings.ai_approved_providers or "").split(",") if p.strip()}
    return {"anthropic"} | listed


def _rates(provider: Provider, model: str) -> tuple[float, float, float]:
    match = max((p for p, _ in provider.pricing if model.startswith(p)), key=len, default=None)
    if match is None:
        # Unknown model: charge the dearest known rate so spend is never under-reported.
        return max((r for _, r in provider.pricing), key=lambda r: r[2])
    return dict(provider.pricing)[match]


def _blocks(content: Any) -> list[dict]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])


def _translate(request: dict[str, Any], model: str, provider: Provider) -> dict[str, Any]:
    """Anthropic-shaped request -> OpenAI chat-completions body."""
    system = request.get("system") or ""
    if not isinstance(system, str):
        system = "\n\n".join(b.get("text", "") for b in system)
    out_messages: list[dict] = [{"role": "system", "content": system}] if system else []
    for message in request.get("messages") or []:
        parts: list[dict] = []
        for block in _blocks(message.get("content")):
            if block.get("type") == "text":
                parts.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "image":
                if not provider.vision:
                    raise ProviderRefused("provider has no image input")
                src = block["source"]
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
            else:
                raise ProviderRefused(f"unsupported content block {block.get('type')!r}")
        # Plain text as a string - the widest-supported shape.
        if all(p["type"] == "text" for p in parts):
            out_messages.append({"role": message["role"], "content": "\n\n".join(p["text"] for p in parts)})
        else:
            out_messages.append({"role": message["role"], "content": parts})

    body: dict[str, Any] = {"model": model, "messages": out_messages,
                            "max_tokens": request.get("max_tokens", 2048)}
    tools = request.get("tools") or []
    if tools:
        body["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"],
        }} for t in tools]
        choice = request.get("tool_choice") or {}
        if choice.get("type") == "tool":
            body["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    body.update(dict(provider.extra_body))
    return body


@dataclass(slots=True)
class ProviderAnswer:
    tool_input: dict | None
    text: str
    usage: SimpleNamespace  # input_tokens / output_tokens / cache_read_input_tokens / cache_creation_input_tokens
    cost_paise: int


async def call(ref: str, request: dict[str, Any], *, timeout: float = 60.0) -> ProviderAnswer:
    """Send an Anthropic-shaped request to an approved non-Anthropic provider."""
    name, model = parse_ref(ref)
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ProviderRefused(f"unknown provider {name!r}")
    if name not in approved():
        raise ProviderRefused(f"provider {name!r} is not in AI_APPROVED_PROVIDERS")
    key = getattr(settings, provider.key_setting, "") or ""
    if not key:
        raise ProviderRefused(f"{provider.key_setting} is not set")

    body = _translate(request, model, provider)
    headers = {h: v.format(key=key) for h, v in provider.headers}
    async with httpx.AsyncClient(timeout=timeout) as http:
        response = await http.post(f"{provider.base_url}/chat/completions", json=body, headers=headers)
    response.raise_for_status()
    data = response.json()

    message = (data.get("choices") or [{}])[0].get("message") or {}
    tool_input = None
    for call_ in message.get("tool_calls") or []:
        try:
            parsed = json.loads(call_["function"]["arguments"])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            tool_input = parsed
            break

    raw = data.get("usage") or {}
    prompt = int(raw.get("prompt_tokens") or 0)
    cached = int((raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    rate_in, rate_cached, rate_out = _rates(provider, model)
    rupees = ((prompt - cached) * rate_in + cached * rate_cached + completion * rate_out) / 1_000_000
    return ProviderAnswer(
        tool_input=tool_input,
        text=message.get("content") or "",
        usage=SimpleNamespace(input_tokens=prompt - cached, output_tokens=completion,
                              cache_read_input_tokens=cached, cache_creation_input_tokens=0),
        cost_paise=round(rupees * 100),
    )

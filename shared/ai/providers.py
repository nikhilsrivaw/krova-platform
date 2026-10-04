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
class ModelSpec:
    # Path appended to the provider's base URL.
    path: str
    # INR per million tokens: (input, cached input, output).
    pricing: tuple[float, float, float]
    # Extra body fields every request to this model carries.
    extra_body: tuple[tuple[str, Any], ...] = ()
    # Reasoning models spend output tokens thinking before they answer, and
    # those count against max_tokens. Added on top of the caller's budget so
    # the thinking cannot use up the room the actual answer needs - the
    # caller's max_tokens was sized for the answer alone.
    reasoning_headroom: int = 0
    # Accepts image input. Per model, since a provider can host both kinds.
    vision: bool = False


@dataclass(frozen=True, slots=True)
class Provider:
    base_url: str
    key_setting: str
    # Header name -> value template; "{key}" is replaced with the API key.
    headers: tuple[tuple[str, str], ...]
    vision: bool
    # By model-id prefix; longest match wins.
    models: tuple[tuple[str, ModelSpec], ...]


_SARVAM_HEADERS = (("api-subscription-key", "{key}"), ("Authorization", "Bearer {key}"))

PROVIDERS: dict[str, Provider] = {
    # docs.sarvam.ai - OpenAI-style tools and tool_choice, no image input,
    # hosted in India. Prices in INR from its pricing page (2026-08).
    #
    # sarvam-105b is a reasoning model: tested 2026-09-27 on the Type 2
    # conversations it spent ~2,500 output tokens thinking per extraction,
    # took over 60s on some, and dropped amounts and dates - not usable for
    # extraction. Kept only so it can be re-tested.
    #
    # The open-weight models are served on /v2. Gemma 4 31B does not reason
    # (answers straight away, so only the tool JSON is billed as output);
    # DeepSeek V4 Flash reasons but its output is the cheapest here.
    "sarvam": Provider(
        base_url="https://api.sarvam.ai",
        key_setting="sarvam_api_key",
        headers=_SARVAM_HEADERS,
        vision=False,
        models=(
            ("sarvam-105b", ModelSpec(
                path="/v1/chat/completions", pricing=(29.28, 10.98, 73.2),
                extra_body=(("reasoning_effort", "low"),), reasoning_headroom=6000,
            )),
            ("gemma-4-31b", ModelSpec(path="/v2/chat/completions", pricing=(36.6, 13.73, 91.5))),
            ("deepseekv4-flash", ModelSpec(
                path="/v2/chat/completions", pricing=(19.8, 0.63, 59.4),
                extra_body=(("reasoning_effort", "low"),), reasoning_headroom=4000,
            )),
        ),
    ),
    # Gemini's OpenAI-compatible endpoint (paid tier only - the free tier
    # trains on content). USD list prices converted.
    "gemini": Provider(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_setting="gemini_api_key",
        headers=(("Authorization", "Bearer {key}"),),
        vision=True,
        models=(
            ("gemini-3.5-flash-lite", ModelSpec(
                path="/chat/completions",
                pricing=(0.30 * _INR_PER_USD, 0.03 * _INR_PER_USD, 2.50 * _INR_PER_USD),
            )),
            ("gemini-3.8-flash", ModelSpec(
                path="/chat/completions",
                pricing=(0.75 * _INR_PER_USD, 0.075 * _INR_PER_USD, 3.75 * _INR_PER_USD),
            )),
        ),
    ),
    # Models hosted on AWS Bedrock, served on the bedrock-mantle endpoint in
    # Mumbai (verified by a live call). Prices are the AWS pricing page's
    # Mumbai on-demand rates (checked 2026-10). No caching is documented for
    # these, so cached input is priced at the normal input rate.
    "bedrock": Provider(
        base_url="https://bedrock-mantle.ap-south-1.api.aws/v1",
        key_setting="bedrock_api_key",
        headers=(("Authorization", "Bearer {key}"),),
        vision=False,
        models=(
            ("deepseek.v3.2", ModelSpec(
                path="/chat/completions",
                pricing=(0.74 * _INR_PER_USD, 0.74 * _INR_PER_USD, 2.22 * _INR_PER_USD),
                reasoning_headroom=1000,
            )),
            ("moonshotai.kimi-k2.5", ModelSpec(
                path="/chat/completions",
                pricing=(0.72 * _INR_PER_USD, 0.72 * _INR_PER_USD, 3.60 * _INR_PER_USD),
            )),
            # GLM 4.7 (Z.AI): in-region in Mumbai. Price from the AWS pricing page
            # (Mumbai, on-demand).
            ("zai.glm-4.7", ModelSpec(
                path="/chat/completions",
                pricing=(0.72 * _INR_PER_USD, 0.72 * _INR_PER_USD, 2.64 * _INR_PER_USD),
                reasoning_headroom=4000,
            )),
            # Mistral Large 3 on Bedrock, Mumbai on-demand (AWS pricing page).
            ("mistral.mistral-large-3-675b-instruct", ModelSpec(
                path="/chat/completions",
                vision=True,
                pricing=(0.59 * _INR_PER_USD, 0.59 * _INR_PER_USD, 1.76 * _INR_PER_USD),
            )),
            # OpenAI open-weight model on Bedrock, Mumbai on-demand. Reasoning model:
            # thinking tokens count against max_tokens, so headroom is added.
            ("openai.gpt-oss-120b", ModelSpec(
                path="/chat/completions",
                pricing=(0.18 * _INR_PER_USD, 0.18 * _INR_PER_USD, 0.71 * _INR_PER_USD),
                reasoning_headroom=2000,
            )),
            # Mistral Ministral 3, Mumbai on-demand (AWS pricing page). Small instruct models.
            ("mistral.ministral-3-14b-instruct", ModelSpec(
                path="/chat/completions",
                pricing=(0.24 * _INR_PER_USD, 0.24 * _INR_PER_USD, 0.24 * _INR_PER_USD),
            )),
            ("mistral.ministral-3-8b-instruct", ModelSpec(
                path="/chat/completions",
                pricing=(0.18 * _INR_PER_USD, 0.18 * _INR_PER_USD, 0.18 * _INR_PER_USD),
            )),
            # Google Gemma 3, Mumbai on-demand (user-supplied AWS Mumbai rates). Small instruct models.
            ("google.gemma-3-4b-it", ModelSpec(
                path="/chat/completions",
                pricing=(0.05 * _INR_PER_USD, 0.05 * _INR_PER_USD, 0.09 * _INR_PER_USD),
            )),
            ("google.gemma-3-12b-it", ModelSpec(
                path="/chat/completions",
                pricing=(0.11 * _INR_PER_USD, 0.11 * _INR_PER_USD, 0.34 * _INR_PER_USD),
            )),
            ("google.gemma-3-27b-it", ModelSpec(
                path="/chat/completions",
                pricing=(0.27 * _INR_PER_USD, 0.27 * _INR_PER_USD, 0.45 * _INR_PER_USD),
            )),
            ("minimax.minimax-m2.5", ModelSpec(
                path="/chat/completions",
                pricing=(0.36 * _INR_PER_USD, 0.36 * _INR_PER_USD, 1.44 * _INR_PER_USD),
                # Thinks before it answers; without room it hit max_tokens and
                # returned no tool call. Only tokens actually used are billed.
                reasoning_headroom=4000,
            )),
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


def _spec(provider: Provider, model: str) -> ModelSpec:
    """The model's own settings. An unlisted model is refused - its endpoint,
    reasoning behaviour and price are all unknown, and a guessed price would
    under-report spend."""
    match = max((p for p, _ in provider.models if model.startswith(p)), key=len, default=None)
    if match is None:
        raise ProviderRefused(f"model {model!r} is not configured for this provider")
    return dict(provider.models)[match]


def _blocks(content: Any) -> list[dict]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])


def _translate(request: dict[str, Any], model: str, provider: Provider) -> dict[str, Any]:
    spec = _spec(provider, model)
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
                if not (provider.vision or spec.vision):
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
                            "max_tokens": request.get("max_tokens", 2048) + spec.reasoning_headroom}
    tools = request.get("tools") or []
    if tools:
        body["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"],
        }} for t in tools]
        choice = request.get("tool_choice") or {}
        if choice.get("type") == "tool":
            body["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    body.update(dict(spec.extra_body))
    return body


@dataclass(slots=True)
class ProviderAnswer:
    tool_input: dict | None
    text: str
    usage: SimpleNamespace  # input_tokens / output_tokens / cache_read_input_tokens / cache_creation_input_tokens
    cost_paise: int
    finish_reason: str | None = None


async def call(ref: str, request: dict[str, Any], *, timeout: float = 120.0) -> ProviderAnswer:
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
    spec = _spec(provider, model)
    headers = {h: v.format(key=key) for h, v in provider.headers}
    async with httpx.AsyncClient(timeout=timeout) as http:
        response = await http.post(f"{provider.base_url}{spec.path}", json=body, headers=headers)
    response.raise_for_status()
    data = response.json()

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")
    tool_input = None
    for call_ in message.get("tool_calls") or []:
        try:
            parsed = json.loads(call_["function"]["arguments"])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            tool_input = parsed
            break

    if body.get("tool_choice") and tool_input is None:
        # A forced tool that never came back reads downstream as "found
        # nothing" - indistinguishable from a real empty answer. Say so.
        logger.warning(
            "provider %s returned no tool call (finish_reason=%s, content=%r)",
            ref, finish_reason, (message.get("content") or "")[:200],
        )

    raw = data.get("usage") or {}
    prompt = int(raw.get("prompt_tokens") or 0)
    cached = int((raw.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    rate_in, rate_cached, rate_out = spec.pricing
    rupees = ((prompt - cached) * rate_in + cached * rate_cached + completion * rate_out) / 1_000_000
    return ProviderAnswer(
        tool_input=tool_input,
        text=message.get("content") or "",
        usage=SimpleNamespace(input_tokens=prompt - cached, output_tokens=completion,
                              cache_read_input_tokens=cached, cache_creation_input_tokens=0),
        cost_paise=round(rupees * 100),
        finish_reason=finish_reason,
    )

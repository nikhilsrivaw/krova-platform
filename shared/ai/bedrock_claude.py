"""
Claude Haiku served by Amazon Bedrock in India, called with the Bedrock bearer key.

Used for voice when Anthropic's direct API is not available. The reply comes
back whole, not streamed, so the caller gets it in one piece and can check it
before anything is spoken. The India inference profile keeps inference in
India.
"""

from dataclasses import dataclass
from types import SimpleNamespace

import httpx

from shared.ai import client
from shared.config.settings import settings

HAIKU_PROFILE = "in.anthropic.claude-haiku-4-5-20251001-v1:0"
PRICING_MODEL = "claude-haiku-4-5-20251001"
ENDPOINT = "https://bedrock-runtime.ap-south-1.amazonaws.com/model/{model}/converse"
TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class Answer:
    text: str
    cost_paise: int
    usage: SimpleNamespace


async def converse(
    *, model_id: str, system: str, stable: str, volatile: str, max_tokens: int, task: str,
) -> Answer:
    """
    `stable` is the part that is identical across turns of a call (business
    details, live data); it gets a cache point so later turns read it cheaply.
    `volatile` is the conversation, which changes every turn.
    """
    body = {
        "system": [{"text": system}, {"cachePoint": {"type": "default"}}],
        "messages": [{
            "role": "user",
            "content": [
                {"text": stable},
                {"cachePoint": {"type": "default"}},
                {"text": volatile},
            ],
        }],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
        response = await http.post(
            ENDPOINT.format(model=model_id),
            headers={"Authorization": f"Bearer {settings.bedrock_api_key}"},
            json=body,
        )
    response.raise_for_status()
    data = response.json()

    usage_raw = data.get("usage") or {}
    usage = SimpleNamespace(
        input_tokens=int(usage_raw.get("inputTokens") or 0),
        output_tokens=int(usage_raw.get("outputTokens") or 0),
        cache_read_input_tokens=int(usage_raw.get("cacheReadInputTokens") or 0),
        cache_creation_input_tokens=int(usage_raw.get("cacheWriteInputTokens") or 0),
    )
    cost = client._cost_paise(
        PRICING_MODEL,
        usage.input_tokens,
        usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
    )
    client._log_usage(task, f"bedrock:{model_id}", usage, cost)

    blocks = (data.get("output") or {}).get("message", {}).get("content") or []
    text = "".join(block.get("text", "") for block in blocks)
    return Answer(text=text, cost_paise=cost, usage=usage)

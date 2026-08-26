"""
Prometheus metrics.

Voice quality regressions are invisible without measurement - a call
that works but answers 800ms slower feels broken and throws no error.
These are the numbers that actually matter for a voice agent, not
generic HTTP metrics.

The one to alert on is `voice_time_to_first_audio_seconds`. Everything
else is diagnosis.
"""

from prometheus_client import Counter, Gauge, Histogram

# Buckets chosen around what a caller perceives: under 1s feels natural,
# 1-2s is noticeable, past 3s sounds broken.
_LATENCY_BUCKETS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)

time_to_first_audio = Histogram(
    "voice_time_to_first_audio_seconds",
    "Silence the caller hears between finishing speaking and hearing the agent",
    ["tenant", "model"],
    buckets=_LATENCY_BUCKETS,
)

reply_duration = Histogram(
    "voice_reply_duration_seconds",
    "Full generation time for one agent reply",
    ["tenant", "model"],
    buckets=_LATENCY_BUCKETS,
)

active_calls = Gauge(
    "voice_active_calls",
    "Calls currently connected to this instance",
)

calls_total = Counter(
    "voice_calls_total",
    "Calls handled",
    ["tenant", "outcome"],
)

turns_total = Counter(
    "voice_turns_total",
    "Conversation turns",
    ["tenant", "role"],
)

barge_ins_total = Counter(
    "voice_barge_ins_total",
    "Times a caller interrupted the agent",
    ["tenant"],
)

errors_total = Counter(
    "voice_errors_total",
    "Failures by stage",
    ["stage"],
)

llm_tokens_total = Counter(
    "voice_llm_tokens_total",
    "Tokens consumed",
    ["tenant", "model", "kind"],
)

llm_cost_usd_total = Counter(
    "voice_llm_cost_usd_total",
    "LLM spend in USD",
    ["tenant", "model"],
)

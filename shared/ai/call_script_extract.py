"""
Reading a finished CallScript call's transcript into structured answers -
shared/db/models/call_campaign.py::CallScriptResponse's own source.

Same tool-calling extraction shape as call_summary.py, deliberately kept
as its own module rather than folded into it: that one classifies every
call the same fixed way (outcome/sentiment/topic); this one reads a
business-defined, per-script question list, which call_summary.py's
fixed SUMMARIZE_TOOL schema has no way to represent.

Same never-invent discipline as every extraction in this codebase: a
question the transcript doesn't actually cover is left out of `answers`
entirely, never filled in with a guess - the tool schema itself only
asks for questions that were genuinely answered, not one slot per
question with an "unanswered" placeholder to reach for.
"""

from dataclasses import dataclass, field

from shared.ai import client
from shared.utils.logging import get_logger

logger = get_logger(__name__)

EXTRACT_TOOL = {
    "name": "record_call_script_result",
    "description": "Record the questions this call actually got a real answer to, and how it went.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "description": "One entry per question the transcript genuinely answered - omit any question that was never actually covered, never guess at one.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The exact question text, copied from the list given.",
                        },
                        "answer": {
                            "type": "string",
                            "description": "What the caller actually said, in their own words or a faithful paraphrase - never invented.",
                        },
                    },
                    "required": ["question", "answer"],
                },
            },
            "score": {
                "type": "integer",
                "description": "0-100, how good a fit this caller seems based only on what they said - ONLY set this for a lead_qualification call, omit entirely for a survey.",
            },
            "summary": {
                "type": "string",
                "description": "One plain-English sentence a busy owner can read in two seconds.",
            },
        },
        "required": ["answers", "summary"],
    },
}

SYSTEM = """You read the transcript of a finished structured phone call - an agent working through a fixed list of questions on a business's behalf - and record exactly what was actually answered.

Never invent or infer an answer the caller did not actually give. A question the agent asked but the caller never really answered (talked around it, the call ended first, gave a non-answer) must not appear in `answers` at all - leaving it out is the honest record, not a failure.

If this was a lead_qualification call, also score it 0-100 on fit, based strictly on what the caller actually said - never on tone or guesswork about things they didn't state. For a survey call, do not include a score at all."""


@dataclass(slots=True)
class ScriptExtractResult:
    answers: dict[str, str] = field(default_factory=dict)
    score: int | None = None
    summary: str = ""
    cost_paise: int = 0


def _render(transcript: list[dict]) -> str:
    lines = []
    for turn in transcript:
        who = "Caller" if turn.get("role") == "caller" else "Agent"
        lines.append(f"{who}: {turn.get('text', '')}")
    return "\n".join(lines)


async def extract(
    transcript: list[dict], *, questions: list[str], purpose: str, business_name: str | None = None,
) -> ScriptExtractResult | None:
    """Structured answers for a finished CallScript call, or None if there's nothing to read (an empty transcript)."""
    if not transcript or not questions:
        return None

    business_line = f"Business: {business_name}\n\n" if business_name else ""
    numbered_questions = "\n".join(f"- {q}" for q in questions)
    prompt = (
        f"{business_line}This was a {purpose.replace('_', ' ')} call. "
        f"The questions on the script were:\n{numbered_questions}\n\n"
        f"Call transcript:\n{_render(transcript)}\n\n"
        "Record what was actually answered."
    )

    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        speed="fast",
        tool=EXTRACT_TOOL,
        max_tokens=600,
    )

    tool_input = completion.tool_input or {}
    raw_answers = tool_input.get("answers") or []
    # Only entries genuinely shaped like {question, answer} count - a
    # malformed entry is dropped, not partially trusted.
    answers: dict[str, str] = {}
    for entry in raw_answers:
        if not isinstance(entry, dict):
            continue
        q = (entry.get("question") or "").strip()
        a = (entry.get("answer") or "").strip()
        if q and a:
            answers[q] = a

    score = tool_input.get("score") if purpose == "lead_qualification" else None
    if score is not None:
        try:
            score = max(0, min(100, int(score)))
        except (TypeError, ValueError):
            score = None

    summary = (tool_input.get("summary") or "").strip()
    if not summary:
        logger.warning("call script extraction came back with no summary text")
        summary = "No summary available."

    return ScriptExtractResult(
        answers=answers, score=score, summary=summary, cost_paise=completion.cost_paise,
    )

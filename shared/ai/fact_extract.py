"""
Commitments, signals and a quotation from one model call per message.

The three extractors used to each send the same conversation, so a single
inbound message paid for its context up to three times. This asks for all of
them in one tool call. Each part keeps the schema, prompt section and parsing
from its own module, so the rules tuned there still apply; only the number of
calls changes.

Which parts are asked for is decided by the caller, as before: signals depend
on the business's capability and the message direction, and the quotation on
the quotations capability and whether the window could contain a price.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from shared.ai import client, commitments, quotation_extract, signals

SignalMode = Literal["off", "product", "conversation"]

TASK = "extract_message_facts"
TOOL_NAME = "record_message_facts"


@dataclass(slots=True)
class FactExtraction:
    commitments: commitments.Extraction
    signals: signals.Extraction
    quotation: quotation_extract.Extraction
    cost_paise: int


async def extract(
    *,
    messages: list[dict],
    business_context: str,
    signal_mode: SignalMode = "off",
    want_quotation: bool = False,
    now: datetime | None = None,
    cache: bool = False,
) -> FactExtraction:
    now = now or datetime.now(timezone.utc)
    usable = [m for m in messages if (m.get("text") or "").strip()]
    if not usable:
        return FactExtraction(
            commitments=commitments.Extraction(commitments=[], cost_paise=0, rejected=0),
            signals=signals.Extraction(signals=[], cost_paise=0, rejected=0),
            quotation=quotation_extract.Extraction(quotation=None, cost_paise=0),
            cost_paise=0,
        )

    valid_ids = {str(m["id"]) for m in usable}

    sections = [commitments.SYSTEM]
    properties = {"commitments": commitments.EXTRACT_TOOL["input_schema"]["properties"]["commitments"]}
    required = ["commitments"]
    instructions = ["Record every promise in this conversation. If there are none, return an empty list."]

    kinds: tuple[str, ...] = ()
    if signal_mode != "off":
        product = signal_mode == "product"
        kinds = signals.PRODUCT_KINDS if product else signals.CONVERSATION_KINDS
        sections.append(signals.SYSTEM if product else signals.CONVERSATION_SYSTEM)
        properties["signals"] = signals._tool(kinds)["input_schema"]["properties"]["signals"]
        required.append("signals")
        instructions.append(
            "Record every product feedback signal in this conversation. If there are none, return an empty list."
            if product
            else "Record every complaint, churn risk, competitor mention or piece of praise from the customer. "
            "If there are none, return an empty list."
        )

    if want_quotation:
        sections.append(quotation_extract.SYSTEM)
        properties["quotation"] = quotation_extract.EXTRACT_TOOL["input_schema"]
        required.append("quotation")
        instructions.append("Did the business quote a price here? If not, set found to false.")

    tool = {
        "name": TOOL_NAME,
        "description": (
            "Record the promises, signals and quotation found in this conversation. "
            "Empty lists and found=false are correct when there is nothing."
        ),
        "input_schema": {"type": "object", "properties": properties, "required": required},
    }

    prompt = (
        f"Business context:\n{business_context}\n\n"
        f"Today is {now.strftime('%d %B %Y')}.\n\n"
        f"Conversation:\n{commitments._render(usable)}\n\n"
        + " ".join(instructions)
    )

    completion = await client.complete(
        system="\n\n".join(sections),
        messages=[{"role": "user", "content": prompt}],
        cache_system=cache,
        speed="deep",
        task=TASK,
        tool=tool,
        max_tokens=4096,
    )
    data = completion.tool_input or {}
    cost = completion.cost_paise

    found, rejected = commitments.parse_items(data.get("commitments"), valid_ids, now)
    commitment_result = commitments.Extraction(commitments=found, cost_paise=cost, rejected=rejected)

    signal_result = signals.Extraction(signals=[], cost_paise=0, rejected=0)
    if signal_mode != "off":
        found_signals, signal_rejected = signals.parse_items(
            data.get("signals"), kinds, signal_mode == "product", valid_ids
        )
        signal_result = signals.Extraction(
            signals=found_signals, cost_paise=0, rejected=signal_rejected
        )

    quotation_result = quotation_extract.Extraction(quotation=None, cost_paise=0)
    if want_quotation:
        quotation_result = quotation_extract.parse_quotation(
            data.get("quotation") or {}, valid_ids, 0
        )

    return FactExtraction(
        commitments=commitment_result,
        signals=signal_result,
        quotation=quotation_result,
        cost_paise=cost,
    )

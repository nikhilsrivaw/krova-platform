"""
Reading an owner's file (price list, menu, brochure, FAQ sheet) into the Business Card - once.

The old upload kept the whole file and the AI read all of it before every reply, so a 30-page brochure was paid for on
every customer message. Here the file is read one time, the facts that answer the card's questions are pulled out
within each field's character limit, and the owner checks them before anything is saved. The file itself is not kept.

Never invents: a field the file does not answer comes back empty, the same discipline as every other AI-drafted thing
on this platform - drafted, then a person decides.
"""

import base64
import io
import re
from dataclasses import dataclass, field as dc_field
from typing import Any

from pypdf import PdfReader

from shared.ai import client
from shared.verticals import knowledge_card, knowledge_lookup
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# What is read, by type. A PDF's text is pulled out here and sent as text; a photo goes to the model as an image.
PDF_TYPES = {"application/pdf"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
TEXT_EXTENSIONS = (".txt", ".md", ".csv")
_EXT_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

MAX_FILE_BYTES = 5 * 1024 * 1024
# A text file past this is cut: the card holds about 4,000 characters, so the first ~10k tokens of a file are plenty
# to fill it, and it keeps one read cheap.
MAX_TEXT_CHARS = 40_000
MAX_PDF_PAGES = 30
# Room for the card plus a full catalogue (300 rows is about 6,000 tokens). Only what is written is billed.
MAX_OUTPUT_TOKENS = 9000

SYSTEM = """You fill in a small business's knowledge card from a document the owner gave you (a price list, menu,
brochure, rate card, FAQ sheet or a photo of one).

The card is read by the business's AI assistant before every customer reply, so it must be short and exact.

Rules:
- Use ONLY facts written in the document. If the document does not answer a field, return an empty string for it.
  Never guess, never fill in typical values, never invent prices, dates, names, phone numbers or policies.
- Keep every field within its character limit. Prefer the facts customers ask about most; drop marketing words.
- Prices exactly as written, with their unit ("Rs 1,200 per month"). For a long list, put the most important items in
  "offerings" (up to 10 lines) - the full list stays with the owner.
- "full_list" is only a web link written in the document (http... or https...), otherwise empty.
- "offers" only for an offer or notice the document states, with its end date if given.
- Top questions: only question-and-answer pairs the document itself answers, at most the number asked for,
  each answer one or two short sentences.
- Catalogue: EVERY item the document prices or lists (products, services, plans, models, menu dishes), one row each:
  its name, its price exactly as written with its unit, and a few words of detail (size, variant, what is included).
  This is the long list - "offerings" above keeps only the top ones.
- More questions: further question-and-answer pairs the document answers that did not fit in the top questions.
- Language: write in ENGLISH. Only if the document itself is written in Hindi or Hinglish, write in that. Never French,
  Spanish or any other language, whatever language you usually answer in."""


@dataclass(slots=True)
class CardReading:
    fields: dict[str, str]
    faqs: list[tuple[str, str]] = dc_field(default_factory=list)
    truncated: bool = False
    cost_paise: int = 0
    # the opened-when-needed knowledge the file also holds (shared/verticals/knowledge_lookup.py)
    catalogue: list[dict[str, str]] = dc_field(default_factory=list)
    bank: list[tuple[str, str]] = dc_field(default_factory=list)


class UnreadableFile(Exception):
    """The file is of a kind this cannot read, or has nothing in it."""


def media_type_for(filename: str, content_type: str | None) -> str:
    """The type the file is read as: 'text', or a PDF/image media type. Raises UnreadableFile otherwise."""
    lowered = (filename or "").lower()
    if lowered.endswith(TEXT_EXTENSIONS) or (content_type or "").startswith("text/"):
        return "text"
    for ext, mt in _EXT_TYPES.items():
        if lowered.endswith(ext):
            return mt
    if content_type in PDF_TYPES | IMAGE_TYPES:
        return content_type
    raise UnreadableFile("Upload a PDF, a photo (JPG, PNG, WebP) or a text file (.txt, .md, .csv).")


def _tool(fields: list[dict[str, Any]], faq_room: int, bank_room: int = 0) -> dict[str, Any]:
    props: dict[str, Any] = {
        f["key"]: {
            "type": "string",
            "description": f"{f['title']}: {f['question']} At most {f['limit']} characters. Empty if the document does not say.",
        }
        for f in fields
    }
    props["faqs"] = {
        "type": "array",
        "maxItems": faq_room,
        "description": f"Up to {faq_room} questions customers would ask that the document answers.",
        "items": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": f"At most {knowledge_card.FAQ_QUESTION_LIMIT} characters."},
                "answer": {"type": "string", "description": f"At most {knowledge_card.FAQ_ANSWER_LIMIT} characters."},
            },
            "required": ["question", "answer"],
        },
    }
    limits = knowledge_lookup.CATALOGUE_LIMITS
    props["catalogue"] = {
        "type": "array",
        "maxItems": knowledge_lookup.CATALOGUE_ROWS_LIMIT,
        "description": "Every priced or listed item in the document, one row each. Empty if it lists none.",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": f"At most {limits['name']} characters."},
                "price": {"type": "string", "description": f"Exactly as written, with its unit. At most {limits['price']} characters."},
                "details": {"type": "string", "description": f"Size, variant or what is included. At most {limits['details']} characters."},
            },
            "required": ["name", "price", "details"],
        },
    }
    props["more_questions"] = {
        "type": "array",
        "maxItems": bank_room,
        "description": f"Up to {bank_room} more questions the document answers, beyond the top questions.",
        "items": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": f"At most {knowledge_lookup.BANK_QUESTION_LIMIT} characters."},
                "answer": {"type": "string", "description": f"At most {knowledge_lookup.BANK_ANSWER_LIMIT} characters."},
            },
            "required": ["question", "answer"],
        },
    }
    return {
        "name": "fill_card",
        "description": "Fill the knowledge card from the document.",
        "input_schema": {"type": "object", "properties": props, "required": [f["key"] for f in fields] + ["faqs"]},
    }


def pdf_text(raw: bytes) -> str:
    """
    The text layer of a PDF, read here rather than by the model: it works with every model (the Bedrock ones take no
    PDF input) and a page of text costs far fewer tokens than a page sent as a document. A scanned PDF has no text
    layer - the owner is asked for a photo instead, which the model can read.
    """
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnreadableFile("That PDF is locked with a password. Upload an unlocked copy, or a photo of it.")
        parts: list[str] = []
        size = 0
        for page in reader.pages[:MAX_PDF_PAGES]:
            text = (page.extract_text() or "").strip()
            parts.append(text)
            size += len(text)
            if size > MAX_TEXT_CHARS:
                break
    except UnreadableFile:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken PDF is the owner's file, not our error
        raise UnreadableFile("We could not open that PDF. Try a photo of it instead.") from exc
    text = "\n\n".join(p for p in parts if p)
    if len(text) < 20:
        raise UnreadableFile("This PDF looks like a scan with no text in it. Upload a photo of the page instead.")
    return text


def _content(raw: bytes, media_type: str) -> tuple[dict[str, Any], bool]:
    if media_type in ("text", *PDF_TYPES):
        text = pdf_text(raw) if media_type in PDF_TYPES else raw.decode("utf-8", errors="replace").strip()
        if not text:
            raise UnreadableFile("We could not find any text in that file.")
        return {"type": "text", "text": f"<document>\n{text[:MAX_TEXT_CHARS]}\n</document>"}, len(text) > MAX_TEXT_CHARS
    data = base64.standard_b64encode(raw).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}, False


def clean(fields: list[dict[str, Any]], tool_input: dict[str, Any] | None, faq_room: int) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Keep only usable answers: within limits, links that are links, no empty pairs."""
    raw = tool_input or {}
    out: dict[str, str] = {}
    for f in fields:
        value = raw.get(f["key"])
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()[: f["limit"]]
        if knowledge_card.check_value(f, value) is None:
            out[f["key"]] = value
    faqs: list[tuple[str, str]] = []
    for pair in raw.get("faqs") or []:
        if not isinstance(pair, dict):
            continue
        q = str(pair.get("question") or "").strip()[: knowledge_card.FAQ_QUESTION_LIMIT]
        a = str(pair.get("answer") or "").strip()[: knowledge_card.FAQ_ANSWER_LIMIT]
        if q and a:
            faqs.append((q, a))
    return out, faqs[:faq_room]


# Words common in French and Spanish and rare in English, Hindi or Hinglish. Enough of them means the model answered
# in the wrong language.
_FOREIGN_WORDS = {"est", "les", "des", "une", "pour", "avec", "dans", "sur", "vous", "nous", "aux", "qui", "sont",
                  "entreprise", "appels", "sont", "los", "las", "para", "con", "una", "del", "empresa"}


def clean_lookup(tool_input: dict[str, Any] | None, bank_room: int) -> tuple[list[dict[str, str]], list[tuple[str, str]]]:
    """Catalogue rows within their limits (no nameless rows), and saved answers within theirs."""
    raw = tool_input or {}
    rows = knowledge_lookup.clean_rows([r for r in raw.get("catalogue") or [] if isinstance(r, dict)])
    bank: list[tuple[str, str]] = []
    for pair in raw.get("more_questions") or []:
        if not isinstance(pair, dict):
            continue
        q = str(pair.get("question") or "").strip()[: knowledge_lookup.BANK_QUESTION_LIMIT]
        a = str(pair.get("answer") or "").strip()[: knowledge_lookup.BANK_ANSWER_LIMIT]
        if q and a:
            bank.append((q, a))
    return rows, bank[: max(0, bank_room)]


def looks_foreign(values: dict[str, str], faqs: list[tuple[str, str]]) -> bool:
    text = " ".join([*values.values(), *(q + " " + a for q, a in faqs)]).lower()
    words = re.findall(r"[a-zà-ÿ]+", text)
    if len(words) < 8:
        return False
    hits = sum(1 for w in words if w in _FOREIGN_WORDS)
    return hits >= 4 and hits / len(words) > 0.04


async def read_into_card(
    *, raw: bytes, filename: str, content_type: str | None, vertical: str, faq_room: int, bank_room: int = 0,
) -> CardReading:
    """One read of the file; returns suggestions for the card. Saves nothing."""
    if len(raw) > MAX_FILE_BYTES:
        raise UnreadableFile("That file is too large. Keep it under 5 MB.")
    if not raw:
        raise UnreadableFile("That file is empty.")
    media_type = media_type_for(filename, content_type)
    block, truncated = _content(raw, media_type)
    fields = knowledge_card.fields_for(vertical)
    tool = _tool(fields, max(0, faq_room), max(0, bank_room))
    ask = "Fill the card from this document. Write in English (or Hindi/Hinglish only if the document is in that)."
    completion = await client.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": [block, {"type": "text", "text": ask}]}],
        speed="fast", task="knowledge_read_file", tool=tool, max_tokens=MAX_OUTPUT_TOKENS,
    )
    cost = completion.cost_paise
    values, faqs = clean(fields, completion.tool_input, max(0, faq_room))
    if looks_foreign(values, faqs):
        # Some models drift into their own home language (Mistral into French). One retry, told plainly.
        logger.warning("knowledge file read came back in another language, asking again file=%s", filename)
        completion = await client.complete(
            system=SYSTEM,
            messages=[{"role": "user", "content": [block, {"type": "text", "text": ask + " Your last answer was in French - answer in English."}]}],
            speed="fast", task="knowledge_read_file", tool=tool, max_tokens=MAX_OUTPUT_TOKENS,
        )
        cost += completion.cost_paise
        values, faqs = clean(fields, completion.tool_input, max(0, faq_room))
        if looks_foreign(values, faqs):
            raise UnreadableFile("We could not read this file in English. Try again, or type it into the card.")
    rows, bank = clean_lookup(completion.tool_input, bank_room)
    if not values and not faqs and not rows and not bank:
        logger.info("knowledge file read found nothing usable file=%s", filename)
    return CardReading(fields=values, faqs=faqs, truncated=truncated, cost_paise=cost, catalogue=rows, bank=bank)

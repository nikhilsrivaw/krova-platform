"""
Reading an owner's file (price list, menu, brochure, FAQ sheet) into the Business Card - once.

The old upload kept the whole file and the AI read all of it before every reply, so a 30-page brochure was paid for on
every customer message. Here the file is read one time, the facts that answer the card's questions are pulled out
within each field's character limit, and the owner checks them before anything is saved. The file itself is not kept.

Never invents: a field the file does not answer comes back empty, the same discipline as every other AI-drafted thing
on this platform - drafted, then a person decides.
"""

import asyncio
import base64
import io
import re
from dataclasses import dataclass, field as dc_field
from typing import Any

from pypdf import PdfReader

from shared.ai import client, pdf_structure
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
    # a large PDF read in parts (read_large_pdf): pages read, pages with no text (scans), and found more than fits
    pages_read: int = 0
    pages_without_text: int = 0
    more_than_fits: bool = False
    # Checked against the document (check_catalogue / check_answers): "ok" or "look", and the page a course row is on.
    catalogue_checks: list[tuple[str, int | None]] = dc_field(default_factory=list)
    faq_checks: list[str] = dc_field(default_factory=list)
    bank_checks: list[str] = dc_field(default_factory=list)


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


def _pdfium_text(raw: bytes) -> str | None:
    """
    The text layer read with PDFium (Chrome's PDF engine). pypdf drops or reorders Hindi and other Indic letters - a
    test prospectus came out as "म" for "में" and "परी ाएँ" for "परीक्षाएँ" - while PDFium reads them as written.
    None if PDFium cannot open the file; pypdf then has its go, with its clearer messages for locked or broken files.
    """
    try:
        import pypdfium2

        doc = pypdfium2.PdfDocument(raw)
        try:
            parts: list[str] = []
            size = 0
            for i in range(min(len(doc), MAX_PDF_PAGES)):
                page = doc[i]
                text_page = page.get_textpage()
                text = (text_page.get_text_range() or "").replace("\r\n", "\n").strip()
                text_page.close()
                page.close()
                parts.append(text)
                size += len(text)
                if size > MAX_TEXT_CHARS:
                    break
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 - pypdf is the fallback
        return None
    return "\n\n".join(p for p in parts if p)


def pdf_text(raw: bytes) -> str:
    """
    The text layer of a PDF, read here rather than by the model: it works with every model (the Bedrock ones take no
    PDF input) and a page of text costs far fewer tokens than a page sent as a document. A scanned PDF has no text
    layer - the owner is asked for a photo instead, which the model can read.
    """
    text = _pdfium_text(raw)
    if text and len(text) >= 20:
        return text
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


# ── a large PDF, read in parts ─────────────────────────────────────────────────────────────────────────────────────
#
# A college prospectus runs to 100-300 pages with fee and eligibility tables. Only a type whose template turns on
# file_reading.large_files gets this (today: institution). The PDF's text is cut into parts of a few pages, each part is
# read like a small file, and the findings are merged into one set of suggestions for the same review screen. The file
# is still read once and not kept, so a reply never pays for the whole document. A 150-page PDF is about 13 parts.

MAX_LARGE_FILE_BYTES = 25 * 1024 * 1024
MAX_LARGE_PAGES = 400
PART_CHARS = 30_000  # about 8-10k tokens of input per part, so each answer has room for every row it finds
PARTS_AT_ONCE = 4
# Answers asked of one part: 40 short answers plus its courses fit in MAX_OUTPUT_TOKENS, so the reply is never cut off.
PART_ANSWERS = 40
# A page with less text than this is a scan, a photo or a blank divider.
PAGE_TEXT_MIN = 40


def reading_options(vertical: str) -> dict:
    """The template's file_reading block: {"large_files": bool, "guide": str}. Empty for a type that has none."""
    from shared import verticals

    try:
        block = verticals.get(vertical).get("file_reading") or {}
    except verticals.UnknownVertical:
        return {}
    return block if isinstance(block, dict) else {}


def pdf_pages(raw: bytes, max_pages: int = MAX_LARGE_PAGES) -> list[str]:
    """Each page's text, in order (PDFium, which keeps Hindi intact; pypdf if PDFium cannot open the file)."""
    try:
        import pypdfium2

        doc = pypdfium2.PdfDocument(raw)
        try:
            pages: list[str] = []
            for i in range(min(len(doc), max_pages)):
                page = doc[i]
                text_page = page.get_textpage()
                pages.append((text_page.get_text_range() or "").replace("\r\n", "\n").strip())
                text_page.close()
                page.close()
            return pages
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 - pypdf is the fallback
        pass
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnreadableFile("That PDF is locked with a password. Upload an unlocked copy.")
        return [(p.extract_text() or "").strip() for p in reader.pages[:max_pages]]
    except UnreadableFile:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken PDF is the owner's file, not our error
        raise UnreadableFile("We could not open that PDF.") from exc


def page_parts(pages: list[str], part_chars: int = PART_CHARS) -> list[tuple[int, int, str]]:
    """(first page, last page, text) parts of about part_chars each, cut only between pages. Pages are numbered from 1."""
    parts: list[tuple[int, int, str]] = []
    first, last, chunk, size = 0, 0, [], 0
    for number, text in enumerate(pages, start=1):
        if len(text) < PAGE_TEXT_MIN:
            continue
        if chunk and size + len(text) > part_chars:
            parts.append((first, last, "\n\n".join(chunk)))
            chunk, size = [], 0
        if not chunk:
            first = number
        chunk.append(f"[Page {number}]\n{text[:part_chars]}")
        size += len(text)
        last = number
    if chunk:
        parts.append((first, last, "\n\n".join(chunk)))
    return parts


Part = tuple[dict[str, str], list[tuple[str, str]], list[dict[str, str]], list[tuple[str, str]]]


def merge_readings(
    readings: list[Part], *, faq_room: int, bank_room: int,
) -> tuple[dict[str, str], list[tuple[str, str]], list[dict[str, str]], list[tuple[str, str]], bool]:
    """
    One set of suggestions from the parts, in page order. A card field keeps the first part's answer (the front of a
    prospectus says who and where). Courses and answers are kept once by name or question; past the room, the rest is
    dropped and the last value says so.
    """
    fields: dict[str, str] = {}
    faqs: list[tuple[str, str]] = []
    rows: list[dict[str, str]] = []
    bank: list[tuple[str, str]] = []
    seen_q: set[str] = set()
    seen_rows: set[str] = set()
    found_q = found_rows = 0
    for values, part_faqs, part_rows, part_bank in readings:
        for key, value in values.items():
            fields.setdefault(key, value)
        for row in part_rows:
            name = row["name"].strip().lower()
            if name in seen_rows:
                continue
            seen_rows.add(name)
            found_rows += 1
            if len(rows) < knowledge_lookup.CATALOGUE_ROWS_LIMIT:
                rows.append(row)
        top = set(part_faqs)
        for q, a in [*part_faqs, *part_bank]:
            key = q.strip().lower()
            if key in seen_q:
                continue
            seen_q.add(key)
            found_q += 1
            if (q, a) in top and len(faqs) < faq_room:
                faqs.append((q, a))
            elif len(bank) < bank_room:
                bank.append((q, a))
    more = found_rows > len(rows) or found_q > len(faqs) + len(bank)
    return fields, faqs, rows, bank, more


async def _read_part(
    first: int, last: int, total: int, text: str, *, fields: list[dict[str, Any]], tool: dict[str, Any], system: str,
    faq_room: int, bank_room: int,
) -> tuple[Part, int]:
    ask = (
        f"These are pages {first} to {last} of a {total}-page document. Fill what these pages say and leave the rest "
        "empty - the other pages are read separately. List every course and every answer on these pages. Write in "
        "English (or Hindi/Hinglish only if the document is in that)."
    )
    block = {"type": "text", "text": f"<document>\n{text}\n</document>"}
    cost = 0
    for attempt in range(2):
        note = "" if attempt == 0 else " Your last answer was in French - answer in English."
        completion = await client.complete(
            system=system, messages=[{"role": "user", "content": [block, {"type": "text", "text": ask + note}]}],
            speed="fast", task="knowledge_read_file", tool=tool, max_tokens=MAX_OUTPUT_TOKENS,
        )
        cost += completion.cost_paise
        values, faqs = clean(fields, completion.tool_input, faq_room)
        if not looks_foreign(values, faqs):
            rows, bank = clean_lookup(completion.tool_input, bank_room)
            return (values, faqs, rows, bank), cost
    logger.warning("large file part kept answering in another language, skipped pages=%s-%s", first, last)
    return ({}, [], [], []), cost


async def read_large_pdf(*, raw: bytes, filename: str, vertical: str, faq_room: int, bank_room: int) -> CardReading:
    """A long PDF, read in parts a few at a time; one merged set of suggestions for the owner to check. Saves nothing."""
    if len(raw) > MAX_LARGE_FILE_BYTES:
        raise UnreadableFile("That file is too large. Keep it under 25 MB.")
    if not raw:
        raise UnreadableFile("That file is empty.")
    # Tables laid out with their column names and Kruti Dev Hindi converted (shared/ai/pdf_structure.py); plain page
    # text when the PDF cannot be laid out. Both are slow on a long file, so they run off the event loop.
    laid_out = await asyncio.to_thread(pdf_structure.read, raw, MAX_LARGE_PAGES)
    if laid_out:
        pages = [p.text for p in laid_out]
        table_rows = [(n, row) for n, p in enumerate(laid_out, start=1) for row in p.rows]
    else:
        pages = await asyncio.to_thread(pdf_pages, raw)
        table_rows = []
    parts = page_parts(pages)
    if not parts:
        raise UnreadableFile("This PDF looks like a scan with no text in it. Upload a PDF whose text can be selected.")
    fields = knowledge_card.fields_for(vertical)
    faq_room, bank_room = max(0, faq_room), max(0, bank_room)
    part_room = min(bank_room, PART_ANSWERS)
    tool = _tool(fields, faq_room, part_room)
    guide = str(reading_options(vertical).get("guide") or "").strip()
    system = SYSTEM + (f"\n\nThis kind of document:\n{guide}" if guide else "")
    gate = asyncio.Semaphore(PARTS_AT_ONCE)

    async def one(part: tuple[int, int, str]) -> tuple[Part, int]:
        async with gate:
            return await _read_part(part[0], part[1], len(pages), part[2], fields=fields, tool=tool, system=system,
                                    faq_room=faq_room, bank_room=part_room)

    results = await asyncio.gather(*(one(p) for p in parts), return_exceptions=True)
    readings: list[Part] = []
    cost = failed = 0
    for (first, last, _), result in zip(parts, results):
        if isinstance(result, BaseException):
            failed += 1
            logger.warning("large file part failed pages=%s-%s (%s)", first, last, type(result).__name__)
            continue
        readings.append(result[0])
        cost += result[1]
    if not readings:
        raise next(r for r in results if isinstance(r, BaseException))
    values, faqs, rows, bank, more = merge_readings(readings, faq_room=faq_room, bank_room=bank_room)
    whole = "\n".join(pages)
    catalogue_checks = check_catalogue(rows, table_rows, whole)
    logger.info(
        "large file read file=%s pages=%s parts=%s failed=%s rows=%s rows_to_check=%s cost_paise=%s", filename,
        len(pages), len(parts), failed, len(rows), sum(1 for c, _ in catalogue_checks if c != "ok"), cost,
    )
    return CardReading(
        fields=values, faqs=faqs, truncated=failed > 0 or len(pages) >= MAX_LARGE_PAGES, cost_paise=cost,
        catalogue=rows, bank=bank, pages_read=len(pages),
        pages_without_text=sum(1 for p in pages if len(p) < PAGE_TEXT_MIN), more_than_fits=more,
        catalogue_checks=catalogue_checks, faq_checks=check_answers(faqs, whole), bank_checks=check_answers(bank, whole),
    )


# ── checking what was read against the document ───────────────────────────────────────────────────────────────────
#
# A model reading a fee table can still put one course's fee on the next course. So every course row is looked for in
# the document's own table rows: "ok" when one table row holds both the fee's number and the course's name, "look"
# otherwise - shown to the owner with the page, before anything is saved. Answers are checked for numbers the document
# does not contain. No AI: plain matching, so it costs nothing and cannot itself make things up.

_GENERIC_NAME_WORDS = {
    "of", "in", "and", "the", "for", "a", "an", "course", "programme", "program", "degree", "diploma", "certificate",
    "bachelor", "master", "masters", "post", "graduate", "graduation", "pg", "ug", "science", "arts", "ma", "ba", "msc",
    "bsc", "mcom", "bcom", "m", "b", "year", "years", "fee", "fees",
}


def _figures(text: str, shortest: int = 3) -> list[str]:
    """The numbers in a text, commas dropped ("Rs 1,20,000" -> "120000"), of at least `shortest` digits."""
    return [n for n in (m.replace(",", "") for m in re.findall(r"\d[\d,]*\d|\d", text or "")) if len(n) >= shortest]


def _has_figure(text: str, figure: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(figure)}(?!\d)", (text or "").replace(",", "")) is not None


def _name_words(name: str) -> tuple[list[str], list[str]]:
    words = [w for w in re.findall(r"[^\W_]+", (name or "").lower()) if len(w) >= 2 or w.isdigit()]
    distinct = [w for w in words if w not in _GENERIC_NAME_WORDS]
    return distinct, words


def check_catalogue(rows: list[dict], table_rows: list[tuple[int, str]], whole: str) -> list[tuple[str, int | None]]:
    """Per course row: ("ok", page) when one table row of the document holds its fee and its name; else ("look", page)."""
    out: list[tuple[str, int | None]] = []
    for row in rows:
        figures = _figures(row.get("price", ""))
        distinct, words = _name_words(row.get("name", ""))
        named = [(page, text) for page, text in table_rows if any(w in text.lower() for w in (distinct or words))]
        both = [page for page, text in named if figures and all(_has_figure(text, f) for f in figures)]
        if both:
            out.append(("ok", both[0]))
        elif figures and not table_rows and all(_has_figure(whole, f) for f in figures):
            out.append(("ok", None))  # no tables to hold it against; the fee is at least in the document
        else:
            out.append(("look", named[0][0] if named else None))
    return out


def check_answers(pairs: list[tuple[str, str]], whole: str) -> list[str]:
    """Per answer: "look" when it gives a number (a fee, a date, a count) the document does not contain, else "ok"."""
    return ["look" if any(not _has_figure(whole, f) for f in _figures(a, 2)) else "ok" for _, a in pairs]

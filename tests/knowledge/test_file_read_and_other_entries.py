"""
Reading a file into the card once (instead of keeping the whole file for the AI to read on every message), and the
small budget for "other entries". No network: the Claude call is replaced by a scripted answer.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.api.routers import knowledge
from shared.ai import knowledge_reader as kr
from shared.verticals import knowledge_card as kc


# ── other entries: few and short ──────────────────────────────────────────────

def _item(topic=None, card=None):
    return SimpleNamespace(topic=topic, extra={"card": card} if card else {})


def test_only_free_notes_count_as_other_entries():
    assert kc.is_other(_item())
    assert not kc.is_other(_item(topic="when"))
    assert not kc.is_other(_item(topic=kc.TEAM_TOPIC))
    assert not kc.is_other(_item(card="faq"))


def test_other_entries_have_a_count_limit():
    full = [(i, 10) for i in range(kc.OTHER_ITEMS_LIMIT)]
    assert kc.check_other_room(full[:-1], 10) is None
    assert "switched on" in kc.check_other_room(full, 10)


def test_other_entries_have_a_token_limit():
    assert kc.check_other_room([(1, 900)], 100) is None
    assert "1000 tokens" in kc.check_other_room([(1, 900)], 101)


def test_a_free_note_cannot_be_longer_than_the_whole_budget():
    with pytest.raises(Exception):
        knowledge.KnowledgeIn(title="x", content="a" * (kc.OTHER_CHARS_LIMIT + 1))


def test_the_old_whole_file_upload_is_gone():
    paths = {r.path for r in knowledge.router.routes}
    assert "/knowledge/upload" not in paths and "/upload" not in paths
    assert any(p.endswith("/read-file") for p in paths)


# ── what can be read ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,ctype,expected", [
    ("menu.pdf", None, "application/pdf"),
    ("RATE CARD.PDF", "application/octet-stream", "application/pdf"),
    ("photo.jpg", None, "image/jpeg"),
    ("photo.jpeg", None, "image/jpeg"),
    ("board.png", None, "image/png"),
    ("x.webp", None, "image/webp"),
    ("faq.txt", None, "text"),
    ("prices.csv", None, "text"),
    ("notes", "text/plain", "text"),
    ("scan", "application/pdf", "application/pdf"),
])
def test_file_types_that_are_read(name, ctype, expected):
    assert kr.media_type_for(name, ctype) == expected


@pytest.mark.parametrize("name", ["sheet.xlsx", "deck.pptx", "doc.docx", "a.zip"])
def test_other_file_types_are_refused_plainly(name):
    with pytest.raises(kr.UnreadableFile):
        kr.media_type_for(name, None)


# ── what comes back is checked before the owner sees it ──────────────────────

FIELDS = kc.fields_for("general")


def test_clean_keeps_answers_within_each_limit():
    limit = next(f["limit"] for f in FIELDS if f["key"] == "about")
    out, _ = kr.clean(FIELDS, {"about": "x" * (limit + 50), "when": "  ", "where": None}, 5)
    assert len(out["about"]) == limit
    assert "when" not in out and "where" not in out


def test_clean_drops_a_link_that_is_not_a_link():
    out, _ = kr.clean(FIELDS, {"full_list": "see our website"}, 5)
    assert "full_list" not in out
    out, _ = kr.clean(FIELDS, {"full_list": "https://example.com/menu.pdf"}, 5)
    assert out["full_list"] == "https://example.com/menu.pdf"


def test_clean_ignores_unknown_keys_and_caps_top_questions():
    faqs = [{"question": f"q{i}", "answer": "a"} for i in range(8)] + [{"question": "", "answer": "a"}, "junk"]
    out, kept = kr.clean(FIELDS, {"made_up_field": "x", "faqs": faqs}, 3)
    assert "made_up_field" not in out
    assert kept == [("q0", "a"), ("q1", "a"), ("q2", "a")]


def test_clean_with_no_room_for_questions():
    _, kept = kr.clean(FIELDS, {"faqs": [{"question": "q", "answer": "a"}]}, 0)
    assert kept == []


# ── one read, sent the right way ─────────────────────────────────────────────

def _fake_complete(answer, seen):
    async def complete(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(tool_input=answer, cost_paise=7)
    return complete


def _pdf(text: str | None) -> bytes:
    """A one-page PDF, with a text layer when `text` is given and none (like a scan) when it is not."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_a_pdf_is_read_here_and_sent_as_text_so_any_model_can_take_it(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(kr.client, "complete", _fake_complete({"about": "We fix phones.", "when": "", "faqs": []}, seen))
    raw = _pdf("Screen repair Rs 1500. Open Mon to Sat.")
    reading = asyncio.run(kr.read_into_card(raw=raw, filename="a.pdf", content_type=None, vertical="general", faq_room=10))
    block = seen["messages"][0]["content"][0]
    assert block["type"] == "text" and "Screen repair Rs 1500" in block["text"]
    assert seen["speed"] == "fast" and seen["task"] == "knowledge_read_file"
    assert reading.fields == {"about": "We fix phones."}


def test_a_scanned_pdf_asks_for_a_photo_without_an_ai_call(monkeypatch):
    async def boom(**kwargs):
        raise AssertionError("must not call the AI")
    monkeypatch.setattr(kr.client, "complete", boom)
    with pytest.raises(kr.UnreadableFile, match="photo"):
        asyncio.run(kr.read_into_card(raw=_pdf(None), filename="scan.pdf", content_type=None, vertical="general", faq_room=1))


def test_a_broken_pdf_is_refused_plainly():
    with pytest.raises(kr.UnreadableFile):
        kr.pdf_text(b"%PDF-1.4 not really a pdf")


def test_a_photo_is_sent_as_an_image(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(kr.client, "complete", _fake_complete({}, seen))
    asyncio.run(kr.read_into_card(raw=b"\x89PNG", filename="menu.png", content_type="image/png", vertical="general", faq_room=10))
    assert seen["messages"][0]["content"][0]["type"] == "image"


def test_a_long_text_file_is_cut_to_keep_one_read_cheap(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(kr.client, "complete", _fake_complete({}, seen))
    raw = ("line\n" * 20_000).encode()
    reading = asyncio.run(kr.read_into_card(raw=raw, filename="big.txt", content_type=None, vertical="general", faq_room=10))
    assert reading.truncated
    assert len(seen["messages"][0]["content"][0]["text"]) < kr.MAX_TEXT_CHARS + 50


def test_empty_and_huge_files_are_refused_before_any_ai_call(monkeypatch):
    async def boom(**kwargs):
        raise AssertionError("must not call the AI")
    monkeypatch.setattr(kr.client, "complete", boom)
    with pytest.raises(kr.UnreadableFile):
        asyncio.run(kr.read_into_card(raw=b"", filename="a.txt", content_type=None, vertical="general", faq_room=1))
    with pytest.raises(kr.UnreadableFile):
        asyncio.run(kr.read_into_card(raw=b"x" * (kr.MAX_FILE_BYTES + 1), filename="a.pdf", content_type=None, vertical="general", faq_room=1))
    with pytest.raises(kr.UnreadableFile):
        asyncio.run(kr.read_into_card(raw=b"   ", filename="a.txt", content_type=None, vertical="general", faq_room=1))


def test_the_tool_offers_exactly_the_card_fields():
    tool = kr._tool(FIELDS, 4)
    props = tool["input_schema"]["properties"]
    assert set(props) == {f["key"] for f in FIELDS} | {"faqs"}
    assert props["faqs"]["maxItems"] == 4


# ── a few reads a day ────────────────────────────────────────────────────────

def test_file_reads_are_limited_per_day():
    business = SimpleNamespace(settings={"other": 1})
    lefts = [knowledge._take_file_read(business) for _ in range(knowledge.FILE_READS_PER_DAY)]
    assert lefts == list(range(knowledge.FILE_READS_PER_DAY - 1, -1, -1))
    with pytest.raises(HTTPException) as exc:
        knowledge._take_file_read(business)
    assert exc.value.status_code == 429
    assert business.settings["other"] == 1


def test_the_count_starts_again_the_next_day():
    business = SimpleNamespace(settings={"knowledge_file_reads": {"day": "2000-01-01", "count": 99}})
    assert knowledge._take_file_read(business) == knowledge.FILE_READS_PER_DAY - 1
    assert business.settings["knowledge_file_reads"]["day"] == datetime.now(timezone.utc).date().isoformat()


# ── the routes: refused before the AI when the file is wrong ─────────────────

class _Upload:
    def __init__(self, name, data, ctype=None):
        self.filename, self._data, self.content_type = name, data, ctype

    async def read(self):
        return self._data


def test_read_file_route_refuses_a_spreadsheet_without_counting_a_read():
    business = SimpleNamespace(settings={}, vertical="general")

    class Db:
        async def get(self, *_):
            return business

    user = SimpleNamespace(business=uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(knowledge.read_file(current_user=user, db=Db(), file=_Upload("a.xlsx", b"x")))
    assert exc.value.status_code == 415
    assert business.settings == {}


# ── the card comes back in English (or the document's Hindi/Hinglish), never the model's own language ──────────

FRENCH = ("KROVA est une couche d'IA pour les entreprises qui gère les conversations sur WhatsApp, Instagram DM et "
          "appels téléphoniques. Elle répond dans le ton de l'entreprise et suit les engagements pris.")


def test_french_is_spotted():
    assert kr.looks_foreign({"about": FRENCH}, [])


@pytest.mark.parametrize("text", [
    "KROVA is an AI layer for businesses that handles conversations on WhatsApp, Instagram DM and phone calls.",
    "Hum Mon se Sat 10 baje se 7 baje tak khule hain. Sunday band rehta hai. Delivery 2 din mein ho jaati hai.",
    "Screen repair Rs 1500, battery Rs 900, home pickup available in Lucknow for orders above Rs 500.",
])
def test_english_and_hinglish_are_not_flagged(text):
    assert not kr.looks_foreign({"about": text}, [("Do you deliver?", "Yes, across the city.")])


def test_a_french_answer_is_asked_again_in_english(monkeypatch):
    answers = [{"about": FRENCH}, {"about": "KROVA is an AI layer for business conversations."}]
    asks: list[str] = []

    async def complete(**kwargs):
        asks.append(kwargs["messages"][0]["content"][1]["text"])
        return SimpleNamespace(tool_input=answers[len(asks) - 1], cost_paise=5)

    monkeypatch.setattr(kr.client, "complete", complete)
    reading = asyncio.run(kr.read_into_card(raw=b"KROVA notes", filename="a.txt", content_type=None, vertical="general", faq_room=0))
    assert reading.fields["about"].startswith("KROVA is")
    assert len(asks) == 2 and "English" in asks[1] and reading.cost_paise == 10


def test_french_twice_is_refused_not_shown(monkeypatch):
    async def complete(**kwargs):
        return SimpleNamespace(tool_input={"about": FRENCH}, cost_paise=5)

    monkeypatch.setattr(kr.client, "complete", complete)
    with pytest.raises(kr.UnreadableFile):
        asyncio.run(kr.read_into_card(raw=b"KROVA notes", filename="a.txt", content_type=None, vertical="general", faq_room=0))

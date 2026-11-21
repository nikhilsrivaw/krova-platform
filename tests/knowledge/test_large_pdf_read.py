"""A long PDF (a college prospectus) is read in parts and merged - only for a type whose template turns it on."""

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402

from shared.ai import knowledge_reader as kr  # noqa: E402
from shared.verticals import knowledge_lookup  # noqa: E402


def test_only_the_institution_type_reads_large_files():
    assert kr.reading_options("institution").get("large_files") is True
    assert "never move a fee or a date" in kr.reading_options("institution")["guide"]
    assert not kr.reading_options("general").get("large_files")
    assert kr.reading_options("no_such_type") == {}


def test_pages_are_cut_into_parts_between_pages_and_scans_are_skipped():
    pages = ["a" * 9000] * 10 + [""] + ["b" * 9000] * 5
    parts = kr.page_parts(pages, part_chars=30_000)
    assert [(p[0], p[1]) for p in parts] == [(1, 3), (4, 6), (7, 9), (10, 13), (14, 16)]
    assert "[Page 12]" in parts[3][2] and "[Page 11]" not in "".join(p[2] for p in parts)
    assert kr.page_parts(["", "  "]) == []


def test_merging_keeps_page_order_drops_repeats_and_says_when_more_than_fits():
    part1 = ({"about": "MMU, Gorakhpur"}, [("Hostel?", "Yes")], [{"name": "BCA", "price": "Rs 45,000", "details": ""}],
             [("BCA eligibility?", "12th pass")])
    part2 = ({"about": "later page", "where": "Gorakhpur"}, [("hostel?", "dup")],
             [{"name": "bca", "price": "x", "details": ""}, {"name": "MBA", "price": "Rs 1,20,000", "details": ""}],
             [("Admission form link?", "https://x.edu/apply"), ("Refund rule?", "Full refund before classes")])
    fields, faqs, rows, bank, more = kr.merge_readings([part1, part2], faq_room=1, bank_room=2)
    assert fields == {"about": "MMU, Gorakhpur", "where": "Gorakhpur"}
    assert faqs == [("Hostel?", "Yes")]
    assert [r["name"] for r in rows] == ["BCA", "MBA"]
    assert bank == [("BCA eligibility?", "12th pass"), ("Admission form link?", "https://x.edu/apply")]
    assert more is True


def test_catalogue_is_capped_at_its_limit():
    rows = [{"name": f"Course {i}", "price": "", "details": ""} for i in range(knowledge_lookup.CATALOGUE_ROWS_LIMIT + 5)]
    _, _, kept, _, more = kr.merge_readings([({}, [], rows, [])], faq_room=0, bank_room=0)
    assert len(kept) == knowledge_lookup.CATALOGUE_ROWS_LIMIT and more


def _fake_pdf(monkeypatch, pages):
    monkeypatch.setattr(kr, "pdf_pages", lambda raw, max_pages=kr.MAX_LARGE_PAGES: pages)


def test_a_150_page_prospectus_is_read_in_parts_with_the_type_guide(monkeypatch):
    _fake_pdf(monkeypatch, [f"Page text {i} " * 300 for i in range(150)] + [""] * 2)
    calls = []

    async def complete(**kw):
        calls.append(kw)
        n = len(calls)
        return SimpleNamespace(cost_paise=200, tool_input={
            "faqs": [], "catalogue": [{"name": f"Course {n}", "price": "Rs 10", "details": ""}],
            "more_questions": [{"question": f"Q{n}?", "answer": "A"}],
        })

    monkeypatch.setattr(kr.client, "complete", complete)
    reading = asyncio.run(kr.read_large_pdf(raw=b"%PDF", filename="p.pdf", vertical="institution", faq_room=10, bank_room=200))
    assert len(calls) > 1 and reading.cost_paise == 200 * len(calls)
    assert "This kind of document" in calls[0]["system"] and "pages 1 to" in calls[0]["messages"][0]["content"][1]["text"]
    assert calls[0]["tool"]["input_schema"]["properties"]["more_questions"]["maxItems"] == kr.PART_ANSWERS
    assert len(reading.catalogue) == len(calls) and len(reading.bank) == len(calls)
    assert reading.pages_read == 152 and reading.pages_without_text == 2 and not reading.truncated


def test_one_failed_part_keeps_the_rest_and_says_so(monkeypatch):
    _fake_pdf(monkeypatch, ["x" * 29_000] * 3)
    seen = []

    async def complete(**kw):
        seen.append(1)
        if len(seen) == 2:
            raise RuntimeError("model down")
        return SimpleNamespace(cost_paise=1, tool_input={"faqs": [], "catalogue": [], "more_questions": [
            {"question": f"Q{len(seen)}?", "answer": "A"}]})

    monkeypatch.setattr(kr.client, "complete", complete)
    monkeypatch.setattr(kr, "PARTS_AT_ONCE", 1)
    reading = asyncio.run(kr.read_large_pdf(raw=b"%PDF", filename="p.pdf", vertical="institution", faq_room=0, bank_room=10))
    assert reading.truncated and len(reading.bank) == 2


def test_a_scan_or_an_oversized_file_is_refused(monkeypatch):
    _fake_pdf(monkeypatch, ["", ""])
    with pytest.raises(kr.UnreadableFile, match="scan"):
        asyncio.run(kr.read_large_pdf(raw=b"%PDF", filename="p.pdf", vertical="institution", faq_room=0, bank_room=0))
    with pytest.raises(kr.UnreadableFile, match="25 MB"):
        asyncio.run(kr.read_large_pdf(raw=b"x" * (kr.MAX_LARGE_FILE_BYTES + 1), filename="p.pdf", vertical="institution",
                                      faq_room=0, bank_room=0))

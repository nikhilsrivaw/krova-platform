"""The pick model's own mistakes on a long list, caught before they reach a reply. Each raw answer below is one the live
model really gave on a 203-item prospectus list (scripts/pick_raw_test.py)."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.ai import knowledge_pick as kp  # noqa: E402

NAMES = ["Arts (B.A.)", "Commerce (B.Com.)", "Science (B.Sc.)", "Bachelor of Computer Application (BCA)",
         "Business Administration (BBA)", "Library and Information Science (BLIS)"]
NAMES += [f"Course {n}" for n in range(7, 34)]
NAMES += ["Business Administration (MBA)", "B.Ed. (Special Education) (ODL)", "Hindi (M.A.)"]
BANK = [{"question": "What is the eligibility for MBA?", "answer": "Graduation"},
        {"question": "Is Aadhaar mandatory for admission?", "answer": "Yes"}]
MENU = kp._menu([{"name": n, "price": "1000/-"} for n in NAMES], BANK, [])


def _names(found):
    return [r["name"] for r in found["rows"]] + [a["question"] for a in found["answers"]]


def test_the_written_name_wins_over_a_wrong_number():
    found = kp._read("LANGUAGE: Hindi\n\n1: Bachelor of Computer Application (BCA)", MENU, "BCA ki fees kitni hai?")
    assert _names(found) == ["Bachelor of Computer Application (BCA)"]
    found = kp._read("LANGUAGE: Hindi\n4: Bachelor of Computer Application (BCA) / 6: Li", MENU, "")
    assert _names(found) == ["Bachelor of Computer Application (BCA)", "Library and Information Science (BLIS)"]


def test_a_name_that_matches_nothing_is_not_trusted():
    assert kp._read("LANGUAGE: English\n3: Gorakhpur study centre", MENU, "study centre kaha hai") is None


def test_counting_the_list_from_one_is_not_a_pick():
    raw = "LANGUAGE: Hindi\n\n1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15"
    assert kp._read(raw, MENU, "admission ki last date kya hai") is None
    assert kp._read("LANGUAGE: Hindi\n\n24, " + raw.split("\n\n")[1], MENU, "last date") is None
    # A real run elsewhere in the list (the diploma courses) still counts.
    assert len(_names(kp._read("LANGUAGE: Hindi\n\n7, 8, 9, 10, 11", MENU, "diploma courses kaun se hai"))) == 5


def test_a_short_name_they_typed_must_be_among_the_picks():
    assert kp._read("LANGUAGE: English\n\n1", MENU, "MBA admission fee?") is None  # B.A. for an MBA question
    assert kp._read("LANGUAGE: English\n\n4", MENU, "MBA admission fee?") is None  # BCA for it
    assert _names(kp._read("LANGUAGE: English\n\n34", MENU, "MBA admission fee?")) == ["Business Administration (MBA)"]
    assert _names(kp._read("LANGUAGE: English\n\n37", MENU, "eligibility for MBA")) == ["What is the eligibility for MBA?"]
    assert _names(kp._read("LANGUAGE: Hindi\n\n35", MENU, "B.Ed ki fees")) == ["B.Ed. (Special Education) (ODL)"]
    assert _names(kp._read("LANGUAGE: Hindi\n\n36", MENU, "M.A. Hindi kitne saal ka hai")) == ["Hindi (M.A.)"]


def test_words_that_are_not_short_names_do_not_block_a_pick():
    # "Special" and "Education" sit in brackets but are ordinary words, not a course's short name.
    assert _names(kp._read("LANGUAGE: English\n\n38", MENU, "special documents for admission?")) == [
        "Is Aadhaar mandatory for admission?"
    ]
    assert _names(kp._read("LANGUAGE: Hindi\n\n4", MENU, "iski fees kitni hai")) == [
        "Bachelor of Computer Application (BCA)"
    ]


def test_the_form_label_before_missing_is_read():
    found = kp._read("LANGUAGE: Hindi\nNumbers: MISSING: Gorakhpur study centre address", MENU, "")
    assert found["missing"] == "Gorakhpur study centre address"


def test_a_run_counted_from_anywhere_is_not_a_pick():
    assert kp._read("LANGUAGE: Hindi\n\n5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16", MENU, "documents?") is None
    assert kp._read("LANGUAGE: Hindi\n\n7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,", MENU, "study centre") is None
    assert len(_names(kp._read("LANGUAGE: Hindi\n\n7, 8, 9, 10, 11, 12, 13, 20, 21", MENU, "diploma"))) == 5


def test_names_copied_with_the_list_line_or_cut_short_still_match():
    raw = "LANGUAGE: Hindi\n4: Bachelor of Computer Application (BCA) (price on file), 37: What is the eligibility fo"
    assert _names(kp._read(raw, MENU, "")) == ["Bachelor of Computer Application (BCA)", "What is the eligibility for MBA?"]


def test_the_answer_form_asks_for_names():
    assert "its number, a colon and its name" in kp.ASK

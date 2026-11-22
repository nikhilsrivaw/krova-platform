"""A prospectus typed in Kruti Dev with fee tables: Hindi comes back as Hindi, tables keep their column names, and every
course's fee is checked against the document's own table rows. Samples are from a real university information bulletin."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402

from shared.ai import knowledge_reader as kr  # noqa: E402
from shared.ai import krutidev, pdf_structure  # noqa: E402


@pytest.mark.parametrize("keys, hindi", [
    ("Áos'k", "प्रवेश"), ("egÙoiw.kZ", "महत्त्वपूर्ण"), ("funsZ'k", "निर्देश"), ("dk;ZØe", "कार्यक्रम"),
    ("f'k{kkfFkZ;ksa", "शिक्षार्थियों"), ("fo'ofo|ky;", "विश्वविद्यालय"), ("Li’V", "स्पष्ट"), ("mŸkjnk;h", "उत्तरदायी"),
    ("vkosnu i= dks", "आवेदन पत्र को"), ("fdlh Hkh =qfV", "किसी भी त्रुटि"), ("U;wure vgZrk", "न्यूनतम अर्हता"),
    ("okf.kT; ¼ch-dkWe-½", "वाणिज्य (बी.कॉम.)"), ("4000@&", "4000/-"), ("13@04@2022", "13/04/2022"),
])
def test_kruti_dev_keys_become_hindi(keys, hindi):
    assert krutidev.convert(keys) == hindi


def test_english_typed_in_the_same_font_is_left_alone():
    words = ["Bachelor", "of", "Computer", "Application", "(BCA)"]
    assert krutidev.convert_line(words) == words
    assert krutidev.convert_line(["E"]) == ["E"]  # the medium column: English
    assert krutidev.convert_line(["dEI;wVj", "vuqç;ksx"]) == ["कम्प्यूटर", "अनुप्रयोग"]


def test_a_kruti_dev_font_is_told_from_english():
    hindi = "lHkh vkosndksa dks pkfg, fd vkWu ykbu Qkez dks /;ku ls Hkjsa rFkk fdlh Hkh =qfV ds fy, f'k{kkFkhZ Lo;a gS " * 3
    assert krutidev.looks_like_krutidev(hindi)
    assert not krutidev.looks_like_krutidev("All applicants must fill the online form with care and check it. " * 5)


FEE_TABLE = [
    ["Prog. Code", "Name of the programme", "Duration (years)", None, "Admission Fee", "Minimum Qualification"],
    [None, None, "Min.", "Max.", None, None],
    ["Graduation", None, None, None, None, None],
    ["101", "Arts (B.A.)", "3", "6", "4000/-", "10+2"],
    ["105", "Bachelor of Computer Application (BCA)", "3", "6", "13000/-", "10+2 or diploma"],
]


def test_a_fee_table_row_carries_its_column_names():
    lines, names = pdf_structure._render_table(FEE_TABLE, None)
    assert names[2] == "Duration (years) Min." and names[3] == "Duration (years) Max."
    assert lines[0] == "-- Graduation --"
    assert lines[2] == (
        "Prog. Code: 105 | Name of the programme: Bachelor of Computer Application (BCA) | Duration (years) Min.: 3 | "
        "Duration (years) Max.: 6 | Admission Fee: 13000/- | Minimum Qualification: 10+2 or diploma"
    )


def test_a_table_running_onto_the_next_page_keeps_its_header():
    _, names = pdf_structure._render_table(FEE_TABLE, None)
    rest = [["226", "Hindi", "2", "4", "8000/-", "Graduation"]]
    lines, _ = pdf_structure._render_table(rest, names)
    assert lines == ["Prog. Code: 226 | Name of the programme: Hindi | Duration (years) Min.: 2 | Duration (years) Max.: 4 | "
                     "Admission Fee: 8000/- | Minimum Qualification: Graduation"]
    # A part-row running over from the last page is data too, not a new header.
    over = [[None, "Statistics", None, None, None, "Maths in 10+2"], ["190", "Mathematics", "2", "4", "8000/-", "BSc"]]
    assert pdf_structure._render_table(over, names)[0][0].startswith("Name of the programme: Statistics")
    # A new table with its own header is not mistaken for the rest of the last one.
    other = [["Semester", "Paper No.", "Course", "New Code", "Credit", "Note"], ["1", "25093", "English", "SECEC-01", "4", "-"]]
    assert pdf_structure._render_table(other, names)[0] == [
        "Semester: 1 | Paper No.: 25093 | Course: English | New Code: SECEC-01 | Credit: 4 | Note: -"
    ]


ROWS = [
    (10, "Prog. Code: 101 | Name of the programme: Arts (B.A.) | Admission Fee: 4000/-"),
    (10, "Prog. Code: 105 | Name of the programme: Bachelor of Computer Application (BCA) | Admission Fee: 13000/-"),
    (12, "Prog. Code: 702 | Name of the programme: Business Administration (MBA) | Admission Fee: 20,000/-"),
]


def test_each_fee_is_matched_to_its_own_course_row():
    rows = [
        {"name": "BCA", "price": "Rs 13,000 per year", "details": ""},
        {"name": "BCA", "price": "Rs 4,000 per year", "details": ""},  # the B.A. fee on the BCA - a model's slip
        {"name": "MBA", "price": "20000/-", "details": ""},
        {"name": "Arts (B.A.)", "price": "4000/-", "details": ""},
        {"name": "PhD Physics", "price": "Rs 50,000", "details": ""},
        {"name": "BCA", "price": "On request", "details": ""},
    ]
    assert kr.check_catalogue(rows, ROWS, "") == [
        ("ok", 10), ("look", 10), ("ok", 12), ("ok", 10), ("look", None), ("look", 10),
    ]


def test_answers_with_numbers_not_in_the_document_are_marked():
    whole = "Challenge evaluation fee Rs 300/- per paper within 30 days. Last date 15 September."
    pairs = [("Fee?", "Rs 300 per paper, apply within 30 days"), ("Last date?", "31 March"), ("Hostel?", "Yes, on campus")]
    assert kr.check_answers(pairs, whole) == ["ok", "look", "ok"]

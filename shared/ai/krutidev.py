"""
Kruti Dev text back into Hindi.

Many Indian government and university documents are typed in Kruti Dev 010, a font that draws Hindi letters over plain
English keys. The PDF keeps the keys, not the letters, so its text reads "Áos'k" where the page shows "प्रवेश", and a fee
of "4000/-" comes out as "4000@&". Neither a model nor a person can read that. This turns the keys back into Unicode
Hindi, so a prospectus typed in Kruti Dev can be read like any other.

Only text in a Kruti Dev font is converted (looks_like_krutidev decides per font), never English typed beside it.
"""

import re

# Longest keys first: "vks" before "vk" before "v". Keys are the Kruti Dev 010 layout as typed documents use it (= is a
# full त्र: "i=" पत्र); a few (’ for ष्, Ÿ for त्त्) are how Word's autocorrect and the font's glyph names come out of a PDF.
_PAIRS: list[tuple[str, str]] = [
    ("ñ", "॰"), ("Q+Z", "QZ+"), ("sas", "sa"), ("aa", "a"), (")Z", "र्द्ध"), ("ZZ", "Z"),
    ("å", "०"), ("ƒ", "१"), ("„", "२"), ("…", "३"), ("†", "४"), ("‡", "५"), ("ˆ", "६"), ("‰", "७"), ("Š", "८"), ("‹", "९"),
    ("¶+", "फ़्"), ("d+", "क़"), ("[+k", "ख़"), ("[+", "ख़्"), ("x+", "ग़"), ("T+", "ज़्"), ("t+", "ज़"), ("M+", "ड़"),
    ("<+", "ढ़"), ("Q+", "फ़"), (";+", "य़"), ("j+", "ऱ"), ("u+", "ऩ"),
    ("Ùk", "त्त"), ("Ù", "त्त्"), ("Ÿk", "त्त"), ("Ÿ", "त्त्"), ("ä", "क्त"), ("–", "दृ"), ("—", "कृ"), ("é", "न्न"), ("™", "न्न्"),
    ("à", "ह्न"), ("á", "ह्य"), ("â", "हृ"), ("ã", "ह्म"), ("ºz", "ह्र"), ("º", "ह्"), ("í", "द्द"),
    ("{k", "क्ष"), ("{", "क्ष्"), ("=", "त्र"), ("«", "त्र्"),
    ("Nî", "छ्य"), ("Vî", "ट्य"), ("Bî", "ठ्य"), ("Mî", "ड्य"), ("<î", "ढ्य"), ("|", "द्य"), ("K", "ज्ञ"), ("}", "द्व"),
    ("J", "श्र"), ("Vª", "ट्र"), ("Mª", "ड्र"), ("<ªª", "ढ्र"), ("Nª", "छ्र"), ("Ø", "क्र"), ("Ý", "फ्र"), ("nzZ", "र्द्र"),
    ("æ", "द्र"), ("ç", "प्र"), ("Á", "प्र"), ("xz", "ग्र"), ("#", "रु"), (":", "रू"),
    ("v‚", "ऑ"), ("vks", "ओ"), ("vkS", "औ"), ("vk", "आ"), ("v", "अ"), ("b±", "ईं"), ("Ã", "ई"), ("bZ", "ई"), ("b", "इ"),
    ("m", "उ"), ("Å", "ऊ"), (",s", "ऐ"), (",", "ए"), ("_", "ऋ"),
    ("ô", "क्क"), ("d", "क"), ("Dk", "क"), ("D", "क्"), ("[k", "ख"), ("[", "ख्"), ("x", "ग"), ("Xk", "ग"), ("X", "ग्"),
    ("Ä", "घ"), ("?k", "घ"), ("?", "घ्"), ("³", "ङ"),
    ("pkS", "चै"), ("p", "च"), ("Pk", "च"), ("P", "च्"), ("N", "छ"), ("t", "ज"), ("Tk", "ज"), ("T", "ज्"), (">", "झ"),
    ("÷", "झ्"), ("¥", "ञ"),
    ("ê", "ट्ट"), ("ë", "ट्ठ"), ("V", "ट"), ("B", "ठ"), ("ì", "ड्ड"), ("ï", "ड्ढ"), ("M", "ड"), ("<", "ढ"), (".k", "ण"), (".", "ण्"),
    ("r", "त"), ("Rk", "त"), ("R", "त्"), ("Fk", "थ"), ("F", "थ्"), (")", "द्ध"), ("n", "द"), ("/k", "ध"), ("èk", "ध"),
    ("/", "ध्"), ("è", "ध्"), ("u", "न"), ("Uk", "न"), ("U", "न्"),
    ("i", "प"), ("Ik", "प"), ("I", "प्"), ("Q", "फ"), ("¶", "फ्"), ("c", "ब"), ("Ck", "ब"), ("C", "ब्"), ("Hk", "भ"), ("H", "भ्"),
    ("e", "म"), ("Ek", "म"), ("E", "म्"), (";", "य"), ("¸", "य्"), ("j", "र"), ("y", "ल"), ("Yk", "ल"), ("Y", "ल्"), ("G", "ळ"),
    ("o", "व"), ("Ok", "व"), ("O", "व्"),
    ("'k", "श"), ("'", "श्"), ("\"k", "ष"), ("\"", "ष्"), ("’k", "ष"), ("’", "ष्"), ("l", "स"), ("Lk", "स"), ("L", "स्"), ("g", "ह"),
    ("È", "ीं"), ("z", "्र"), ("ª", "्र"),
    ("Ì", "द्द"), ("Í", "ट्ट"), ("Î", "ट्ठ"), ("Ï", "ड्ड"), ("Ñ", "कृ"), ("Ò", "भ"), ("Ó", "्य"), ("Ô", "ड्ढ"), ("Ö", "झ्"),
    ("Ú", "क्"), ("Ü", "श्"),
    ("‚", "ॉ"), ("kW", "ॉ"), ("ks", "ो"), ("kS", "ौ"), ("k", "ा"), ("h", "ी"), ("q", "ु"), ("w", "ू"), ("`", "ृ"), ("s", "े"), ("S", "ै"),
    ("a", "ं"), ("¡", "ँ"), ("%", "ः"), ("W", "ॅ"), ("•", "ऽ"), ("·", "ऽ"), ("~", "्"), ("+", "़"), ("A", "।"),
    ("@", "/"), ("&", "-"), ("-", "."), ("]", ","), ("¼", "("), ("½", ")"), ("“", "\""), ("”", "\""), ("‘", "'"), ("¨", "ो"),
    ("f", "ि"), ("Z", "र्"),
]
# One pass, longest key first at every position.
_KEYS = sorted({k for k, _ in _PAIRS}, key=len, reverse=True)
_MAP = dict(_PAIRS)
_SCAN = re.compile("|".join(re.escape(k) for k in _KEYS) + r"|.", re.S)

_CONSONANT = "[क-हक़-य़]"
_MATRA = "[ा-ौँ-ः]"
# ि is typed before its consonant cluster: move it after the cluster (half consonants + the full one, with any nukta).
_I_SIGN = re.compile(rf"ि((?:{_CONSONANT}़?्)*{_CONSONANT}़?)")
# र् (reph, typed Z) is typed after the syllable it sits on: move it before that syllable's consonant cluster.
_REPH = re.compile(rf"((?:{_CONSONANT}़?्)*{_CONSONANT}़?{_MATRA}*)र्")


def convert(text: str) -> str:
    """Kruti Dev keys to Unicode Hindi. Digits, spaces and line breaks are kept as they are."""
    out = "".join(_MAP.get(m.group(0), m.group(0)) for m in _SCAN.finditer(text))
    out = out.replace("्ा", "")  # a half letter followed by the aa sign is the full letter
    out = _I_SIGN.sub(lambda m: m.group(1) + "ि", out)
    out = _REPH.sub(lambda m: "र्" + m.group(1), out)
    out = out.replace("ाे", "ो").replace("ाै", "ौ").replace("अा", "आ").replace("एे", "ऐ")
    return out


# Words that are everywhere in Hindi prose, as Kruti Dev keys: के, का, की, में, है, से, को, और, तथा, एवं, कि, पर, नहीं, लिए.
_COMMON = {"ds", "dk", "dh", "esa", "gS", "gSA", "ls", "dks", "vkSj", "rFkk", ",oa", "fd", "ij", "ugha", "fy,", "gksxk", "gksxh"}


def looks_like_krutidev(text: str) -> bool:
    """True when enough words are common Hindi words in Kruti Dev keys - which English never contains."""
    words = text.split()
    if len(words) < 20:
        return False
    return sum(1 for w in words if w in _COMMON) / len(words) >= 0.03


# ── English typed in the same font ────────────────────────────────────────────────────────────────────────────────
#
# A Kruti Dev document often types English in the same font ("Bachelor of Computer Application (BCA)"), and converting
# that gives nonsense. So each word is judged: Kruti Dev keys show themselves by their punctuation keys (; ' { / ¼ ...)
# or a capital in mid-word (rFkk, lHkh); English by real parentheses, capitals-only words (BCA, and a lone E or H for
# the medium - a half letter never stands alone; "A" alone is the danda), or common words of course listings. A word with neither follows the rest of its line.

_KRUTI_KEYS = re.compile(r"[;'\"{}\[\]|/`~¼½ÁØç’¡ÙŸ@&=]|[a-z][A-Z]|^[A-Z][A-Z]*[a-z]+[A-Z]")
_ENGLISH_HINTS = {
    "of", "the", "and", "or", "in", "for", "to", "after", "any", "with", "by", "on", "from", "is", "are", "be", "at",
    "an", "a", "as", "all", "not", "per", "year", "years", "month", "months", "programme", "program", "course",
    "courses", "fee", "fees", "application", "applications", "education", "board", "standard", "equivalent", "diploma",
    "graduation", "graduate", "bachelor", "master", "masters", "science", "computer", "arts", "commerce", "management",
    "business", "administration", "adminstration", "library", "information", "technical", "trade", "two", "three",
    "one", "four", "degree", "certificate", "post", "minimum", "maximum", "qualification", "admission", "duration",
    "name", "code", "online", "offline", "student", "students", "learner", "learners", "university", "examination",
    "exam", "refund", "policy", "category", "percentage", "time", "period", "date", "last", "other", "only", "social",
    "work", "journalism", "mass", "communication", "english", "hindi", "history", "sociology", "political",
    "economics", "geography", "mathematics", "physics", "chemistry", "botany", "zoology", "law", "health", "yoga",
}


def _kruti_word(word: str) -> bool | None:
    """True for Kruti Dev keys, False for English, None when the word itself does not say."""
    if _KRUTI_KEYS.search(word) or word in _COMMON:
        return True
    bare = word.strip(".,:-+()").lower()
    if "(" in word or ")" in word or bare in _ENGLISH_HINTS or (word.isalpha() and word.isupper() and word != "A"):
        return False
    return None


def convert_line(words: list[str]) -> list[str]:
    """Words of one line typed in a Kruti Dev font: Hindi converted, English left as it is."""
    votes = [_kruti_word(w) for w in words]
    english_line = votes.count(False) > votes.count(True)
    return [
        w if vote is False or (vote is None and english_line) else convert(w)
        for w, vote in zip(words, votes)
    ]

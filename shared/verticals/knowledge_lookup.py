"""
The "opened when needed" knowledge: a Catalogue and a Question bank.

The Business Card is read before every reply, so it has to stay small. These two can be large (hundreds of items, dozens
of answers) because the AI never reads them whole: before a reply, the customer's latest words are matched against
them and only the few rows and answers that fit are shown, next to the conversation. A message that matches nothing
costs nothing extra. Matching is plain word overlap - no extra AI call, so it adds no cost and no wait on a phone call.

The same for every business type: a row is a name, a price and a short detail, whatever is being sold.

Storage: the Catalogue is one KnowledgeItem with topic "catalogue" and its rows in extra["catalogue"]; each Question
bank answer is a KnowledgeItem with extra={"card": "bank"}. Both carry token_estimate 0, because nothing about them is
read on every message, so they do not use up the plan's knowledge budget.
"""

import re
from typing import Any

CATALOGUE_TOPIC = "catalogue"
CATALOGUE_ROWS_LIMIT = 300
CATALOGUE_LIMITS = {"name": 80, "price": 40, "details": 150}

BANK = "bank"
BANK_LIMIT = 50
BANK_QUESTION_LIMIT = 150
BANK_ANSWER_LIMIT = 400

# How much one reply may be shown: enough to answer, small enough to stay cheap (about 450 tokens at most).
MATCH_ROWS = 5
MATCH_ANSWERS = 3

# Words that say nothing about WHICH item or question is meant (English and Hinglish).
_STOP = {
    "a", "an", "the", "is", "are", "am", "was", "be", "do", "does", "did", "i", "me", "my", "you", "your", "we", "our",
    "it", "its", "this", "that", "these", "those", "to", "of", "in", "on", "at", "for", "from", "with", "and", "or",
    "but", "if", "can", "could", "will", "would", "should", "have", "has", "had", "what", "which", "who", "how", "when",
    "where", "why", "please", "pls", "plz", "hi", "hello", "hey", "ok", "okay", "yes", "no", "not", "any", "some",
    "about", "there", "here", "get", "want", "need", "tell", "know", "much", "many", "also", "too", "so", "just",
    "price", "prices", "rate", "rates", "cost", "costs", "charge", "charges", "rs", "inr", "rupees", "rupee",
    "kya", "hai", "hain", "ho", "hoga", "hogi", "ka", "ki", "ke", "ko", "se", "me", "mein", "main", "mai", "par", "pe",
    "aur", "ya", "bhi", "to", "toh", "na", "nahi", "nhi", "haan", "ha", "ji", "sir", "madam", "bhai", "aap", "aapka",
    "aapki", "aapke", "apna", "apni", "kitna", "kitne", "kitni", "kaise", "kab", "kahan", "kaha", "kaun", "kon", "koi",
    "kuch", "wala", "wali", "wale", "chahiye", "batao", "bataiye", "bata", "dijiye", "do", "de", "sakte", "sakta",
    "milega", "milegi", "milta", "milti", "raha", "rahi", "rahe", "tha", "thi", "the", "hu", "hoon", "ek",
}


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("es"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def words(text: str) -> set[str]:
    """The meaningful words of a text, lower-cased and roughly singular."""
    return {_stem(w) for w in re.findall(r"\w+", (text or "").lower()) if w not in _STOP and (len(w) > 1 or w.isdigit())}


def _hit(word: str, target: set[str]) -> bool:
    # whole word, or a shared start of 4+ letters ("shirt" / "shirting", "iphone" / "iphone13")
    if word in target:
        return True
    if len(word) < 4:
        return False
    return any(len(t) >= 4 and (t.startswith(word) or word.startswith(t)) for t in target)


def match_catalogue(rows: list[dict[str, Any]], text: str, limit: int = MATCH_ROWS) -> list[dict[str, Any]]:
    """The rows the customer's words point at: a name match counts double a detail match. Nothing matched, nothing back."""
    asked = words(text)
    if not asked or not rows:
        return []
    scored = []
    for i, row in enumerate(rows):
        name, details = words(row.get("name", "")), words(row.get("details", ""))
        name_hits = sum(1 for w in asked if _hit(w, name))
        if not name_hits:
            continue  # a row is only shown when its own name was asked about
        detail_hits = sum(1 for w in asked if _hit(w, details))
        scored.append((name_hits * 2 + detail_hits, -i, row))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [row for _, _, row in scored[:limit]]


def match_bank(pairs: list[dict[str, Any]], text: str, limit: int = MATCH_ANSWERS) -> list[dict[str, Any]]:
    """
    The saved answers whose question shares enough meaningful words with what was asked: two or more, or all of a
    short question's words ("emi?" matches "Do you offer EMI?").
    """
    asked = words(text)
    if not asked or not pairs:
        return []
    scored = []
    for i, pair in enumerate(pairs):
        q = words(pair.get("question", ""))
        if not q:
            continue
        hits = sum(1 for w in q if _hit(w, asked))
        if hits >= 2 or (hits >= 1 and hits == len(q)) or (hits >= 1 and len(q) <= 2):
            scored.append((hits / len(q), hits, -i, pair))
    scored.sort(key=lambda s: (s[0], s[1], s[2]), reverse=True)
    return [pair for _, _, _, pair in scored[:limit]]


def row_line(row: dict[str, Any]) -> str:
    line = row.get("name", "").strip()
    if row.get("price"):
        line += f" - {row['price'].strip()}"
    if row.get("details"):
        line += f" ({row['details'].strip()})"
    return line


def clean_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Rows as stored: trimmed to their limits, empty names dropped, at most CATALOGUE_ROWS_LIMIT."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        clean = {k: str(row.get(k) or "").strip()[:limit] for k, limit in CATALOGUE_LIMITS.items()}
        if clean["name"]:
            out.append(clean)
    return out[:CATALOGUE_ROWS_LIMIT]

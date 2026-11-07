"""
Which language a customer wrote in - for routing rules ("English speakers to the English team").

Free and instant, no AI call:
  - an Indian script is read from the characters themselves, which is exact. Devanagari is reported as "hindi" (it is
    also Marathi's script; the label on screen says so);
  - Latin letters are English or Hinglish. That is a guess from common Hindi words typed in English letters ("hai",
    "kya", "nahi"...): good on a real sentence, unsure on a two-word message, which reads as English.
A voice call's language is already known from speech (Customer.preferred_language, "hi-IN"), and is mapped here too.
"""

import re

LANGUAGES = (
    "english", "hinglish", "hindi", "tamil", "telugu", "bengali", "gujarati", "kannada", "malayalam", "punjabi", "odia",
)

# Unicode blocks of the Indian scripts.
_SCRIPTS = (
    ("hindi", 0x0900, 0x097F), ("bengali", 0x0980, 0x09FF), ("punjabi", 0x0A00, 0x0A7F), ("gujarati", 0x0A80, 0x0AFF),
    ("odia", 0x0B00, 0x0B7F), ("tamil", 0x0B80, 0x0BFF), ("telugu", 0x0C00, 0x0C7F), ("kannada", 0x0C80, 0x0CFF),
    ("malayalam", 0x0D00, 0x0D7F),
)

# Everyday Hindi words as people type them in English letters. Words that are also English ("main", "to", "me") are
# left out so an English sentence is not mistaken for Hinglish.
_HINGLISH = {
    "hai", "hain", "kya", "nahi", "nahin", "nhi", "mujhe", "mera", "meri", "mere", "aap", "aapka", "aapki", "apna",
    "kab", "kaise", "kitna", "kitne", "kitni", "chahiye", "karo", "karna", "kar", "kardo", "krdo", "bhai", "bhaiya",
    "ji", "haan", "ha", "accha", "acha", "theek", "thik", "bhej", "bhejo", "dijiye", "kijiye", "hoga", "hogi", "tha",
    "thi", "abhi", "kal", "aaj", "yeh", "ye", "woh", "wo", "kyun", "kyu", "matlab", "bata", "batao", "batana", "lekin",
    "aur", "bhi", "sirf", "wala", "wali", "wale", "raha", "rahi", "rahe", "milega", "milegi", "chalega", "dena", "lena",
    "paisa", "paise", "jaldi", "zyada", "kam", "sab", "kuch", "koi", "kaun", "kahan", "yahan", "wahan", "ek", "do",
}

# What shared/ai/knowledge_pick.py may name from a message's words. detect() can only see letters, so to it "MBA fees
# evlo" is English; a name from this list says it is Tamil.
SPOKEN = frozenset(LANGUAGES) | {
    "marathi", "urdu", "assamese", "nepali", "konkani", "maithili", "sanskrit", "sindhi", "kashmiri", "dogri", "bodo",
    "manipuri", "santali",
}
# Written in a script that detect() reports under another language's name.
_SHARES_SCRIPT = {"hindi": {"marathi", "nepali", "konkani", "maithili", "sanskrit", "dogri", "bodo"}, "bengali": {"assamese", "manipuri"}}


def spoken(text: str | None, named: str | None) -> str | None:
    """
    The language of a message once a name from its words is known. An Indian script stays exact, so a name can only
    refine it (Devanagari to Marathi); English letters say nothing, so there the name is trusted.
    """
    written = detect(text)
    if not named or named == written:
        return written
    if written in (None, "english", "hinglish"):
        return "hinglish" if named == "hindi" and written else named
    return named if named in _SHARES_SCRIPT.get(written, ()) else written


_VOICE = {
    "en": "english", "hi": "hindi", "ta": "tamil", "te": "telugu", "bn": "bengali", "gu": "gujarati", "kn": "kannada",
    "ml": "malayalam", "pa": "punjabi", "od": "odia", "or": "odia", "mr": "hindi",
    # Heard on a call but spoken back in the Hindi or Bengali voice (sarvam.speakable), so their lines are too.
    "ur": "hindi", "ne": "hindi", "kok": "hindi", "mai": "hindi", "doi": "hindi", "sa": "hindi", "brx": "hindi",
    "ks": "hindi", "sd": "hindi", "sat": "hindi", "as": "bengali", "mni": "bengali",
}


def detect(text: str | None) -> str | None:
    """The language of one message, or None when there is nothing to judge (no letters)."""
    if not text:
        return None
    counts: dict[str, int] = {}
    latin = 0
    for ch in text:
        code = ord(ch)
        if ("a" <= ch.lower() <= "z"):
            latin += 1
            continue
        for name, low, high in _SCRIPTS:
            if low <= code <= high:
                counts[name] = counts.get(name, 0) + 1
                break
    if counts:
        best = max(counts, key=counts.get)
        if counts[best] >= latin:
            return best
    if latin == 0:
        return None
    words = re.findall(r"[a-z]+", text.lower())
    hindi_words = sum(1 for w in words if w in _HINGLISH)
    if hindi_words >= 2 or (hindi_words == 1 and len(words) <= 4):
        return "hinglish"
    return "english"


def from_voice(code: str | None) -> str | None:
    """'hi-IN' -> 'hindi'. None when unknown."""
    if not code:
        return None
    return _VOICE.get(code.split("-")[0].lower())

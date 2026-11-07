"""
Which language and script a reply must be in - decided in code from the customer's own last message, so it never
rests on the model reading the room. A model shown an earlier Hinglish chat will happily answer an English
"what services do you offer?" in Devanagari; one explicit line about THIS message stops that.

`choice` is the business's own rule (Website chat page "Replies in"): "auto" - the visitor's language and script
(default, and the only rule on WhatsApp, Instagram and calls); "en" - always English; "hi" - always Hindi in Devanagari.
"""

from shared.team import language as lang

CHOICES = ("auto", "en", "hi")

_DEVANAGARI = (0x0900, 0x097F)
# Every Indian script block (Devanagari to Malayalam).
_INDIC = (0x0900, 0x0D7F)


def _clean(choice: str | None) -> str:
    return choice if choice in CHOICES else "auto"


def expected(last_inbound: str | None, choice: str | None = "auto") -> str | None:
    """
    The script the reply must be in: "latin", "devanagari", the name of another Indian script ("tamil"...), or None
    when there is nothing to go on (no letters in their message and no fixed rule).
    """
    choice = _clean(choice)
    if choice == "en":
        return "latin"
    if choice == "hi":
        return "devanagari"
    detected = lang.detect(last_inbound)
    if detected in ("english", "hinglish"):
        return "latin"
    if detected == "hindi":
        return "devanagari"
    return detected


def hint(last_inbound: str | None, choice: str | None = "auto", named: str | None = None) -> str | None:
    """The one line the model reads about this reply's language, or None. `named`: the language judged from its words."""
    choice = _clean(choice)
    if choice == "en":
        return ("Reply language (set by the business): English, in English letters only - no Devanagari - whatever "
                "language the customer writes in.")
    if choice == "hi":
        return ("Reply language (set by the business): Hindi in Devanagari script, whatever language the customer "
                "writes in. Keep product names, prices and links as they are.")
    written = lang.detect(last_inbound)
    detected = lang.spoken(last_inbound, named)
    if detected != written and detected and detected != "hinglish":
        if written in ("english", "hinglish"):
            return (f"Reply language: their last message is {detected.title()} typed in English letters, so reply the "
                    f"same way - {detected.title()} in English letters.")
        return f"Reply language: their last message is in {detected.title()}, so reply in {detected.title()}, in its own script."
    if detected == "english":
        return ("Reply language: their last message is in English, so reply in English, in English letters only - "
                "no Devanagari, even if earlier messages were in Hindi.")
    if detected == "hinglish":
        return ("Reply language: their last message is Hindi typed in English letters (Hinglish), so reply the same "
                "way - Hinglish in English letters only, no Devanagari.")
    if detected == "hindi":
        return "Reply language: their last message is in Devanagari Hindi, so reply in Devanagari Hindi."
    if detected:
        return f"Reply language: their last message is in {detected.title()} script, so reply in {detected.title()}."
    return None


# Arabic script, as Urdu is written (and Sindhi, Kashmiri).
_ARABIC = ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))


def _arabic_script(text: str | None) -> bool:
    if not text:
        return False
    arabic = other = 0
    for ch in text:
        code = ord(ch)
        if any(low <= code <= high for low, high in _ARABIC):
            arabic += 1
        elif ch.isalpha():
            other += 1
    return arabic >= 3 and arabic > other


def spoken_hint(last_heard: str | None) -> str | None:
    """
    On a call the reply is read aloud, and the voice reads no Urdu script. When what the caller said came back from
    speech in Urdu script, the reply is the same everyday language written in Devanagari so the Hindi voice can say it.
    None otherwise - the usual hint() applies.
    """
    if _arabic_script(last_heard):
        return ("Reply language: the caller is speaking Urdu. Reply in simple spoken Urdu (everyday Hindustani) written "
                "in Devanagari script - your words are read aloud by a voice that cannot read Urdu script.")
    return None


def mismatched(reply: str | None, want: str | None) -> bool:
    """
    True when a reply is plainly in the wrong script: a Latin-letters reply carrying Indian-script words, or a
    Devanagari reply written in English letters. Numbers, prices and links are ignored; a brand name in English
    inside a Devanagari reply is normal and does not count.
    """
    if not reply or not want:
        return False
    latin = indic = deva = 0
    for ch in reply:
        code = ord(ch)
        if "a" <= ch.lower() <= "z":
            latin += 1
        elif _INDIC[0] <= code <= _INDIC[1]:
            indic += 1
            if _DEVANAGARI[0] <= code <= _DEVANAGARI[1]:
                deva += 1
    if want == "latin":
        return indic >= 3
    if want == "devanagari":
        return deva == 0 and latin >= 12
    return False


def correction(want: str | None) -> str:
    """Said to the model when its reply came back in the wrong script."""
    if want == "latin":
        return ("Your reply used Devanagari (or another Indian script). Write the same reply again in English letters "
                "only - English if they wrote English, Hinglish if they wrote Hinglish.")
    return "Your reply was in English letters. Write the same reply again in Devanagari Hindi."

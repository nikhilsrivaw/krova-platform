"""
Deterministic checks that decide whether an extraction model call can find
anything at all - so the call is skipped only when it provably could not.

services/workers/analyse.py runs the commitment, signal and quotation
extractors over a customer's conversation. It is triggered per inbound
message, and most inbound messages are "ok", "thanks" or a 👍 that follow
something already analysed. These functions look only at the messages that
arrived since the last analysis (the "window") and answer two questions:

  window_is_trivial   - is there nothing new here any extractor could use?
  window_may_quote    - could the business have named a price in it?

Both lean towards "yes, run it": a false "run" costs one model call, a
false "skip" loses a promise or a quote. Hence the narrow rules:

- Any outbound message in the window makes it non-trivial. Outbound
  messages are never analysed on their own (they are ingested with
  enqueue_analysis=False), so a business promise - "delivery Monday" - is
  only ever read when the customer's next message triggers a run, and that
  next message is very often just "ok".
- Any attachment (other than a sticker), button reply, form, location or
  shared contact is non-trivial.
- Any digit is non-trivial ("5 baje", "2000", "done 1234").
- Only pure acknowledgements are trivial. Affirmatives that can close a
  promise - "yes", "haan", "done", "pakka", "confirm", "sure" - are NOT on
  the list: "done" can mean "payment done", "haan" can accept a booking.
- Only a handful of plain acknowledgement emoji are trivial. 😡 👎 ❌ can
  be a complaint, and ❤️ 😍 👏 could be read as praise - both go to the model.
- "nice", "great", "good" stay off the word list for the same reason.
"""

import re

# Words that on their own carry nothing to extract. Deliberately excludes
# every affirmative or completion word - see the module docstring.
ACK_WORDS = frozenset({
    "ok", "okay", "okey", "okk", "okkk", "k", "kk", "kkk", "oky",
    "thanks", "thank", "you", "u", "thanku", "thankyou", "thanx", "thnx", "thx", "ty", "tq",
    "tysm", "much", "so", "very",
    "hmm", "hm", "hmmm", "hmmmm",
    "ji", "sir", "madam", "maam", "mam", "bhaiya", "bhai", "didi", "dear",
    "theek", "thik", "hai", "h", "he",
    "achha", "acha", "accha", "achcha",
    "noted",
    "shukriya", "dhanyavad", "dhanyawad",
    "ठीक", "है", "धन्यवाद", "शुक्रिया", "जी",
})

MAX_ACK_WORDS = 5

# Positive / neutral acknowledgement emoji only. Anything else - 😡 👎 ❌
# 😢 - may be a complaint and goes to the model.
ACK_EMOJI = frozenset("👍🙏👌🙂😊☺✅🤝")
# Variation selectors, skin tones and joiners that ride along with emoji.
_EMOJI_MODIFIERS = re.compile(
    "[" + chr(0xFE0F) + chr(0xFE0E) + chr(0x200D) + chr(0x1F3FB) + "-" + chr(0x1F3FF) + "]"
)

# Letters, plus the Devanagari block so vowel signs (matras) stay inside
# their word - Python's \w alone splits "धन्यवाद" at every matra.
_DEVANAGARI_LETTERS = (
    chr(0x0900) + "-" + chr(0x0963) + chr(0x0970) + "-" + chr(0x097F)  # excludes its digits
)
_WORD = re.compile(r"(?:[^\W\d_]|[" + _DEVANAGARI_LETTERS + "])+", re.UNICODE)
_DIGIT = re.compile(r"\d")
# Kinds of media that are content in themselves, as opposed to a sticker.
_TRIVIAL_MEDIA_KINDS = frozenset({"sticker"})

# A price can be written without a digit: "ek lakh", "pachas hazaar".
_PRICE_WORDS = re.compile(
    r"₹|\brs\.?\b|\binr\b|rupe|\blakh|\blac\b|hazaa?r|thousand|crore|\bcr\b|hundred|\bsau\b|"
    r"\bprice\b|\brate\b|\bquot|\bestimate|\bcost\b|\bcharges?\b|\bfees?\b|\bamount\b|"
    r"रुपए|रुपये|लाख|हज़ार|हजार",
    re.IGNORECASE,
)


def _text(message: dict) -> str:
    return (message.get("text") or "").strip()


def _media(message: dict) -> dict:
    return message.get("media") or {}


def message_is_trivial(message: dict) -> bool:
    """
    True only for an inbound message with nothing any extractor could use:
    a sticker, a positive emoji or reaction, or a short pure acknowledgement.
    """
    if message.get("direction") != "inbound":
        return False

    media = _media(message)
    if media.get("read_as"):
        return False
    kind = media.get("kind")
    if kind and kind not in _TRIVIAL_MEDIA_KINDS:
        return False

    text = _text(message)
    if not text:
        # A bare sticker, or an empty body.
        return True
    if _DIGIT.search(text):
        return False

    words = _WORD.findall(text.lower())
    leftover = _EMOJI_MODIFIERS.sub("", _WORD.sub("", text.lower()))
    leftover = re.sub(r"[\s.,!?~'\"-]", "", leftover)
    if any(ch not in ACK_EMOJI for ch in leftover):
        return False
    if len(words) > MAX_ACK_WORDS:
        return False
    return all(w in ACK_WORDS for w in words)


def window_is_trivial(window: list[dict]) -> bool:
    """
    True when every message since the last analysis is a trivial inbound
    message - so no extractor could find anything new. An empty window is
    not trivial (nothing to judge by; let the model decide).
    """
    return bool(window) and all(message_is_trivial(m) for m in window)


def window_may_quote(window: list[dict]) -> bool:
    """
    Could the business have named a price since the last analysis?

    A quotation is the business stating a price, so it needs an outbound
    message in the window that carries a number, a price word, or an
    attachment (a quote PDF or photo). Voice turns are always allowed
    through - spoken amounts are transcribed inconsistently.
    """
    for m in window:
        if m.get("direction") != "outbound":
            continue
        if m.get("channel") == "voice" or _media(m):
            return True
        text = _text(m)
        if _DIGIT.search(text) or _PRICE_WORDS.search(text):
            return True
    return False

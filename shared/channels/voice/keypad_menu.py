"""
The keypad menu a business builds for its phone line: "press 1 to talk to our
team, press 2 for our address, press 0 for the owner".

Stored on the voice connection (ChannelConnection.extra["keypad_menu"]) as plain
JSON, so no migration. This module only validates and describes it; the call
pipeline (pipeline.py) is what acts on a pressed digit, over the keypad events
Plivo already delivers on the call's stream.

The menu is announced right after the greeting and a digit works at any point in
the call. A caller who presses nothing simply talks to the AI as before, so
turning the menu on never removes the AI path.

Three things a key can do:
  transfer  ring a phone number (each key can have its own)
  say       read out a fixed message (address, hours, a link) and carry on
  ai        carry on with the AI assistant
"""

from collections.abc import Callable

MAX_OPTIONS = 10
MAX_LABEL = 60
MAX_MESSAGE = 400
MAX_INTRO = 400
ACTIONS = ("transfer", "say", "ai")
DIGITS = "0123456789"


class MenuError(ValueError):
    """The menu a business sent cannot be used; the message is shown to them."""


def clean(raw: dict, normalise_number: Callable[[str], str]) -> dict:
    """Validate a menu from the settings screen and return the form that gets stored."""
    options_in = raw.get("options") or []
    if len(options_in) > MAX_OPTIONS:
        raise MenuError(f"A menu can have at most {MAX_OPTIONS} keys.")

    options: list[dict] = []
    seen: set[str] = set()
    for item in options_in:
        digit = str(item.get("digit") or "").strip()
        if digit not in tuple(DIGITS):
            raise MenuError("Each key must be a single digit from 0 to 9.")
        if digit in seen:
            raise MenuError(f"Key {digit} is used twice.")
        seen.add(digit)

        label = str(item.get("label") or "").strip()
        if not label:
            raise MenuError(f"Key {digit} needs a short name, for example \"our sales team\".")
        if len(label) > MAX_LABEL:
            raise MenuError(f"Key {digit}: the name is too long (most {MAX_LABEL} characters).")

        action = str(item.get("action") or "").strip()
        if action not in ACTIONS:
            raise MenuError(f"Key {digit}: choose what the key does.")

        option = {"digit": digit, "label": label, "action": action}
        if action == "transfer":
            number = str(item.get("number") or "").strip()
            if not number:
                raise MenuError(f"Key {digit}: enter the phone number to ring.")
            try:
                option["number"] = normalise_number(number)
            except Exception as exc:  # the identity normaliser's own message is the useful one
                raise MenuError(f"Key {digit}: {exc}") from exc
        elif action == "say":
            message = str(item.get("message") or "").strip()
            if not message:
                raise MenuError(f"Key {digit}: type what the agent should say.")
            if len(message) > MAX_MESSAGE:
                raise MenuError(f"Key {digit}: the message is too long (most {MAX_MESSAGE} characters).")
            option["message"] = message
        options.append(option)

    # Read out 1, 2, 3 ... and 0 last, the way phone menus are spoken.
    options.sort(key=lambda o: (o["digit"] == "0", o["digit"]))
    intro = str(raw.get("intro") or "").strip()
    if len(intro) > MAX_INTRO:
        raise MenuError(f"The menu announcement is too long (most {MAX_INTRO} characters).")

    enabled = bool(raw.get("enabled"))
    if enabled and not options:
        raise MenuError("Add at least one key before turning the menu on.")
    return {"enabled": enabled, "intro": intro, "options": options}


def is_live(menu: dict | None) -> bool:
    return bool(menu and menu.get("enabled") and menu.get("options"))


def option_for(menu: dict | None, digit: str) -> dict | None:
    if not is_live(menu):
        return None
    return next((o for o in menu["options"] if o["digit"] == digit), None)


_PRESS = {
    "hi-IN": ("{label} के लिए {digit} दबाएं।", "या बस बताइए कि मैं आपकी कैसे मदद करूं।"),
}


def spoken_prompt(menu: dict | None, language: str = "en-IN") -> str:
    """What is said after the greeting. The owner's own wording wins; otherwise it is built from the keys."""
    if not is_live(menu):
        return ""
    intro = (menu.get("intro") or "").strip()
    if intro:
        return intro
    press, tail = _PRESS.get(language, ("Press {digit} for {label}.", "Or just tell me how I can help."))
    parts = [press.format(digit=o["digit"], label=o["label"]) for o in menu["options"]]
    return " ".join([*parts, tail])

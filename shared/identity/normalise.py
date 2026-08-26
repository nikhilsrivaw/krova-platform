"""
Turning handles into comparable values.

Identity resolution is only ever as good as normalisation. "+91 98765 43210",
"098765 43210" and "919876543210" are one person, and if they are stored as
three different strings they become three customers with three separate
histories - and the cross-channel memory that justifies this whole product
quietly stops working.

So every identifier is normalised once, on the way in, and stored in exactly
one form.
"""

import re

# Krova's market. A bare 10-digit number means India unless told otherwise.
DEFAULT_REGION_CODE = "91"

_NON_DIGITS = re.compile(r"[^\d]")
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


class InvalidIdentifier(ValueError):
    """The value cannot be normalised into a usable identifier."""


def normalise_phone(raw: str, default_region: str = DEFAULT_REGION_CODE) -> str:
    """
    Reduce a phone number to E.164 without the plus: 919876543210.

    Stored without the plus because that is the form WhatsApp webhooks use,
    and converting on the way in beats converting on every lookup.

    Deliberately simple, and India-shaped. A full libphonenumber pass is the
    right answer once numbers arrive from outside India - which, given Plivo's
    India region is domestic-only, is not yet.
    """
    if not raw:
        raise InvalidIdentifier("Phone number is empty")

    digits = _NON_DIGITS.sub("", raw)
    if not digits:
        raise InvalidIdentifier(f"No digits in phone number: {raw!r}")

    # Indian trunk prefix: 098765 43210 -> 9876543210
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    # Bare local number: assume the default region.
    if len(digits) == 10:
        digits = default_region + digits

    # 00 as an international prefix: 0091... -> 91...
    if digits.startswith("00"):
        digits = digits[2:]

    if not 10 <= len(digits) <= 15:  # E.164 allows at most 15
        raise InvalidIdentifier(f"Not a usable phone number: {raw!r}")

    return digits


def normalise_email(raw: str) -> str:
    """
    Lowercase and trim.

    Nothing cleverer. Stripping dots or +tags from Gmail addresses would fold
    two addresses a business treats as different people into one customer, and
    a wrong merge is far worse than a missed one - it shows one person another
    person's history.
    """
    if not raw:
        raise InvalidIdentifier("Email is empty")

    value = raw.strip().lower()
    # Handle "Name <addr@example.com>", which is how email headers arrive.
    if "<" in value and ">" in value:
        value = value[value.rfind("<") + 1 : value.rfind(">")].strip()

    if not _EMAIL_SHAPE.match(value):
        raise InvalidIdentifier(f"Not a usable email address: {raw!r}")

    return value


def normalise_instagram(raw: str) -> str:
    """
    Instagram-scoped user id.

    An opaque numeric id, not a handle: handles change, ids do not. Anyone
    passing an @username here has the wrong value.
    """
    if not raw:
        raise InvalidIdentifier("Instagram id is empty")

    value = raw.strip()
    if value.startswith("@"):
        raise InvalidIdentifier(
            "Expected an Instagram-scoped user id, not a @handle - handles change"
        )
    return value


def normalise(kind: str, raw: str) -> str:
    """Normalise by identity kind. Raises InvalidIdentifier if unusable."""
    match kind:
        case "phone" | "whatsapp":
            return normalise_phone(raw)
        case "email":
            return normalise_email(raw)
        case "instagram":
            return normalise_instagram(raw)
        case _:
            raise InvalidIdentifier(f"Unknown identity kind: {kind!r}")

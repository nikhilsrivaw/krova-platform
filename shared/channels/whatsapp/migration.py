"""
Migrating a number from another provider.

The acquisition path. A business already paying AiSensy or Interakt can bring
their number to Krova - same number, same customers, no rebuilding of their
reputation.

What makes it worth building is what carries over: display name, quality
rating, messaging limit tier, Official Business Account status, and
high-quality approved templates are duplicated and auto-approved on arrival.
A client on TIER_10K stays on TIER_10K. Starting fresh would put them back to
250 messages a day and months of earning quality again.

The one step we cannot do is the one that blocks everything: two-step
verification must be OFF on the number, and only the losing provider - or the
number's owner in their own Business Suite - can switch it off. Any UI has to
say that first, because a client who starts without it fails at step one with
an error that does not explain itself.
"""

import re
from dataclasses import dataclass, field

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class MigrationError(Exception):
    """A migration step failed. The message is shown to the client."""

    def __init__(self, message: str, *, code: int | None = None, step: str | None = None):
        super().__init__(message)
        self.code = code
        self.step = step


_MEANINGS = {
    100: "Meta rejected the request - check the country code and number",
    133004: "That number is already registered on another account",
    133005: "The two-step verification PIN is wrong",
    133006: "This number must be verified before it can be registered",
    133008: "Too many PIN attempts - wait before trying again",
    133009: (
        "Two-step verification is still switched on for this number. Your "
        "current provider must turn it off before it can be moved."
    ),
    133016: "Too many attempts on this number - Meta allows 10 per 72 hours",
    136024: "That verification code is wrong or has expired",
    136025: "This number cannot be migrated - its display name is not approved",
}


@dataclass(slots=True)
class MigrationStart:
    phone_number_id: str
    display_phone_number: str


@dataclass(slots=True)
class Prerequisite:
    label: str
    met: bool | None          # None means we cannot check it from here
    detail: str
    who_fixes: str


@dataclass(slots=True)
class Readiness:
    can_start: bool
    checks: list[Prerequisite] = field(default_factory=list)


# Country codes we can split correctly. Guessing by length does not work -
# "+14155550132" would split as ("141", ...) rather than ("1", ...) - so this
# matches against real codes, longest first. India leads because it is the
# market; the rest are here so a client with an overseas number is not stuck.
_COUNTRY_CODES = {
    "91",   # India
    "1",    # US / Canada
    "44",   # UK
    "61",   # Australia
    "65",   # Singapore
    "971",  # UAE
    "966",  # Saudi Arabia
    "60",   # Malaysia
    "62",   # Indonesia
    "63",   # Philippines
    "66",   # Thailand
    "94",   # Sri Lanka
    "880",  # Bangladesh
    "977",  # Nepal
    "92",   # Pakistan
    "49",   # Germany
    "33",   # France
    "39",   # Italy
    "34",   # Spain
    "31",   # Netherlands
    "27",   # South Africa
    "254",  # Kenya
    "234",  # Nigeria
    "20",   # Egypt
    "55",   # Brazil
    "52",   # Mexico
    "86",   # China
    "81",   # Japan
    "82",   # South Korea
}


def split_number(raw: str) -> tuple[str, str]:
    """
    Meta wants country code and number separately.

    "+91 93693 59067" becomes ("91", "9369359067").

    Length alone cannot resolve this: "6581234567" is a Singapore number with
    its country code and also a valid Indian mobile without one. The leading
    "+" is the only reliable signal, so it decides - written with a plus, the
    country code is read from the front; written without, a ten-digit number
    is Indian, which is the market.
    """
    text = (raw or "").strip()
    explicit = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"[^\d]", "", text)

    if not digits:
        raise MigrationError("Enter a phone number")

    if digits.startswith("00"):
        digits = digits[2:]

    # Written without a country code: ten digits is an Indian mobile.
    if not explicit and len(digits) == 10:
        return "91", digits

    # Longest matching country code wins - 971 before 97, 91 before 9.
    for size in (4, 3, 2, 1):
        code = digits[:size]
        if code in _COUNTRY_CODES and len(digits) - size >= 7:
            return code, digits[size:]

    # Written with a plus but no code we know, or an odd length.
    if len(digits) == 10:
        return "91", digits

    raise MigrationError(
        f"Could not read {raw!r} as a phone number. Include the country code, "
        "for example +91 93693 59067."
    )


def _explain(payload: dict, step: str) -> MigrationError:
    error = payload.get("error") or {}
    code = error.get("code")
    detail = (
        error.get("error_user_msg")
        or (error.get("error_data") or {}).get("details")
        or error.get("message")
        or ""
    )
    known = _MEANINGS.get(code)
    message = known or detail or "Meta refused this step"
    if known and detail and detail not in known:
        message = f"{known} ({detail})"
    return MigrationError(message, code=code, step=step)


class MigrationClient:
    """Moves one number onto a client's WABA."""

    def __init__(self, access_token: str, waba_id: str, *, timeout: float = 30.0):
        self._token = access_token
        self._waba_id = waba_id
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _post(self, path: str, step: str, **kwargs) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{settings.graph_base_url}/{path}", headers=self._headers, **kwargs
            )
        payload = response.json() if response.content else {}
        if response.status_code != 200:
            error = _explain(payload, step)
            logger.warning("migration %s failed (%s): %s", step, error.code, error)
            raise error
        return payload

    async def start(self, phone: str) -> MigrationStart:
        """
        Claim the number onto this WABA.

        Fails here if two-step verification is still on at the old provider,
        which is the commonest way this goes wrong.
        """
        cc, number = split_number(phone)
        payload = await self._post(
            f"{self._waba_id}/phone_numbers",
            step="start",
            data={"cc": cc, "phone_number": number, "migrate_phone_number": "true"},
        )
        phone_number_id = payload.get("id")
        if not phone_number_id:
            raise MigrationError("Meta did not return a phone number id", step="start")

        logger.info("migration started waba=%s number=+%s%s", self._waba_id, cc, number)
        return MigrationStart(
            phone_number_id=str(phone_number_id),
            display_phone_number=f"+{cc}{number}",
        )

    async def request_code(
        self, phone_number_id: str, *, method: str = "SMS", language: str = "en"
    ) -> bool:
        payload = await self._post(
            f"{phone_number_id}/request_code",
            step="request_code",
            data={"code_method": method, "language": language},
        )
        return bool(payload.get("success", True))

    async def verify_code(self, phone_number_id: str, code: str) -> bool:
        payload = await self._post(
            f"{phone_number_id}/verify_code",
            step="verify_code",
            data={"code": code.strip()},
        )
        return bool(payload.get("success", True))

    async def register(self, phone_number_id: str, pin: str) -> bool:
        """
        Finish the move.

        The PIN becomes the number's new two-step verification PIN, so it must
        be stored - re-registering later needs this exact value and Meta will
        not tell us what it is.
        """
        if not (pin.isdigit() and len(pin) == 6):
            raise MigrationError("The PIN must be exactly 6 digits", step="register")
        payload = await self._post(
            f"{phone_number_id}/register",
            step="register",
            json={"messaging_product": "whatsapp", "pin": pin},
        )
        return bool(payload.get("success", True))

    async def readiness(self) -> Readiness:
        """
        What must be true before a migration can start.

        Two of these we can check and two we cannot. Saying so plainly beats a
        green tick we have not earned - especially the two-step one, which is
        where migrations actually fail.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{settings.graph_base_url}/{self._waba_id}",
                headers=self._headers,
                params={"fields": "business_verification_status,account_review_status"},
            )
        waba = response.json() if response.status_code == 200 else {}

        verified = waba.get("business_verification_status") == "verified"
        reviewed = waba.get("account_review_status") == "APPROVED"

        checks = [
            Prerequisite(
                label="Your business is verified with Meta",
                met=verified,
                detail="Required before a number can be moved onto your account.",
                who_fixes="You, in Meta Business Settings",
            ),
            Prerequisite(
                label="Your WhatsApp account is approved",
                met=reviewed,
                detail="Meta reviews the account before it can receive a number.",
                who_fixes="Meta, automatically",
            ),
            Prerequisite(
                label="Two-step verification is OFF on the number",
                met=None,
                detail=(
                    "This is the step migrations fail on. Your current provider "
                    "must switch it off, or you can do it yourself in Meta "
                    "Business Suite."
                ),
                who_fixes="Your current provider",
            ),
            Prerequisite(
                label="A payment method is on your WhatsApp account",
                met=None,
                detail=(
                    "Meta bills you directly for messages. Krova adds no charge, "
                    "but nothing delivers without one."
                ),
                who_fixes="You, in Meta Business Suite",
            ),
        ]

        return Readiness(can_start=verified and reviewed, checks=checks)

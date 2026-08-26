"""
Managing a client's WhatsApp account: the number, its profile, its health.

Everything a business would otherwise open WhatsApp Manager to do. A Tech
Provider that sends messages but makes its clients leave for anything else is
a messaging integration wearing a platform's name.

Three groups of operations, and they fail in different ways.

The business profile is what customers actually see when they open the chat -
the description, the photo, the address. It is the most visible thing on the
account and the easiest to leave wrong for months.

Number verification is a two-step handshake: request a code, then submit it.
The code arrives by SMS or voice call to the number itself, so a client
holding the phone has to be in the loop.

Health - quality rating, messaging tier, throughput - is read-only and is the
early warning nobody watches. Quality drops before a number gets restricted;
by the time messages stop, it is too late to act.
"""

from dataclasses import dataclass
from typing import Any, Literal

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Meta's vertical list for the business profile. Shown to the client as a
# choice, so the labels matter as much as the values.
VERTICALS = {
    "AUTO": "Automotive",
    "BEAUTY": "Beauty, spa and salon",
    "APPAREL": "Clothing and apparel",
    "EDU": "Education",
    "ENTERTAIN": "Entertainment",
    "EVENT_PLAN": "Event planning and service",
    "FINANCE": "Finance and banking",
    "GROCERY": "Food and grocery",
    "GOVT": "Public service",
    "HOTEL": "Hotel and lodging",
    "HEALTH": "Medical and health",
    "NONPROFIT": "Non-profit",
    "PROF_SERVICES": "Professional services",
    "RETAIL": "Shopping and retail",
    "TRAVEL": "Travel and transportation",
    "RESTAURANT": "Restaurant",
    "NOT_A_BIZ": "Not a business",
    "OTHER": "Other",
}

# Meta's limits on profile fields.
MAX_ABOUT = 139
MAX_DESCRIPTION = 512
MAX_ADDRESS = 256
MAX_EMAIL = 128
MAX_WEBSITES = 2

CodeMethod = Literal["SMS", "VOICE"]


class AccountError(Exception):
    """Meta refused an account operation."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


_MEANINGS = {
    131000: "Something went wrong on Meta's side - try again shortly",
    133005: "That two-step verification PIN is wrong",
    133006: "This number must be verified before it can be registered",
    133008: "Too many PIN attempts - wait before trying again",
    133010: "This number is not registered on WhatsApp Business",
    133016: "Too many attempts on this number - Meta allows 10 per 72 hours",
    136024: "That verification code is wrong or has expired",
    100: "Meta rejected the request - check the values being sent",
}


@dataclass(slots=True)
class BusinessProfile:
    """What a customer sees when they open the chat."""

    about: str | None = None
    address: str | None = None
    description: str | None = None
    email: str | None = None
    websites: list[str] | None = None
    vertical: str | None = None
    profile_picture_url: str | None = None

    def validate(self) -> None:
        if self.about and len(self.about) > MAX_ABOUT:
            raise AccountError(f"'About' must be under {MAX_ABOUT} characters")
        if self.description and len(self.description) > MAX_DESCRIPTION:
            raise AccountError(f"Description must be under {MAX_DESCRIPTION} characters")
        if self.address and len(self.address) > MAX_ADDRESS:
            raise AccountError(f"Address must be under {MAX_ADDRESS} characters")
        if self.email and len(self.email) > MAX_EMAIL:
            raise AccountError(f"Email must be under {MAX_EMAIL} characters")
        if self.websites and len(self.websites) > MAX_WEBSITES:
            raise AccountError(f"At most {MAX_WEBSITES} websites")
        if self.vertical and self.vertical not in VERTICALS:
            raise AccountError(f"Unknown business category: {self.vertical}")


@dataclass(slots=True)
class Blocker:
    """One reason an account cannot send, in Meta's own words."""

    entity: str          # PHONE_NUMBER | WABA | BUSINESS | APP
    state: str           # AVAILABLE | LIMITED | BLOCKED
    code: int | None
    message: str
    fix: str | None


@dataclass(slots=True)
class Readiness:
    """
    Whether a client can actually send, and what stands in the way.

    can_send is Meta's verdict, not ours: AVAILABLE, LIMITED or BLOCKED. The
    most common cause of BLOCKED on a freshly onboarded client is no payment
    method on their WABA - the one step a Tech Provider cannot do for them.
    """

    can_send: str
    blockers: list[Blocker]
    notes: list[str]

    @property
    def ready(self) -> bool:
        return self.can_send == "AVAILABLE"

    @property
    def needs_payment_method(self) -> bool:
        """
        True when the blocker looks like a missing payment method.

        Matched on Meta's text because there is no dedicated code or field for
        it - the only signal we get is the description they return.
        """
        haystack = " ".join(
            [b.message.lower() for b in self.blockers] + [n.lower() for n in self.notes]
        )
        return any(
            phrase in haystack
            for phrase in ("payment method", "billing", "payment information", "credit card")
        )


@dataclass(slots=True)
class NumberHealth:
    """
    The state of a number, in one object.

    quality_rating and messaging_limit_tier are the two that decide whether a
    business can keep operating: quality falls first, then Meta restricts, and
    only then do messages visibly stop.
    """

    phone_number_id: str
    display_phone_number: str | None
    verified_name: str | None
    quality_rating: str | None
    messaging_limit_tier: str | None
    status: str | None
    code_verification_status: str | None
    name_status: str | None
    throughput_level: str | None
    account_mode: str | None
    is_official_business_account: bool
    platform_type: str | None

    @property
    def healthy(self) -> bool:
        return self.quality_rating in (None, "GREEN") and self.status == "CONNECTED"

    @property
    def daily_limit(self) -> int | None:
        """Unique recipients per day, from the tier name."""
        tiers = {
            "TIER_50": 50,
            "TIER_250": 250,
            "TIER_1K": 1_000,
            "TIER_10K": 10_000,
            "TIER_100K": 100_000,
            "TIER_UNLIMITED": None,
        }
        return tiers.get(self.messaging_limit_tier or "", None)


def _explain(payload: dict) -> AccountError:
    error = payload.get("error") or {}
    code = error.get("code")
    detail = (
        error.get("error_user_msg")
        or (error.get("error_data") or {}).get("details")
        or error.get("message")
        or ""
    )
    known = _MEANINGS.get(code)
    message = known or detail or "Meta rejected the request"
    if known and detail and detail not in known:
        message = f"{known} ({detail})"
    return AccountError(message, code=code)


class AccountClient:
    """Account operations for one connected number."""

    def __init__(self, access_token: str, phone_number_id: str, *, timeout: float = 25.0):
        self._token = access_token
        self._number = phone_number_id
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        url = f"{settings.graph_base_url}/{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(method, url, headers=self._headers, **kwargs)
        payload = response.json() if response.content else {}
        if response.status_code != 200:
            error = _explain(payload)
            logger.warning("account %s %s failed (%s): %s", method, path, error.code, error)
            raise error
        return payload

    # ── profile ──────────────────────────────────────────────────────────────

    async def get_profile(self) -> BusinessProfile:
        payload = await self._call(
            "GET",
            f"{self._number}/whatsapp_business_profile",
            params={
                "fields": "about,address,description,email,profile_picture_url,"
                "websites,vertical"
            },
        )
        data = (payload.get("data") or [{}])[0]
        return BusinessProfile(
            about=data.get("about"),
            address=data.get("address"),
            description=data.get("description"),
            email=data.get("email"),
            websites=data.get("websites"),
            vertical=data.get("vertical"),
            profile_picture_url=data.get("profile_picture_url"),
        )

    async def update_profile(self, profile: BusinessProfile) -> bool:
        """
        Change what customers see.

        Only the fields actually supplied are sent - Meta treats an omitted
        field as unchanged, and sending null would wipe it.
        """
        profile.validate()
        body: dict[str, Any] = {"messaging_product": "whatsapp"}
        for field in ("about", "address", "description", "email", "vertical"):
            value = getattr(profile, field)
            if value is not None:
                body[field] = value
        if profile.websites is not None:
            body["websites"] = profile.websites

        if len(body) == 1:
            raise AccountError("Nothing to update")

        payload = await self._call(
            "POST", f"{self._number}/whatsapp_business_profile", json=body
        )
        logger.info("profile updated number=%s fields=%s", self._number,
                    sorted(k for k in body if k != "messaging_product"))
        return bool(payload.get("success", True))

    # ── health ───────────────────────────────────────────────────────────────

    async def health(self) -> NumberHealth:
        payload = await self._call(
            "GET",
            self._number,
            params={
                "fields": "id,display_phone_number,verified_name,quality_rating,"
                "messaging_limit_tier,status,code_verification_status,name_status,"
                "throughput,account_mode,is_official_business_account,platform_type"
            },
        )
        return NumberHealth(
            phone_number_id=payload.get("id", self._number),
            display_phone_number=payload.get("display_phone_number"),
            verified_name=payload.get("verified_name"),
            quality_rating=payload.get("quality_rating"),
            messaging_limit_tier=payload.get("messaging_limit_tier"),
            status=payload.get("status"),
            code_verification_status=payload.get("code_verification_status"),
            name_status=payload.get("name_status"),
            throughput_level=(payload.get("throughput") or {}).get("level"),
            account_mode=payload.get("account_mode"),
            is_official_business_account=bool(
                payload.get("is_official_business_account")
            ),
            platform_type=payload.get("platform_type"),
        )

    async def readiness(self) -> "Readiness":
        """
        Meta's own answer to "can this client send right now".

        Undocumented in the Tech Provider guides but live on the API, and the
        only way to know that a client is blocked before their first message
        silently fails.
        """
        payload = await self._call(
            "GET", self._number, params={"fields": "health_status"}
        )
        health = payload.get("health_status") or {}

        blockers: list[Blocker] = []
        notes: list[str] = []

        for entity in health.get("entities") or []:
            state = entity.get("can_send_message", "AVAILABLE")
            for info in entity.get("additional_info") or []:
                notes.append(str(info))
            for error in entity.get("errors") or []:
                blockers.append(
                    Blocker(
                        entity=entity.get("entity_type", "UNKNOWN"),
                        state=state,
                        code=error.get("error_code"),
                        message=error.get("error_description", ""),
                        fix=error.get("possible_solution"),
                    )
                )
            # An entity can be blocked with no error attached - record it so the
            # client is not told everything is fine when Meta says otherwise.
            if state == "BLOCKED" and not (entity.get("errors") or []):
                blockers.append(
                    Blocker(
                        entity=entity.get("entity_type", "UNKNOWN"),
                        state=state,
                        code=None,
                        message=f"{entity.get('entity_type')} is blocked from sending",
                        fix=None,
                    )
                )

        return Readiness(
            can_send=health.get("can_send_message", "UNKNOWN"),
            blockers=blockers,
            notes=notes,
        )

    # ── verification and registration ────────────────────────────────────────

    async def request_verification_code(
        self, *, method: CodeMethod = "SMS", language: str = "en"
    ) -> bool:
        """
        Ask Meta to send a verification code to the number itself.

        The client has to be holding the phone - there is no way around that,
        and any UI should say so before starting.
        """
        payload = await self._call(
            "POST",
            f"{self._number}/request_code",
            json={"code_method": method, "language": language},
        )
        logger.info("verification code requested number=%s via %s", self._number, method)
        return bool(payload.get("success", True))

    async def verify_code(self, code: str) -> bool:
        payload = await self._call(
            "POST", f"{self._number}/verify_code", json={"code": code.strip()}
        )
        return bool(payload.get("success", True))

    async def register(self, pin: str) -> bool:
        """
        Register the number for Cloud API.

        `pin` becomes its two-step verification PIN, and re-registering later
        needs the same value - there is no way to read it back from Meta, so
        the caller must store it.

        Note: data_localization_region is deliberately not sent. Meta's docs
        still list it, but the live API rejects it on v21.0+.
        """
        payload = await self._call(
            "POST",
            f"{self._number}/register",
            json={"messaging_product": "whatsapp", "pin": pin},
        )
        return bool(payload.get("success", True))

    async def deregister(self) -> bool:
        payload = await self._call("POST", f"{self._number}/deregister", json={})
        return bool(payload.get("success", True))

    async def set_two_step_pin(self, pin: str) -> bool:
        """
        Change the number's two-step verification PIN.

        Six digits. Meta requires the current PIN to change it, so a client who
        has lost theirs has to go through support - worth warning about before
        they set one.
        """
        if not (pin.isdigit() and len(pin) == 6):
            raise AccountError("The PIN must be exactly 6 digits")
        payload = await self._call("POST", self._number, json={"pin": pin})
        return bool(payload.get("success", True))


class WabaClient:
    """Operations on the WhatsApp Business Account itself."""

    def __init__(self, access_token: str, waba_id: str, *, timeout: float = 25.0):
        self._token = access_token
        self._waba_id = waba_id
        self._timeout = timeout

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        url = f"{settings.graph_base_url}/{path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        payload = response.json() if response.content else {}
        if response.status_code != 200:
            raise _explain(payload)
        return payload

    async def details(self) -> dict:
        """Account name, currency, review status, and who owns it."""
        return await self._call(
            "GET",
            self._waba_id,
            params={
                "fields": "id,name,currency,timezone_id,account_review_status,"
                "business_verification_status,message_template_namespace,"
                "owner_business_info"
            },
        )

    async def phone_numbers(self) -> list[dict]:
        payload = await self._call(
            "GET",
            f"{self._waba_id}/phone_numbers",
            params={
                "fields": "id,display_phone_number,verified_name,quality_rating,"
                "status,code_verification_status,messaging_limit_tier,platform_type"
            },
        )
        return payload.get("data", [])

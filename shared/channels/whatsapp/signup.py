"""
Embedded Signup - connecting a business's own WhatsApp account.

The flow, per Meta's implementation guide:

  1. The browser completes Meta's dialog and hands us a short-lived code.
  2. We exchange it server-side for a business integration system user token.
  3. debug_token tells us which WhatsApp Business Account was granted, and
     when the token expires.
  4. We read the account and its phone numbers.
  5. We subscribe our app to the account's webhooks.
  6. We register the number so it can send.

Steps 5 and 6 are the ones that decide whether the connection actually works,
and both fail silently if skipped. An unsubscribed WABA delivers nothing to
our webhook - no error, no warning, just a business that connected
successfully and never receives a message. It is the most commonly missed step
in Embedded Signup, which is why it is not optional here and why its result is
stored rather than assumed.

Token expiry is read from debug_token rather than assumed to be sixty days.
Meta decides the lifetime; guessing it means guessing when a client goes
silent.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class SignupError(Exception):
    """Signup could not be completed. The message is shown to the business."""


@dataclass(slots=True)
class GraphCall:
    """One Graph request we made, surfaced so the UI can show the working."""

    method: str
    path: str
    permission: str
    status: int


@dataclass(slots=True)
class SignupResult:
    access_token: str
    token_expires_at: datetime | None
    waba_id: str
    waba_name: str | None
    phone_number_id: str
    display_phone_number: str | None
    verified_name: str | None
    quality_rating: str | None
    webhook_subscribed: bool
    number_registered: bool
    registration_pin: str | None
    calls: list[GraphCall] = field(default_factory=list)


async def complete_signup(code: str, *, pin: str) -> SignupResult:
    """
    Turn the dialog's authorisation code into a working connection.

    `pin` becomes the number's two-step verification PIN, so the caller must
    store it: re-registering this number later requires the same value.
    """
    if not settings.meta_app_id or not settings.meta_app_secret:
        raise SignupError("WhatsApp signup is not configured on this server")

    code = code.strip()
    if not code:
        raise SignupError("No authorisation code was provided")

    base = settings.graph_base_url
    calls: list[GraphCall] = []

    def record(method: str, path: str, permission: str, status: int) -> None:
        calls.append(GraphCall(method, path, permission, status))

    async with httpx.AsyncClient(timeout=25.0) as client:
        # 1 - code -> business integration system user token
        token_res = await client.get(
            f"{base}/oauth/access_token",
            params={
                "client_id": settings.meta_app_id,
                "client_secret": settings.meta_app_secret,
                "code": code,
            },
        )
        record("GET", "/oauth/access_token", "-", token_res.status_code)
        if token_res.status_code != 200:
            logger.error("code exchange failed: %s", token_res.text[:300])
            raise SignupError("Could not complete the connection with Meta")

        access_token = token_res.json().get("access_token")
        if not access_token:
            raise SignupError("Meta did not return an access token")

        auth = {"Authorization": f"Bearer {access_token}"}
        app_token = f"{settings.meta_app_id}|{settings.meta_app_secret}"

        # 2 - which account was granted, and for how long
        debug_res = await client.get(
            f"{base}/debug_token",
            params={"input_token": access_token},
            headers={"Authorization": f"Bearer {app_token}"},
        )
        record("GET", "/debug_token", "-", debug_res.status_code)

        waba_id: str | None = None
        expires_at: datetime | None = None

        if debug_res.status_code == 200:
            data = debug_res.json().get("data", {})
            # 0 means "does not expire". Anything else is a Unix timestamp.
            raw_expiry = data.get("data_access_expires_at") or data.get("expires_at")
            if raw_expiry:
                expires_at = datetime.fromtimestamp(int(raw_expiry), tz=timezone.utc)

            for scope in data.get("granular_scopes", []):
                if scope.get("scope") == "whatsapp_business_management":
                    targets = scope.get("target_ids") or []
                    if targets:
                        waba_id = targets[0]
                        break

        if not waba_id:
            raise SignupError(
                "No WhatsApp Business Account was shared during signup. "
                "Please try again and select an account."
            )

        # 3 - read the account and its numbers
        waba_res = await client.get(
            f"{base}/{waba_id}",
            params={"fields": "id,name,currency,timezone_id"},
            headers=auth,
        )
        record("GET", f"/{waba_id}", "whatsapp_business_management", waba_res.status_code)
        waba = waba_res.json() if waba_res.status_code == 200 else {}

        numbers_res = await client.get(
            f"{base}/{waba_id}/phone_numbers",
            params={
                "fields": "id,display_phone_number,verified_name,quality_rating"
            },
            headers=auth,
        )
        record(
            "GET",
            f"/{waba_id}/phone_numbers",
            "whatsapp_business_management",
            numbers_res.status_code,
        )
        numbers = (
            numbers_res.json().get("data", []) if numbers_res.status_code == 200 else []
        )
        if not numbers:
            raise SignupError(
                "That WhatsApp Business Account has no phone number on it yet"
            )

        primary = numbers[0]
        phone_number_id = primary["id"]

        # 4 - subscribe to this account's webhooks
        #
        # Without this, Meta delivers nothing for this number and says nothing
        # about it. A connection that skips this looks perfect and is inert.
        sub_res = await client.post(f"{base}/{waba_id}/subscribed_apps", headers=auth)
        record(
            "POST",
            f"/{waba_id}/subscribed_apps",
            "whatsapp_business_management",
            sub_res.status_code,
        )
        subscribed = sub_res.status_code == 200 and sub_res.json().get("success", False)
        if not subscribed:
            logger.error(
                "subscribed_apps failed for waba=%s: %s", waba_id, sub_res.text[:300]
            )
            raise SignupError(
                "Connected, but we could not subscribe to this account's messages. "
                "Please try connecting again."
            )

        # 5 - register the number so it can send
        # NOTE: data_localization_region is NOT sent here. Meta's registration
        # docs still list it as an optional parameter, but the live API rejects
        # it on v21.0 and above:
        #
        #   (#12) Deprecated for versions v21.0 or higher
        #
        # Verified against the real API on v25.0. Local storage is now toggled
        # only while a number is unregistered, so enabling India residency for
        # a client means doing it as part of their first registration, through
        # whatever v21+ mechanism replaces this - not by adding the field back.
        reg_res = await client.post(
            f"{base}/{phone_number_id}/register",
            headers=auth,
            json={"messaging_product": "whatsapp", "pin": pin},
        )
        record(
            "POST",
            f"/{phone_number_id}/register",
            "whatsapp_business_messaging",
            reg_res.status_code,
        )
        registered = reg_res.status_code == 200 and reg_res.json().get("success", False)
        if not registered:
            # Usually "already registered", which is fine. 133016 means the
            # number is rate limited - ten attempts per 72 hours - and retrying
            # makes it worse, so this never retries automatically.
            logger.warning(
                "register returned %s for %s: %s",
                reg_res.status_code,
                phone_number_id,
                reg_res.text[:300],
            )

    return SignupResult(
        access_token=access_token,
        token_expires_at=expires_at,
        waba_id=waba_id,
        waba_name=waba.get("name"),
        phone_number_id=phone_number_id,
        display_phone_number=primary.get("display_phone_number"),
        verified_name=primary.get("verified_name"),
        quality_rating=primary.get("quality_rating"),
        webhook_subscribed=subscribed,
        number_registered=registered,
        registration_pin=pin,
        calls=calls,
    )

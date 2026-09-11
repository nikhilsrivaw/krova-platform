"""
The Plivo calls a business's voice onboarding needs, before any call happens.

Everything here talks to Plivo's REST API directly over httpx rather than
the official `plivo` SDK - the SDK is synchronous, and every other Plivo
integration in this codebase (signature verification, the Stream client) is
async. The SDK was still useful once: its source is what revealed the real
v3 signature algorithm and the real subaccount purchase flow, both of which
disagreed with the prose docs.

The one non-obvious rule this module encodes, confirmed against a real
account rather than assumed from docs: a subaccount can SEARCH numbers with
its own credentials, but BUYING one must go through the PARENT account's
credentials with a `subaccount` field in the body - calling PhoneNumber buy
directly as the subaccount returns a bare 404, not a permission error.
"""

from dataclasses import dataclass

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

BASE_URL = "https://api.plivo.com/v1"
TIMEOUT = 20.0


class PlivoError(Exception):
    """A Plivo API call failed. The message is safe to show to a business."""


def parent_auth() -> httpx.BasicAuth:
    if not settings.plivo_auth_id or not settings.plivo_auth_token:
        raise PlivoError("Krova's Plivo account is not configured")
    return httpx.BasicAuth(settings.plivo_auth_id, settings.plivo_auth_token)


@dataclass(slots=True)
class Subaccount:
    auth_id: str
    auth_token: str


async def create_subaccount(name: str) -> Subaccount:
    """
    One subaccount per business - separate Auth ID/Token, logs, webhooks,
    while billing stays on Krova's parent account. This is the reseller
    construct Plivo's own support confirmed is the correct model here.
    """
    url = f"{BASE_URL}/Account/{settings.plivo_auth_id}/Subaccount/"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(url, auth=parent_auth(), json={"name": name, "enabled": True})

    if res.status_code not in (200, 201):
        logger.warning("plivo subaccount create failed: %s %s", res.status_code, res.text)
        raise PlivoError("Could not create a Plivo subaccount")

    body = res.json()
    return Subaccount(auth_id=body["auth_id"], auth_token=body["auth_token"])


async def search_numbers(
    subaccount: Subaccount,
    *,
    country_iso: str = "IN",
    number_type: str = "local",
    pattern: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Available numbers, searched under the business's own subaccount."""
    url = f"{BASE_URL}/Account/{subaccount.auth_id}/PhoneNumber/"
    params = {"country_iso": country_iso, "type": number_type, "limit": limit}
    if pattern:
        params["pattern"] = pattern

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.get(
            url, auth=httpx.BasicAuth(subaccount.auth_id, subaccount.auth_token), params=params
        )

    if res.status_code != 200:
        logger.warning("plivo number search failed: %s %s", res.status_code, res.text)
        raise PlivoError("Could not search Plivo's number inventory")

    return res.json().get("objects", [])


async def buy_number(
    number: str, subaccount: Subaccount, *, compliance_application_id: str | None = None
) -> None:
    """
    Buy a number for a subaccount.

    Must be called with the PARENT account's credentials - a subaccount
    calling this on itself gets a plain 404, confirmed on a real account.
    Billing lands on Krova regardless of whose subaccount ends up owning the
    number, which is exactly the reseller billing model Plivo documents.
    """
    url = f"{BASE_URL}/Account/{settings.plivo_auth_id}/PhoneNumber/{number}/"
    payload: dict = {"subaccount": subaccount.auth_id}
    if compliance_application_id:
        payload["compliance_application_id"] = compliance_application_id

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(url, auth=parent_auth(), json=payload)

    if res.status_code not in (200, 201, 202):
        logger.warning("plivo number buy failed: %s %s", res.status_code, res.text)
        raise PlivoError(f"Could not buy {number}")


async def create_application(
    subaccount: Subaccount, *, app_name: str, answer_url: str, hangup_url: str
) -> str:
    """The Application a purchased number is linked to - the Answer URL Plivo calls."""
    url = f"{BASE_URL}/Account/{subaccount.auth_id}/Application/"
    payload = {
        "app_name": app_name,
        "answer_url": answer_url,
        "answer_method": "POST",
        "hangup_url": hangup_url,
        "hangup_method": "POST",
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            url, auth=httpx.BasicAuth(subaccount.auth_id, subaccount.auth_token), json=payload
        )

    if res.status_code not in (200, 201):
        logger.warning("plivo application create failed: %s %s", res.status_code, res.text)
        raise PlivoError("Could not create a Plivo Application")

    return res.json()["app_id"]


async def link_number(subaccount: Subaccount, number: str, app_id: str) -> None:
    """Point a purchased number at the Application that answers its calls."""
    url = f"{BASE_URL}/Account/{subaccount.auth_id}/Number/{number}/"

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            url,
            auth=httpx.BasicAuth(subaccount.auth_id, subaccount.auth_token),
            json={"app_id": app_id},
        )

    if res.status_code != 200:
        logger.warning("plivo number link failed: %s %s", res.status_code, res.text)
        raise PlivoError(f"Could not link {number} to its Application")


async def get_number(subaccount: Subaccount, number: str) -> dict:
    """
    A number's own record - read after buying it for the fields the search
    result doesn't carry once it's actually owned, chiefly `voice_rate` for
    per-call cost tracking.
    """
    url = f"{BASE_URL}/Account/{subaccount.auth_id}/Number/{number}/"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.get(
            url, auth=httpx.BasicAuth(subaccount.auth_id, subaccount.auth_token)
        )

    if res.status_code != 200:
        logger.warning("plivo number get failed: %s %s", res.status_code, res.text)
        raise PlivoError(f"Could not read {number}'s details")

    return res.json()


async def get_call_cdr(*, auth_id: str, auth_token: str, call_uuid: str) -> dict | None:
    """
    The authoritative billed record for one call - fetched after hangup,
    not estimated from duration times a rate. Confirmed directly with Plivo
    support this is the correct pattern: the hangup webhook carries timing
    and status only, never the final rated cost.

    `total_amount` and `total_rate` are in US dollars - confirmed against a
    real CDR and the account's own Pricing endpoint (a domestic India
    voice_network_group rate of "0.00475" is only plausible as USD/minute;
    as INR it would be under a paisa per minute). Nothing in the CDR
    response states the currency explicitly, which is exactly the kind of
    silent unit assumption that produced the original bug this replaces.

    Works for either a subaccount's own credentials or the parent account's -
    whichever owns the number the call was on.
    """
    url = f"{BASE_URL}/Account/{auth_id}/Call/{call_uuid}/"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.get(url, auth=httpx.BasicAuth(auth_id, auth_token))

    if res.status_code == 404:
        return None
    if res.status_code != 200:
        logger.warning("plivo CDR fetch failed: %s %s", res.status_code, res.text)
        raise PlivoError(f"Could not fetch call record for {call_uuid}")

    return res.json()


async def transfer_call(*, auth_id: str, auth_token: str, call_uuid: str, aleg_url: str) -> None:
    """
    Redirect a live call's A-leg to fresh XML fetched from `aleg_url`.

    Plivo's Live Call Modification API - tells an in-progress call to stop
    doing whatever it's currently doing (here: leave the Stream) and
    execute new XML instead. This is the warm-transfer mechanism: bridging
    an AI-answered call to a real human phone when the agent escalates,
    without hanging up and losing the caller. Works with either the parent
    account's credentials or a subaccount's, same as get_call_cdr above -
    whichever owns the number the call is on.
    """
    url = f"{BASE_URL}/Account/{auth_id}/Call/{call_uuid}/"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            url,
            auth=httpx.BasicAuth(auth_id, auth_token),
            json={"legs": "aleg", "aleg_url": aleg_url},
        )

    if res.status_code not in (200, 202):
        logger.warning("plivo call transfer failed: %s %s", res.status_code, res.text)
        raise PlivoError(f"Could not transfer call {call_uuid}")


async def make_call(
    *,
    auth_id: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    answer_url: str,
    hangup_url: str,
    machine_detection: str | None = None,
    machine_detection_time: int = 5000,
) -> str:
    """
    Place an outbound call. Returns Plivo's request_uuid - confirmed
    against Plivo's own docs this is NOT the eventual call_uuid (that only
    exists once Plivo actually dials out, and arrives later as `CallUUID`
    in the answer_url/hangup_url payloads) - so nothing here can key
    anything on call_uuid in advance. Whatever this call needs to
    correlate later belongs in answer_url/hangup_url's own query string,
    not something stashed against this return value.

    machine_detection is off by default, and that default was measured,
    not assumed. With it set to "hangup"/"true" Plivo holds the call for
    machine_detection_time (5000ms default) analysing the audio before it
    invokes answer_url at all - on a real call, Plivo's own CDR put
    answer_time at 17:50:18 and our answer webhook did not fire until
    17:50:24.2, a 6.2 second gap in which our code had not been told the
    call existed and the person who picked up heard nothing. Every
    proactive and campaign call was paying that, on top of the ~2s the
    agent itself needs to speak.

    The trade it buys back - never talking into a voicemail - is real but
    much cheaper to lose than six seconds of dead air on every answered
    call: a caller hangs up on silence long before an answering machine
    costs anyone anything. A caller that opts back in should use async
    detection (machine_detection_url) rather than this blocking form.
    """
    url = f"{BASE_URL}/Account/{auth_id}/Call/"
    payload = {
        "from": from_number,
        "to": to_number,
        "answer_url": answer_url,
        "answer_method": "POST",
        "hangup_url": hangup_url,
        "hangup_method": "POST",
    }
    if machine_detection:
        payload["machine_detection"] = machine_detection
        payload["machine_detection_time"] = machine_detection_time
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(url, auth=httpx.BasicAuth(auth_id, auth_token), json=payload)

    if res.status_code not in (200, 201, 202):
        logger.warning("plivo make_call failed: %s %s", res.status_code, res.text)
        raise PlivoError(f"Could not place a call to {to_number}")

    return res.json().get("request_uuid", "")


async def send_sms(*, auth_id: str, auth_token: str, from_number: str, to_number: str, text: str) -> None:
    """
    Fire-and-forget SMS via Plivo's Message API - unlike make_call above,
    needs no live answer_url/hangup_url webhook, which is exactly why this
    is the escalation failsafe's mechanism rather than a phone call: one
    POST, no new inbound-webhook surface to stand up.
    """
    url = f"{BASE_URL}/Account/{auth_id}/Message/"
    payload = {"src": from_number, "dst": to_number, "text": text}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(url, auth=httpx.BasicAuth(auth_id, auth_token), json=payload)

    if res.status_code not in (200, 201, 202):
        logger.warning("plivo send_sms failed: %s %s", res.status_code, res.text)
        raise PlivoError(f"Could not send SMS to {to_number}")


async def release_number(subaccount: Subaccount, number: str) -> None:
    """
    Give a number back to Plivo - a business that churns or no longer wants
    it stops being billed the monthly rental from here on.
    """
    url = f"{BASE_URL}/Account/{subaccount.auth_id}/Number/{number}/"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.delete(
            url, auth=httpx.BasicAuth(subaccount.auth_id, subaccount.auth_token)
        )

    if res.status_code not in (200, 204):
        logger.warning("plivo number release failed: %s %s", res.status_code, res.text)
        raise PlivoError(f"Could not release {number}")

"""
Plivo's KYC for a reseller: one compliance application per end-customer.

Plivo's own support confirmed the model directly, after an early test here
looked like a subaccount inherited Krova's own KYC - it hadn't; Plivo had
just defaulted to the most recently accepted application because none was
specified. For a real reseller, each business Krova onboards needs its own
EndUser, its own uploaded documents, and its own ComplianceApplication,
referenced explicitly by id when the number is bought.

EndUser and every Compliance* resource live under Krova's PARENT account,
not the business's subaccount - confirmed against a real call: Plivo's
ComplianceRequirement response for a subaccount search still returns a URI
rooted at the parent account. Krova, as the reseller, is the one submitting
compliance on each business's behalf.
"""

from dataclasses import dataclass, field
from pathlib import Path

import httpx

from shared.channels.voice.plivo_client import BASE_URL, TIMEOUT, PlivoError, parent_auth
from shared.config.settings import settings
from shared.db.models import VoiceProvisioning, VoiceProvisioningStatus
from shared.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class DocumentType:
    document_type_id: str
    name: str  # e.g. "Registration Certificate"


@dataclass(slots=True)
class Requirement:
    requirement_id: str
    document_types: list[DocumentType] = field(default_factory=list)


async def get_requirement(
    *, country_iso2: str = "IN", number_type: str = "local", end_user_type: str = "business"
) -> Requirement:
    """
    What documents Plivo needs before a number of this kind can be bought.

    `operation_type=buy_number` is required and undocumented in the prose
    docs - omitting it returns a 400 complaining about a completely
    different missing parameter. Found by reading the field back off a real
    number search result, which embeds the requirement id directly.
    """
    url = f"{BASE_URL}/Account/{settings.plivo_auth_id}/ComplianceRequirement/"
    params = {
        "country_iso2": country_iso2,
        "number_type": number_type,
        "end_user_type": end_user_type,
        "operation_type": "buy_number",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.get(url, auth=parent_auth(), params=params)

    if res.status_code != 200:
        logger.warning("plivo compliance requirement lookup failed: %s %s", res.status_code, res.text)
        raise PlivoError("Could not look up Plivo's KYC requirements")

    body = res.json()
    doc_types = [
        DocumentType(document_type_id=d["document_type_id"], name=d["document_type_name"])
        for group in body.get("acceptable_document_types", [])
        for d in group.get("acceptable_documents", [])
    ]
    return Requirement(requirement_id=body["compliance_requirement_id"], document_types=doc_types)


async def create_end_user(*, business_name: str, end_user_type: str = "business") -> str:
    """One EndUser per business Krova onboards - returns Plivo's end_user_id."""
    url = f"{BASE_URL}/Account/{settings.plivo_auth_id}/EndUser/"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            url, auth=parent_auth(), json={"name": business_name, "end_user_type": end_user_type}
        )

    if res.status_code not in (200, 201):
        logger.warning("plivo end_user create failed: %s %s", res.status_code, res.text)
        raise PlivoError("Could not register this business with Plivo for KYC")

    return res.json()["end_user_id"]


async def upload_document(
    *,
    end_user_id: str,
    document_type_id: str,
    file_path: str,
    alias: str,
    extra_fields: dict[str, str] | None = None,
) -> str:
    """
    Upload one KYC document.

    Some document types need more than the file itself - a Registration
    Certificate rejects the upload with "business_name not provided" unless
    it is passed as an extra form field. Which fields a given document type
    needs is not documented; the caller has to supply what Plivo's error
    message asks for the first time it is tried.
    """
    url = f"{BASE_URL}/Account/{settings.plivo_auth_id}/ComplianceDocument/"
    data = {
        "end_user_id": end_user_id,
        "document_type_id": document_type_id,
        "alias": alias,
        **(extra_fields or {}),
    }
    path = Path(file_path)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        with path.open("rb") as fh:
            res = await client.post(
                url, auth=parent_auth(), data=data, files={"file": (path.name, fh)}
            )

    if res.status_code not in (200, 201):
        logger.warning("plivo document upload failed: %s %s", res.status_code, res.text)
        raise PlivoError(f"Could not upload {alias}")

    return res.json()["document_id"]


async def create_application(
    *,
    requirement_id: str,
    end_user_id: str,
    document_ids: list[str],
    alias: str,
    end_user_type: str = "business",
    country_iso2: str = "IN",
    number_type: str = "local",
    callback_url: str | None = None,
) -> str:
    """
    Bundle a business's EndUser and documents into one application, unsubmitted.

    `callback_url` is meant to get Plivo's decision pushed to us the moment
    it happens, rather than a business waiting on us to poll - accepted
    without error on create, but never actually observed firing on a real,
    fast (automated) rejection in testing. GET .../compliance/status polling
    is the only confirmed-working path right now; this is sent in case it
    starts working, or fires only for slower human-reviewed decisions this
    session never triggered. Plivo documents this as
    an optional field on create, signed the same way every other Plivo
    webhook is.
    """
    url = f"{BASE_URL}/Account/{settings.plivo_auth_id}/ComplianceApplication/"
    payload = {
        "compliance_requirement_id": requirement_id,
        "end_user_id": end_user_id,
        "document_ids": document_ids,
        "alias": alias,
        "end_user_type": end_user_type,
        "country_iso2": country_iso2,
        "number_type": number_type,
    }
    if callback_url:
        payload["callback_url"] = callback_url

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(url, auth=parent_auth(), json=payload)

    if res.status_code not in (200, 201):
        logger.warning("plivo compliance application create failed: %s %s", res.status_code, res.text)
        raise PlivoError("Could not create the KYC application")

    return res.json()["compliance_application_id"]


async def update_application(application_id: str, *, document_ids: list[str]) -> None:
    """
    Replace an application's documents - the resubmission path.

    Used after a rejection: a business uploads corrected documents, this
    swaps them onto the existing application, and submit_application sends
    it back for review rather than starting over with a new application.
    """
    url = f"{BASE_URL}/Account/{settings.plivo_auth_id}/ComplianceApplication/{application_id}/"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(url, auth=parent_auth(), json={"document_ids": document_ids})

    if res.status_code not in (200, 201, 202):
        logger.warning("plivo compliance application update failed: %s %s", res.status_code, res.text)
        raise PlivoError("Could not update the KYC application with new documents")


async def submit_application(application_id: str) -> None:
    """Send a built application to Plivo for review. Cannot be un-submitted."""
    url = f"{BASE_URL}/Account/{settings.plivo_auth_id}/ComplianceApplication/{application_id}/Submit"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Plivo rejects a truly bodyless POST here ("use 'application/json'
        # Content-Type and raw POST with json data") even though there is
        # nothing to send - an empty JSON body satisfies it.
        res = await client.post(url, auth=parent_auth(), json={})

    if res.status_code not in (200, 201, 202):
        logger.warning("plivo compliance application submit failed: %s %s", res.status_code, res.text)
        raise PlivoError("Could not submit the KYC application")


def apply_status(row: VoiceProvisioning, raw_status: str, rejection_reason: str | None) -> None:
    """
    Write a Plivo compliance decision onto a VoiceProvisioning row.

    Plivo's own vocabulary is "accepted", not "approved" - confirmed via
    Plivo's docs (draft/submitted/accepted/rejected/suspended/expired).
    Shared between the poll endpoint and the push webhook so both apply the
    exact same mapping, whichever happens to observe the decision first.
    """
    row.compliance_raw_status = raw_status
    if raw_status.lower() == "accepted":
        row.status = VoiceProvisioningStatus.compliance_approved
    elif raw_status.lower() == "rejected":
        row.status = VoiceProvisioningStatus.compliance_rejected
        row.compliance_rejection_reason = rejection_reason


async def get_application_status(application_id: str) -> dict:
    """Plivo's own status/rejection-reason vocabulary, returned as-is."""
    url = f"{BASE_URL}/Account/{settings.plivo_auth_id}/ComplianceApplication/{application_id}/"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.get(url, auth=parent_auth())

    if res.status_code != 200:
        logger.warning("plivo compliance application status failed: %s %s", res.status_code, res.text)
        raise PlivoError("Could not check the KYC application's status")

    return res.json()

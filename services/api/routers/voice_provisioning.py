"""
A business's path from "nothing" to "a working Plivo voice number", plus
its call log once that number is live.

Five provisioning stages, mirroring how WhatsApp migration is split into
steps rather than one call: subaccount, KYC (requirement -> end-user ->
documents -> application -> submit), then number search and purchase. Each
stage is its own endpoint because KYC review happens on Plivo's side and can
take real time - a business might create its subaccount today and only be
able to buy a number once Plivo approves its application days later.
"""

import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.auth.encryption import decrypt, encrypt
from shared.channels.voice import compliance, plivo_client
from shared.channels.voice.plivo_client import PlivoError, Subaccount
from shared.config.settings import settings
from shared.db.models import (
    Business,
    Call,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Customer,
    CustomerIdentity,
    IdentityKind,
    VoiceProvisioning,
    VoiceProvisioningStatus,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/voice-onboarding", tags=["voice-onboarding"])


async def _require_provisioning(business_id: uuid.UUID, db: DbDep) -> VoiceProvisioning:
    result = await db.execute(
        select(VoiceProvisioning).where(VoiceProvisioning.business_id == business_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create a voice subaccount first: POST /voice-onboarding/subaccount",
        )
    return row


def _subaccount_of(row: VoiceProvisioning) -> Subaccount:
    return Subaccount(auth_id=row.subaccount_auth_id, auth_token=decrypt(row.subaccount_auth_token))


def _fail(exc: PlivoError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


# ── subaccount ────────────────────────────────────────────────────────────

class SubaccountOut(BaseModel):
    subaccount_auth_id: str
    status: str


@router.post("/subaccount", response_model=SubaccountOut, status_code=status.HTTP_201_CREATED)
async def create_subaccount(current_user: CurrentUserDep, db: DbDep) -> SubaccountOut:
    """
    A business's own slice of Plivo. Idempotent: calling this twice returns
    the existing subaccount rather than creating a second one.
    """
    existing = await db.execute(
        select(VoiceProvisioning).where(VoiceProvisioning.business_id == current_user.business)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return SubaccountOut(subaccount_auth_id=row.subaccount_auth_id, status=row.status.value)

    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")

    try:
        sub = await plivo_client.create_subaccount(f"krova-{business.name}-{business.id.hex[:8]}")
    except PlivoError as exc:
        raise _fail(exc) from exc

    row = VoiceProvisioning(
        business_id=current_user.business,
        subaccount_auth_id=sub.auth_id,
        subaccount_auth_token=encrypt(sub.auth_token),
        status=VoiceProvisioningStatus.subaccount_created,
    )
    db.add(row)
    await db.commit()

    logger.info("voice subaccount created business=%s auth_id=%s", current_user.business, sub.auth_id)
    return SubaccountOut(subaccount_auth_id=sub.auth_id, status=row.status.value)


# ── KYC ──────────────────────────────────────────────────────────────────

class RequirementOut(BaseModel):
    requirement_id: str
    document_types: list[dict]


@router.get("/compliance/requirements", response_model=RequirementOut)
async def get_requirements(
    current_user: CurrentUserDep,
    db: DbDep,
    country_iso2: str = "IN",
    number_type: str = "local",
) -> RequirementOut:
    """What documents this business needs to upload before Krova can buy it a number."""
    await _require_provisioning(current_user.business, db)
    try:
        req = await compliance.get_requirement(country_iso2=country_iso2, number_type=number_type)
    except PlivoError as exc:
        raise _fail(exc) from exc
    return RequirementOut(
        requirement_id=req.requirement_id,
        document_types=[{"id": d.document_type_id, "name": d.name} for d in req.document_types],
    )


class EndUserOut(BaseModel):
    end_user_id: str


@router.post("/compliance/end-user", response_model=EndUserOut)
async def create_end_user(current_user: CurrentUserDep, db: DbDep) -> EndUserOut:
    """Register this business as a Plivo KYC identity - a prerequisite for documents."""
    row = await _require_provisioning(current_user.business, db)
    if row.end_user_id:
        return EndUserOut(end_user_id=row.end_user_id)

    business = await db.get(Business, current_user.business)
    if business is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")

    try:
        end_user_id = await compliance.create_end_user(business_name=business.name)
    except PlivoError as exc:
        raise _fail(exc) from exc

    row.end_user_id = end_user_id
    await db.commit()
    return EndUserOut(end_user_id=end_user_id)


class DocumentOut(BaseModel):
    document_id: str


@router.post("/compliance/documents", response_model=DocumentOut)
async def upload_document(
    current_user: CurrentUserDep,
    db: DbDep,
    document_type_id: str = Form(...),
    alias: str = Form(...),
    business_name: str | None = Form(default=None),
    file: UploadFile = File(...),
) -> DocumentOut:
    """
    One KYC document. `business_name` is only needed for document types that
    ask for it (Registration Certificate does; not every type will) - Plivo's
    error message names the missing field the first time a type is tried.
    """
    row = await _require_provisioning(current_user.business, db)
    if not row.end_user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create an end-user first: POST /voice-onboarding/compliance/end-user",
        )

    suffix = Path(file.filename or "").suffix or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        document_id = await compliance.upload_document(
            end_user_id=row.end_user_id,
            document_type_id=document_type_id,
            file_path=tmp_path,
            alias=alias,
            extra_fields={"business_name": business_name} if business_name else None,
        )
    except PlivoError as exc:
        raise _fail(exc) from exc
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    # Replace, not append: re-uploading a corrected document of a type
    # already on file should swap it, not leave both attached to the next
    # application - Plivo accepted two "Registration Certificate" entries
    # without complaint in testing, but sending exactly one per required
    # type is what a real resubmission is supposed to mean.
    row.documents = [
        *(d for d in row.documents if d["document_type_id"] != document_type_id),
        {"document_id": document_id, "document_type_id": document_type_id, "alias": alias},
    ]
    await db.commit()
    return DocumentOut(document_id=document_id)


class ApplicationIn(BaseModel):
    requirement_id: str
    country_iso2: str = "IN"
    number_type: str = "local"


class ApplicationOut(BaseModel):
    application_id: str
    status: str
    rejection_reason: str | None = None


@router.post("/compliance/application", response_model=ApplicationOut)
async def submit_application(body: ApplicationIn, current_user: CurrentUserDep, db: DbDep) -> ApplicationOut:
    """
    Bundle every uploaded document into an application and submit it.

    Submission is one-way - Plivo does not support un-submitting - so this
    is deliberately its own step after documents, not folded into upload.
    """
    row = await _require_provisioning(current_user.business, db)
    if not row.end_user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No end-user registered yet")
    if not row.documents:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No documents uploaded yet")

    business = await db.get(Business, current_user.business)

    try:
        application_id = await compliance.create_application(
            requirement_id=body.requirement_id,
            end_user_id=row.end_user_id,
            document_ids=[d["document_id"] for d in row.documents],
            alias=f"krova-{business.name if business else current_user.business}",
            country_iso2=body.country_iso2,
            number_type=body.number_type,
            callback_url=f"{settings.public_base_url.rstrip('/')}/voice/compliance-status",
        )
        await compliance.submit_application(application_id)
    except PlivoError as exc:
        raise _fail(exc) from exc

    row.compliance_requirement_id = body.requirement_id
    row.compliance_application_id = application_id
    row.status = VoiceProvisioningStatus.compliance_submitted
    await db.commit()

    logger.info(
        "voice compliance submitted business=%s application=%s",
        current_user.business,
        application_id,
    )
    return ApplicationOut(application_id=application_id, status=row.status.value)


@router.get("/compliance/status", response_model=ApplicationOut)
async def compliance_status(current_user: CurrentUserDep, db: DbDep) -> ApplicationOut:
    """
    Poll Plivo for a decision and sync it into our own record.

    The proven path: `/voice/compliance-status` exists to receive Plivo's
    push, and both apply the same mapping, but the push was never observed
    actually firing in testing even with callback_url set on the
    application. This is what a UI should actually rely on until that is
    confirmed working.
    """
    row = await _require_provisioning(current_user.business, db)
    if not row.compliance_application_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No application submitted yet")

    try:
        body = await compliance.get_application_status(row.compliance_application_id)
    except PlivoError as exc:
        raise _fail(exc) from exc

    compliance.apply_status(row, body.get("status", ""), body.get("rejection_reason") or body.get("reason"))
    await db.commit()

    return ApplicationOut(
        application_id=row.compliance_application_id,
        status=row.status.value,
        rejection_reason=row.compliance_rejection_reason,
    )


@router.post("/compliance/resubmit", response_model=ApplicationOut)
async def resubmit_application(current_user: CurrentUserDep, db: DbDep) -> ApplicationOut:
    """
    After a rejection: swap in whatever documents have been uploaded since
    (via POST /compliance/documents again) and send the same application
    back for review, rather than starting a fresh one from scratch.
    """
    row = await _require_provisioning(current_user.business, db)
    if row.status != VoiceProvisioningStatus.compliance_rejected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Nothing to resubmit (status: {row.status.value})",
        )
    if not row.compliance_application_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No application to resubmit")

    try:
        await compliance.update_application(
            row.compliance_application_id,
            document_ids=[d["document_id"] for d in row.documents],
        )
        await compliance.submit_application(row.compliance_application_id)
    except PlivoError as exc:
        raise _fail(exc) from exc

    row.status = VoiceProvisioningStatus.compliance_submitted
    row.compliance_rejection_reason = None
    await db.commit()

    logger.info(
        "voice compliance resubmitted business=%s application=%s",
        current_user.business,
        row.compliance_application_id,
    )
    return ApplicationOut(application_id=row.compliance_application_id, status=row.status.value)


# ── numbers ──────────────────────────────────────────────────────────────

@router.get("/numbers/search")
async def search_numbers(
    current_user: CurrentUserDep,
    db: DbDep,
    country_iso: str = "IN",
    number_type: str = "local",
    pattern: str | None = None,
) -> list[dict]:
    """Numbers this business could buy, searched under its own subaccount."""
    row = await _require_provisioning(current_user.business, db)
    try:
        results = await plivo_client.search_numbers(
            _subaccount_of(row), country_iso=country_iso, number_type=number_type, pattern=pattern
        )
    except PlivoError as exc:
        raise _fail(exc) from exc

    return [
        {
            "number": n["number"],
            "city": n.get("city"),
            "region": n.get("region"),
            "monthly_rental_rate": n.get("monthly_rental_rate"),
            "voice_rate": n.get("voice_rate"),
        }
        for n in results
    ]


class BuyIn(BaseModel):
    number: str


class BuyOut(BaseModel):
    number: str
    connection_id: str


@router.post("/numbers/buy", response_model=BuyOut, status_code=status.HTTP_201_CREATED)
async def buy_number(body: BuyIn, current_user: CurrentUserDep, db: DbDep) -> BuyOut:
    """
    Buy a number, wire it to Krova's voice pipeline, and connect it.

    Requires an approved compliance application - Plivo enforces this on its
    side, so a business that tries before approval gets Plivo's own error
    rather than a check duplicated here.
    """
    row = await _require_provisioning(current_user.business, db)
    if row.status != VoiceProvisioningStatus.compliance_approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"KYC is not approved yet (status: {row.status.value})",
        )

    sub = _subaccount_of(row)
    try:
        await plivo_client.buy_number(
            body.number, sub, compliance_application_id=row.compliance_application_id
        )
        app_id = await plivo_client.create_application(
            sub,
            app_name=f"krova-{body.number}",
            answer_url=f"{settings.public_base_url.rstrip('/')}/voice/answer",
            hangup_url=f"{settings.public_base_url.rstrip('/')}/voice/status",
        )
        await plivo_client.link_number(sub, body.number, app_id)
        number_info = await plivo_client.get_number(sub, body.number)
    except PlivoError as exc:
        raise _fail(exc) from exc

    connection = ChannelConnection(
        business_id=current_user.business,
        channel=Channel.voice,
        external_account_id=body.number,
        status=ConnectionStatus.active,
        access_token=encrypt(sub.auth_token),
        extra={
            "subaccount_auth_id": sub.auth_id,
            "app_id": app_id,
            # Rupees per minute, from Plivo's own record of the number -
            # what per-call cost tracking multiplies duration against.
            "voice_rate": number_info.get("voice_rate"),
        },
    )
    db.add(connection)
    await db.commit()

    logger.info(
        "voice number bought business=%s number=%s connection=%s",
        current_user.business,
        body.number,
        connection.id,
    )
    return BuyOut(number=body.number, connection_id=str(connection.id))


@router.post("/numbers/{number}/release", status_code=status.HTTP_204_NO_CONTENT)
async def release_number(number: str, current_user: CurrentUserDep, db: DbDep) -> None:
    """
    Give a number back - a business that churns stops being billed the
    monthly rental from here on. The connection is marked disconnected
    rather than deleted, so its call history stays on the customer timeline.
    """
    row = await _require_provisioning(current_user.business, db)

    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == current_user.business,
            ChannelConnection.channel == Channel.voice,
            ChannelConnection.external_account_id == number,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such connected number")

    try:
        await plivo_client.release_number(_subaccount_of(row), number)
    except PlivoError as exc:
        raise _fail(exc) from exc

    connection.status = ConnectionStatus.disconnected
    await db.commit()

    logger.info("voice number released business=%s number=%s", current_user.business, number)


# ── call log ─────────────────────────────────────────────────────────────

def _money(paise: int) -> str:
    return f"₹{paise / 100:,.0f}"


class CallLogOut(BaseModel):
    id: str
    customer_id: str | None
    customer_name: str | None
    customer_phone: str | None
    direction: str
    duration_seconds: int | None
    cost_paise: int
    cost_display: str
    cost_breakdown: dict
    hangup_cause: str | None
    started_at: str


@router.get("/logs", response_model=list[CallLogOut])
async def call_logs(
    current_user: CurrentUserDep,
    db: DbDep,
    limit: int = Query(default=100, le=500),
) -> list[CallLogOut]:
    """
    Every call this business's number has handled, most recent first - what
    actually happened is worth more here than what Plivo says the account
    can do, since duration and cost are what the owner is actually asking.
    """
    rows = (
        await db.execute(
            select(Call)
            .where(Call.business_id == current_user.business)
            .order_by(Call.started_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    customer_ids = {c.customer_id for c in rows if c.customer_id}
    names: dict[uuid.UUID, str | None] = {}
    phones: dict[uuid.UUID, str] = {}
    if customer_ids:
        cust_rows = await db.execute(select(Customer).where(Customer.id.in_(customer_ids)))
        names = {c.id: c.display_name for c in cust_rows.scalars().all()}

        id_rows = await db.execute(
            select(CustomerIdentity).where(
                CustomerIdentity.customer_id.in_(customer_ids),
                CustomerIdentity.kind == IdentityKind.phone,
            )
        )
        for identity in id_rows.scalars().all():
            phones.setdefault(identity.customer_id, identity.value)

    return [
        CallLogOut(
            id=str(c.id),
            customer_id=str(c.customer_id) if c.customer_id else None,
            customer_name=names.get(c.customer_id) if c.customer_id else None,
            customer_phone=phones.get(c.customer_id) if c.customer_id else None,
            direction=c.direction.value,
            duration_seconds=c.duration_seconds,
            cost_paise=c.cost_paise,
            cost_display=_money(c.cost_paise),
            cost_breakdown=c.cost_breakdown or {},
            hangup_cause=c.hangup_cause,
            started_at=c.started_at.isoformat(),
        )
        for c in rows
    ]


# ── agent speech & greeting ──────────────────────────────────────────────
#
# What shared/channels/voice/tenant.py's resolve() reads back on every real
# call - greeting, language, language_mode and speaker were already read
# from ChannelConnection.extra with sane defaults, but nothing ever wrote a
# non-default value and no endpoint existed to change them. These two
# endpoints are that missing write path.

# Sarvam bulbul:v3's real, published speaker catalogue - never invented.
MALE_SPEAKERS = [
    "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "varun",
    "manan", "sumit", "kabir", "aayan", "ashutosh", "advait", "anand",
    "tarun", "sunny", "mani", "gokul", "vijay", "mohit", "rehan", "soham",
]
FEMALE_SPEAKERS = [
    "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita", "shreya",
    "roopa", "tanya", "shruti", "suhani", "kavitha", "rupali",
]
VALID_SPEAKERS = set(MALE_SPEAKERS) | set(FEMALE_SPEAKERS)
VALID_LANGUAGE_MODES = {"adaptive", "fixed"}
# Scoped to what a business can actually pick for `language` - Sarvam
# supports more Indian languages, but adaptive mode already auto-detects
# whatever a caller speaks; a fixed choice is only meaningful for the two
# this product is actually built for.
VALID_LANGUAGES = {"en-IN", "hi-IN"}


async def _voice_connection(business_id: uuid.UUID, db: DbDep) -> ChannelConnection:
    result = await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.voice,
            ChannelConnection.status == ConnectionStatus.active,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect a voice number first: buy one under Phone Numbers",
        )
    return connection


class AgentSettingsOut(BaseModel):
    greeting: str
    language: str
    language_mode: str
    speaker: str
    male_speakers: list[str]
    female_speakers: list[str]


def _agent_settings_out(connection: ChannelConnection, business_name: str) -> AgentSettingsOut:
    extra = connection.extra or {}
    return AgentSettingsOut(
        greeting=extra.get("greeting")
        or f"Hello, thank you for calling {business_name}. How can I help you?",
        language=extra.get("language", "en-IN"),
        language_mode=extra.get("language_mode", "adaptive"),
        speaker=extra.get("speaker", "shubh"),
        male_speakers=MALE_SPEAKERS,
        female_speakers=FEMALE_SPEAKERS,
    )


@router.get("/agent-settings", response_model=AgentSettingsOut)
async def get_agent_settings(current_user: CurrentUserDep, db: DbDep) -> AgentSettingsOut:
    connection = await _voice_connection(current_user.business, db)
    business = await db.get(Business, current_user.business)
    return _agent_settings_out(connection, business.name if business else "your business")


class AgentSettingsIn(BaseModel):
    greeting: str | None = None
    language: str | None = None
    language_mode: str | None = None
    speaker: str | None = None


@router.patch("/agent-settings", response_model=AgentSettingsOut)
async def update_agent_settings(
    body: AgentSettingsIn, current_user: CurrentUserDep, db: DbDep
) -> AgentSettingsOut:
    """
    Every field is optional and only what's sent gets changed - a business
    picking just a voice shouldn't have to resend its own greeting text to
    avoid losing it.
    """
    connection = await _voice_connection(current_user.business, db)

    if body.language is not None and body.language not in VALID_LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"language must be one of {sorted(VALID_LANGUAGES)}",
        )
    if body.language_mode is not None and body.language_mode not in VALID_LANGUAGE_MODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"language_mode must be one of {sorted(VALID_LANGUAGE_MODES)}",
        )
    if body.speaker is not None and body.speaker not in VALID_SPEAKERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unrecognised speaker - see male_speakers/female_speakers on GET for the real list",
        )

    extra = dict(connection.extra or {})
    if body.greeting is not None:
        extra["greeting"] = body.greeting.strip()
    if body.language is not None:
        extra["language"] = body.language
    if body.language_mode is not None:
        extra["language_mode"] = body.language_mode
    if body.speaker is not None:
        extra["speaker"] = body.speaker
    connection.extra = extra
    await db.commit()

    logger.info(
        "voice agent settings updated business=%s fields=%s",
        current_user.business,
        [k for k, v in body.model_dump().items() if v is not None],
    )

    business = await db.get(Business, current_user.business)
    return _agent_settings_out(connection, business.name if business else "your business")

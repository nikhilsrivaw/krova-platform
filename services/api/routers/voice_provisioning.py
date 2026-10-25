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
from sqlalchemy import func, or_, select

from services.api.dependencies import OwnerOrAdminDep, CurrentUserDep, DbDep
from shared.auth.encryption import decrypt, encrypt
from shared.billing import plans
from shared.channels.voice import compliance, keypad_menu, plivo_client, sarvam
from shared.channels.voice.plivo_client import PlivoError, Subaccount
from shared.channels.voice import numbers as voice_numbers
from shared.channels.voice.tenant import (
    FEMALE_SPEAKERS,
    MALE_SPEAKERS,
    default_greeting,
)
from shared.config.settings import settings
from shared.identity.normalise import InvalidIdentifier, normalise_phone
from shared.db.models import (
    Business,
    Call,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Customer,
    CustomerIdentity,
    IdentityKind,
    NumberRequest,
    NumberRequestStatus,
    NumberRequestType,
    UsageEvent,
    VoiceProvisioning,
    VoiceProvisioningStatus,
)
from shared.team import access
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
async def create_subaccount(current_user: OwnerOrAdminDep, db: DbDep) -> SubaccountOut:
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

    from shared.billing.guards import require_active_plan

    # A channel is set up by a business with a plan: no plan yet, no WhatsApp / Instagram / voice line.
    await require_active_plan(db, current_user.business)

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

class ComplianceStateOut(BaseModel):
    subaccount_auth_id: str
    has_end_user: bool
    uploaded_document_type_ids: list[str]


@router.get("/compliance/state", response_model=ComplianceStateOut)
async def compliance_state(current_user: OwnerOrAdminDep, db: DbDep) -> ComplianceStateOut:
    """
    Everything the KYC wizard needs to redraw itself correctly after a page
    reload, before any application has been submitted (applicationStatus
    only has something to report once that's true - see this router's own
    docstring on why this is split into stages).

    Without this, the wizard's own React state - which document types were
    already uploaded, whether an end-user was already registered - only
    ever existed in memory for the current tab. Reloading mid-flow made the
    wizard forget a document was already on file, and retrying it hit
    Plivo's alias-uniqueness rule with a confusing error instead of just
    showing the step as already done.
    """
    row = await _require_provisioning(current_user.business, db)
    return ComplianceStateOut(
        subaccount_auth_id=row.subaccount_auth_id,
        has_end_user=row.end_user_id is not None,
        uploaded_document_type_ids=[d["document_type_id"] for d in row.documents],
    )


class RequirementOut(BaseModel):
    requirement_id: str
    document_types: list[dict]
    # Each inner list is one "pick any one" group - see compliance.Requirement.groups.
    groups: list[list[dict]] = []


@router.get("/compliance/requirements", response_model=RequirementOut)
async def get_requirements(
    current_user: OwnerOrAdminDep,
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
        groups=[[{"id": d.document_type_id, "name": d.name} for d in g] for g in req.groups],
    )


class EndUserOut(BaseModel):
    end_user_id: str


@router.post("/compliance/end-user", response_model=EndUserOut)
async def create_end_user(current_user: OwnerOrAdminDep, db: DbDep) -> EndUserOut:
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
    current_user: OwnerOrAdminDep,
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
async def submit_application(body: ApplicationIn, current_user: OwnerOrAdminDep, db: DbDep) -> ApplicationOut:
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
async def compliance_status(current_user: OwnerOrAdminDep, db: DbDep) -> ApplicationOut:
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
async def resubmit_application(current_user: OwnerOrAdminDep, db: DbDep) -> ApplicationOut:
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

class OwnedNumberOut(BaseModel):
    number: str
    city: str | None = None
    region: str | None = None
    is_connected: bool


@router.get("/numbers/owned", response_model=list[OwnedNumberOut])
async def list_owned_numbers(current_user: OwnerOrAdminDep, db: DbDep) -> list[OwnedNumberOut]:
    """
    Every number Plivo says this subaccount owns, cross-checked against
    Krova's own ChannelConnection rows - see plivo_client.list_owned_numbers's
    own docstring for why this exists: a number bought on Plivo's side but
    never successfully linked is real, billed inventory invisible to
    everything else in Krova. is_connected marks the ones already wired to
    voice; anything false here is a genuine candidate to release.
    """
    row = await _require_provisioning(current_user.business, db)
    try:
        owned = await plivo_client.list_owned_numbers(_subaccount_of(row))
    except PlivoError as exc:
        raise _fail(exc) from exc

    connected = {
        c.external_account_id
        for c in (
            await db.execute(
                select(ChannelConnection).where(
                    ChannelConnection.business_id == current_user.business,
                    ChannelConnection.channel == Channel.voice,
                    ChannelConnection.status == ConnectionStatus.active,
                )
            )
        ).scalars().all()
    }
    return [
        OwnedNumberOut(
            number=n["number"],
            city=n.get("city"),
            region=n.get("region"),
            is_connected=n["number"] in connected,
        )
        for n in owned
    ]


@router.get("/numbers/search")
async def search_numbers(
    current_user: OwnerOrAdminDep,
    db: DbDep,
    country_iso: str = "IN",
    number_type: str = "local",
    pattern: str | None = None,
) -> list[dict]:
    """
    Numbers this business could buy, searched under its own subaccount.

    Plivo's own numbers carry no leading zero on the STD code (Bengaluru is
    "8013802883" under +91, not "0801..." - confirmed against a real
    connected number, +91 80 3180 2883). Every Indian caller writes an STD
    code with the leading zero out of habit ("080 Bengaluru"), which the
    search UI's own placeholder text encourages - so strip exactly one
    leading zero here rather than silently returning nothing for the
    format anyone will actually type.
    """
    if pattern and pattern.startswith("0"):
        pattern = pattern[1:]

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
async def buy_number(body: BuyIn, current_user: OwnerOrAdminDep, db: DbDep) -> BuyOut:
    """
    Buy a number, wire it to Krova's voice pipeline, and connect it.

    Requires an approved compliance application - Plivo enforces this on its
    side, so a business that tries before approval gets Plivo's own error
    rather than a check duplicated here.
    """
    from shared.billing.guards import require_active_plan

    # A number costs real rent: no plan, no number.
    await require_active_plan(db, current_user.business)

    from shared.billing import limits as plan_limits

    business = await db.get(Business, current_user.business)
    plan_limits.check_count(
        business.plan if business else None, "phone_lines", "phone line(s)",
        await plan_limits.count_rows(
            db, ChannelConnection,
            ChannelConnection.business_id == current_user.business,
            ChannelConnection.channel == Channel.voice,
            ChannelConnection.status == ConnectionStatus.active,
        ),
    )

    row = await _require_provisioning(current_user.business, db)
    if row.status != VoiceProvisioningStatus.compliance_approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"KYC is not approved yet (status: {row.status.value})",
        )

    # The number costs KROVA rent every month, so the first month is taken from the
    # wallet before it is bought - no money in the wallet, no number.
    from shared.billing import plans as billing_plans, wallet as billing_wallet

    if (await billing_wallet.balance(db, current_user.business) or 0) < billing_plans.NUMBER_RENT_PAISE:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "wallet_empty",
                "message": f"Add at least Rs {billing_plans.NUMBER_RENT_PAISE // 100} to your wallet first - that is one month of rent for the number.",
            },
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
    await billing_wallet.charge_number_rent(db, business_id=current_user.business, number=body.number)
    await db.commit()

    logger.info(
        "voice number bought business=%s number=%s connection=%s",
        current_user.business,
        body.number,
        connection.id,
    )
    return BuyOut(number=body.number, connection_id=str(connection.id))


class ReleaseCheckOut(BaseModel):
    campaigns: list[dict]
    rules: list[dict]
    is_default: bool
    # Numbers the campaigns could move to: still connected and allowed to call out.
    others: list[dict]
    rent_paise: int


async def _connection_by_number(db, business_id: uuid.UUID, number: str) -> ChannelConnection | None:
    rows = (await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.business_id == business_id,
            ChannelConnection.channel == Channel.voice,
            ChannelConnection.external_account_id == number,
        ).order_by(ChannelConnection.created_at.desc())
    )).scalars().all()
    # A number bought again after a release has an old disconnected row too: the live one is the one that matters.
    return next((c for c in rows if c.status == ConnectionStatus.active), rows[0] if rows else None)


@router.get("/numbers/{number}/release-check", response_model=ReleaseCheckOut)
async def release_check(number: str, current_user: OwnerOrAdminDep, db: DbDep) -> ReleaseCheckOut:
    """What still points at this number - shown before it is given back, so nothing is left calling from nowhere."""
    from shared.channels.voice import number_release

    connection = await _connection_by_number(db, current_user.business, number)
    if connection is None or connection.status != ConnectionStatus.active:
        return ReleaseCheckOut(campaigns=[], rules=[], is_default=False, others=[], rent_paise=plans.NUMBER_RENT_PAISE)
    found = await number_release.dependents(db, current_user.business, connection)
    return ReleaseCheckOut(
        campaigns=[{"id": str(c.id), "name": c.name, "status": c.status.value} for c in found.campaigns],
        rules=[{"id": str(r.id), "name": r.name or r.trigger_type} for r in found.rules],
        is_default=found.is_default,
        others=[
            {"connection_id": str(c.id), "number": c.external_account_id, "label": voice_numbers.label_of(c),
             "series": voice_numbers.series_of(c.external_account_id)}
            for c in found.others if voice_numbers.allows_outbound(c)
        ],
        rent_paise=plans.NUMBER_RENT_PAISE,
    )


class ReleaseIn(BaseModel):
    # Where campaigns that call from this number go. None: they stop pointing at a number (a sending one is paused).
    move_campaigns_to: uuid.UUID | None = None


@router.post("/numbers/{number}/release", status_code=status.HTTP_204_NO_CONTENT)
async def release_number(
    number: str, current_user: OwnerOrAdminDep, db: DbDep, body: ReleaseIn | None = None,
) -> None:
    """
    Give a number back - a business that churns stops being billed the
    monthly rental from here on. The connection is marked disconnected
    rather than deleted (not dropped from the DB), so its call history
    stays on the customer timeline.

    Works with or without a matching ChannelConnection row - a number that
    was bought on Plivo's side but never successfully linked (see
    list_owned_numbers above) has none, and must still be releasable.
    Plivo itself is the real authorization boundary here: releasing calls
    with this business's own subaccount credentials, so Plivo simply
    refuses a number that subaccount doesn't actually own.
    """
    from shared.channels.voice import number_release

    row = await _require_provisioning(current_user.business, db)
    connection = await _connection_by_number(db, current_user.business, number)

    move_to = None
    if body is not None and body.move_campaigns_to is not None:
        move_to = next(
            (c for c in await voice_numbers.active(db, current_user.business)
             if c.id == body.move_campaigns_to and (connection is None or c.id != connection.id)),
            None,
        )
        if move_to is None or not voice_numbers.allows_outbound(move_to):
            raise HTTPException(status_code=422, detail="Choose a number you still have that can make outgoing calls.")

    try:
        await plivo_client.release_number(_subaccount_of(row), number)
    except PlivoError as exc:
        raise _fail(exc) from exc

    settled = None
    if connection is not None and connection.status == ConnectionStatus.active:
        settled = await number_release.settle(db, current_user.business, connection, move_to=move_to)
        connection.status = ConnectionStatus.disconnected
    await db.commit()

    logger.info("voice number released business=%s number=%s settled=%s", current_user.business, number, settled)


class MyNumberOut(BaseModel):
    connection_id: str
    number: str
    label: str
    use: str
    default_outbound: bool
    series: str | None
    series_label: str | None
    rent_paise: int
    calls_30d: int
    cost_paise_30d: int


@router.get("/numbers/mine", response_model=list[MyNumberOut])
async def my_numbers(current_user: OwnerOrAdminDep, db: DbDep) -> list[MyNumberOut]:
    """Every connected number with its name, what it is for, its monthly rent and the last 30 days of calls."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from shared.billing import plans as billing_plans

    numbers = await voice_numbers.active(db, current_user.business)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    usage = (
        await db.execute(
            select(Call.connection_id, func.count(Call.id), func.coalesce(func.sum(Call.cost_paise), 0))
            .where(Call.business_id == current_user.business, Call.started_at >= since)
            .group_by(Call.connection_id)
        )
    ).all()
    by_connection = {cid: (int(n), int(cost)) for cid, n, cost in usage}
    default_id = getattr(voice_numbers.choose_outbound(numbers), "id", None)
    out = []
    for c in numbers:
        series = voice_numbers.series_of(c.external_account_id)
        calls, cost = by_connection.get(c.id, (0, 0))
        out.append(MyNumberOut(
            connection_id=str(c.id), number=c.external_account_id, label=voice_numbers.label_of(c),
            use=voice_numbers.use_of(c),
            # What outgoing calls will really use, even if the owner never ticked a default.
            default_outbound=voice_numbers.is_default_outbound(c) or (default_id == c.id and not any(
                voice_numbers.is_default_outbound(x) for x in numbers
            )),
            series=series, series_label=voice_numbers.SERIES_LABEL.get(series) if series else None,
            rent_paise=billing_plans.NUMBER_RENT_PAISE, calls_30d=calls, cost_paise_30d=cost,
        ))
    return out


# ── what each number is for, and who answers it ──────────────────────────

class PickupPerson(BaseModel):
    user_id: str | None = None
    phone: str | None = None
    name: str | None = None


class PickupLevel(BaseModel):
    mode: str = "one_by_one"
    ring_seconds: int = 18
    people: list[PickupPerson] = []


class PickupIO(BaseModel):
    levels: list[PickupLevel] = []
    max_wait: int = 60


class LineProfileIO(BaseModel):
    role: str | None = None
    role_note: str = ""
    pickup: PickupIO = PickupIO()
    no_answer: str = "callback"
    callback_hours: int = 2


class LineProfileOut(LineProfileIO):
    connection_id: str
    roles: list[dict]
    # The number's old single staff phone, shown as the list's first entry until the owner saves a list.
    legacy_staff_phone: str | None = None


def _profile_out(connection: ChannelConnection) -> LineProfileOut:
    from shared.channels.voice import line_profile

    profile = line_profile.read(connection.extra)
    return LineProfileOut(
        connection_id=str(connection.id), **profile,
        roles=[{"key": k, "label": v["label"], "note": v["note"]} for k, v in line_profile.ROLES.items()],
        legacy_staff_phone=None if profile["pickup"]["levels"] else (connection.extra or {}).get("staff_phone_number"),
    )


@router.get("/numbers/{connection_id}/profile", response_model=LineProfileOut)
async def get_line_profile(connection_id: uuid.UUID, current_user: OwnerOrAdminDep, db: DbDep) -> LineProfileOut:
    """What this number is for, who answers it (in levels), and what happens when nobody picks up."""
    return _profile_out(await _voice_connection(current_user.business, db, connection_id))


@router.put("/numbers/{connection_id}/profile", response_model=LineProfileOut)
async def put_line_profile(
    connection_id: uuid.UUID, body: LineProfileIO, current_user: OwnerOrAdminDep, db: DbDep,
) -> LineProfileOut:
    """Replace this number's profile. The screen always sends all of it, so what you see is what is stored."""
    from shared.channels.voice import line_profile
    from shared.db.models import BusinessMember

    connection = await _voice_connection(current_user.business, db, connection_id)
    member_ids = {
        str(uid) for uid in (await db.execute(
            select(BusinessMember.user_id).where(BusinessMember.business_id == current_user.business)
        )).scalars().all()
    }
    try:
        cleaned = line_profile.clean(body.model_dump(), member_ids=member_ids, normalise_phone=normalise_phone)
    except line_profile.ProfileError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    extra = {**(connection.extra or {}), **cleaned}
    if extra.get("copilot_mode") and not cleaned["pickup"]["levels"] and not extra.get("staff_phone_number"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Live Copilot is on for this number, so it needs at least one person to ring.",
        )
    if cleaned["pickup"]["levels"]:
        # The list replaces the old single staff phone; the screen showed it as the list's first entry.
        extra["staff_phone_number"] = None
    connection.extra = extra
    await db.commit()
    logger.info(
        "voice line profile saved business=%s number=%s role=%s levels=%s no_answer=%s",
        current_user.business, connection.external_account_id, cleaned["role"], len(cleaned["pickup"]["levels"]),
        cleaned["no_answer"],
    )
    return _profile_out(connection)


TEST_CALLS_PER_DAY = 5


class TestCallIn(BaseModel):
    phone: str


@router.post("/numbers/{connection_id}/test-call")
async def test_call(connection_id: uuid.UUID, body: TestCallIn, current_user: OwnerOrAdminDep, db: DbDep) -> dict:
    """
    Ring a phone from this number with a short recorded line, to check a "who answers" number really rings. Never
    the AI; a few a day, since KROVA pays the carrier for each.
    """
    from datetime import datetime, timezone

    connection = await _voice_connection(current_user.business, db, connection_id)
    try:
        to = normalise_phone(body.phone)
    except InvalidIdentifier as exc:
        raise HTTPException(status_code=422, detail=f"That is not a phone number ({exc}).") from exc
    business = await db.get(Business, current_user.business)
    settings_now = dict(business.settings or {})
    today = datetime.now(timezone.utc).date().isoformat()
    used = settings_now.get("line_test_calls") or {}
    count = used.get("count", 0) if used.get("day") == today else 0
    if count >= TEST_CALLS_PER_DAY:
        raise HTTPException(status_code=429, detail=f"{TEST_CALLS_PER_DAY} test calls a day - try again tomorrow.")
    auth_id = (connection.extra or {}).get("subaccount_auth_id")
    if not auth_id or not connection.access_token:
        raise HTTPException(status_code=409, detail="This number is not fully connected yet.")
    base = settings.public_base_url.rstrip("/")
    try:
        await plivo_client.make_call(
            auth_id=auth_id, auth_token=decrypt(connection.access_token),
            from_number=connection.external_account_id, to_number=to,
            answer_url=f"{base}/voice/line-test-answer?c={connection.id}",
            hangup_url=f"{base}/voice/line-test-hangup",
        )
    except PlivoError as exc:
        raise _fail(exc) from exc
    settings_now["line_test_calls"] = {"day": today, "count": count + 1}
    business.settings = settings_now
    await db.commit()
    logger.info("line test call placed business=%s from=%s", current_user.business, connection.external_account_id)
    return {"calling": to, "left_today": TEST_CALLS_PER_DAY - count - 1}


# ── call log ─────────────────────────────────────────────────────────────

def _money(paise: int) -> str:
    # Two decimals, not whole rupees: one call's total is routinely under a
    # rupee (STT + TTS + carrier are each tens of paise), and rounding it
    # away made a real, billed call read "₹0" - or "₹1" over a breakdown of
    # three "₹0"s, which is worse, because it looks like a bug in the sum.
    return f"₹{paise / 100:,.2f}"


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
    # What the wallet is charged for this call: the real cost (speech + carrier + the AI that ran the call),
    # plus any platform charge (none for now). cost_paise above is only the speech + carrier part.
    ai_paise: int = 0
    platform_paise: int = 0
    charged_paise: int = 0
    charged_display: str = ""
    # Which of the business's numbers this call used.
    number: str | None = None
    number_label: str | None = None
    hangup_cause: str | None
    started_at: str
    escalated: bool
    escalation_reason: str | None
    outcome: str | None
    sentiment: str | None
    topic: str | None
    summary: str | None


@router.get("/logs", response_model=list[CallLogOut])
async def call_logs(
    current_user: CurrentUserDep,
    db: DbDep,
    limit: int = Query(default=100, le=500),
    connection_id: uuid.UUID | None = Query(default=None),
) -> list[CallLogOut]:
    """
    Every call this business's number has handled, most recent first - what
    actually happened is worth more here than what Plivo says the account
    can do, since duration and cost are what the owner is actually asking.
    """
    conditions = [Call.business_id == current_user.business]
    if connection_id is not None:
        conditions.append(Call.connection_id == connection_id)
    # A team member sees calls with customers they may see, or from callers not yet known
    # (shared/team/access.py) - and, when the owner keeps money and numbers back, neither.
    only = await access.visible_ids(db, current_user)
    if only is not None:
        conditions.append(or_(Call.customer_id.is_(None), Call.customer_id.in_(only)))
    allowed = await access.permissions(db, current_user)
    rows = (
        await db.execute(
            select(Call).where(*conditions).order_by(Call.started_at.desc()).limit(limit)
        )
    ).scalars().all()
    connections = {
        c.id: c
        for c in (
            await db.execute(
                select(ChannelConnection).where(
                    ChannelConnection.business_id == current_user.business, ChannelConnection.channel == Channel.voice,
                )
            )
        ).scalars().all()
    }

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
        if not allowed["see_phones"]:
            phones = {k: access.mask(v) for k, v in phones.items()}

    # Every cost the wallet bills for a call is a voice usage event pointing at it, not just the
    # speech + carrier part stored on the call; sum them so the page shows what is really charged.
    billed: dict[uuid.UUID, int] = {}
    if rows:
        billed = {
            sid: int(total)
            for sid, total in (
                await db.execute(
                    select(UsageEvent.source_id, func.sum(UsageEvent.krova_cost_paise)).where(
                        UsageEvent.business_id == current_user.business,
                        UsageEvent.channel == "voice",
                        UsageEvent.source_type == "call",
                        UsageEvent.source_id.in_([c.id for c in rows]),
                    ).group_by(UsageEvent.source_id)
                )
            ).all()
        }

    if not allowed["see_money"]:
        billed = {}

    def _bill(c) -> tuple[int, int, int]:
        """(AI part, platform charge, total charged) in paise for one call."""
        if not allowed["see_money"]:
            return 0, 0, 0
        cost = max(billed.get(c.id, 0), c.cost_paise or 0)
        charged = plans.call_charge(cost) if cost > 0 else 0
        return cost - (c.cost_paise or 0), charged - cost, charged

    return [
        CallLogOut(
            id=str(c.id),
            ai_paise=_bill(c)[0],
            platform_paise=_bill(c)[1],
            charged_paise=_bill(c)[2],
            charged_display=_money(_bill(c)[2]),
            customer_id=str(c.customer_id) if c.customer_id else None,
            customer_name=names.get(c.customer_id) if c.customer_id else None,
            customer_phone=phones.get(c.customer_id) if c.customer_id else None,
            direction=c.direction.value,
            duration_seconds=c.duration_seconds,
            cost_paise=c.cost_paise if allowed["see_money"] else 0,
            cost_display=_money(c.cost_paise) if allowed["see_money"] else "—",
            cost_breakdown=(c.cost_breakdown or {}) if allowed["see_money"] else {},
            number=connections[c.connection_id].external_account_id if c.connection_id in connections else None,
            number_label=voice_numbers.label_of(connections[c.connection_id]) if c.connection_id in connections else None,
            hangup_cause=c.hangup_cause,
            started_at=c.started_at.isoformat(),
            escalated=c.escalated,
            escalation_reason=c.escalation_reason,
            outcome=c.outcome,
            sentiment=c.sentiment,
            topic=c.topic,
            summary=c.summary,
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
# Canonical in shared/channels/voice/tenant.py (needed there to pick a
# grammatically gender-correct default greeting); imported rather than
# redefined here to keep one source of truth.
VALID_SPEAKERS = set(MALE_SPEAKERS) | set(FEMALE_SPEAKERS)
VALID_LANGUAGE_MODES = {"adaptive", "fixed"}
# Sarvam bulbul:v3's actual documented TTS language coverage (confirmed
# against docs.sarvam.ai, not guessed) - what a business can pick as this
# agent's SPOKEN language. STT covers more (22 languages,
# docs.sarvam.ai/api-reference-docs/speech-to-text/transcribe) but that only
# matters for adaptive mode's own auto-detection, which needs no picker at
# all - a caller can be understood in a language the agent still can't speak
# back in.
SUPPORTED_LANGUAGES = [
    {"code": "en-IN", "label": "English"},
    {"code": "hi-IN", "label": "Hindi"},
    {"code": "bn-IN", "label": "Bengali"},
    {"code": "gu-IN", "label": "Gujarati"},
    {"code": "kn-IN", "label": "Kannada"},
    {"code": "ml-IN", "label": "Malayalam"},
    {"code": "mr-IN", "label": "Marathi"},
    {"code": "od-IN", "label": "Odia"},
    {"code": "pa-IN", "label": "Punjabi"},
    {"code": "ta-IN", "label": "Tamil"},
    {"code": "te-IN", "label": "Telugu"},
]
VALID_LANGUAGES = {l["code"] for l in SUPPORTED_LANGUAGES}


async def _voice_connection(
    business_id: uuid.UUID, db: DbDep, connection_id: uuid.UUID | None = None,
) -> ChannelConnection:
    """
    One of this business's connected voice numbers. Name it with `connection_id`; without one,
    use the default for outgoing calls (else the oldest) - never fail just because there are two.
    """
    numbers = await voice_numbers.active(db, business_id)
    if connection_id is not None:
        connection = next((c for c in numbers if c.id == connection_id), None)
        if connection is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That number is not connected here")
        return connection
    connection = voice_numbers.choose_outbound(numbers) or (numbers[0] if numbers else None)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect a voice number first: buy one under Phone Numbers",
        )
    return connection


class LanguageOption(BaseModel):
    code: str
    label: str


class AgentSettingsOut(BaseModel):
    # Which number these settings belong to, and what the owner calls it.
    connection_id: str
    number: str
    label: str
    use: str  # both | inbound | outbound
    default_outbound: bool
    series: str | None
    greeting: str
    language: str
    language_mode: str
    speaker: str
    male_speakers: list[str]
    female_speakers: list[str]
    languages: list[LanguageOption]
    # None means neither warm-transfer-on-escalate nor copilot mode is in
    # use - the default, and what preserves today's behaviour exactly.
    staff_phone_number: str | None
    copilot_mode: bool
    # False (the default) even when copilot_mode is on: whether KROVA also
    # listens live and whispers suggestions to staff, the one part of
    # copilot mode with an ongoing per-second AI cost - see
    # shared/channels/voice/tenant.py's VoiceRoute.copilot_live_suggestions.
    copilot_live_suggestions: bool
    # None means every call is customer-facing, today's exact behaviour -
    # see Business.owner_phone's own docstring.
    owner_phone: str | None
    # False (the default) is today's exact behaviour: no public page.
    # Business.settings key, not a column - see services/api/routers/
    # trust.py, the endpoint this gates.
    public_trust_page_enabled: bool


def _agent_settings_out(connection: ChannelConnection, business: Business | None) -> AgentSettingsOut:
    extra = connection.extra or {}
    language = extra.get("language", "en-IN")
    speaker = extra.get("speaker", "shubh")
    business_name = business.name if business else "your business"
    return AgentSettingsOut(
        connection_id=str(connection.id),
        number=connection.external_account_id,
        label=voice_numbers.label_of(connection),
        use=voice_numbers.use_of(connection),
        default_outbound=voice_numbers.is_default_outbound(connection),
        series=voice_numbers.series_of(connection.external_account_id),
        # Same default a live call actually speaks (tenant.py's
        # default_greeting) - this used to be a second, English-only copy
        # of that fallback, so the settings page and a real call could
        # show two different greetings for a non-English agent.
        greeting=extra.get("greeting")
        or default_greeting(language, speaker, business_name),
        language=language,
        language_mode=extra.get("language_mode", "adaptive"),
        speaker=speaker,
        male_speakers=MALE_SPEAKERS,
        female_speakers=FEMALE_SPEAKERS,
        languages=[LanguageOption(**l) for l in SUPPORTED_LANGUAGES],
        staff_phone_number=extra.get("staff_phone_number") or None,
        copilot_mode=bool(extra.get("copilot_mode", False)),
        copilot_live_suggestions=bool(extra.get("copilot_live_suggestions", False)),
        owner_phone=business.owner_phone if business else None,
        public_trust_page_enabled=bool((business.settings or {}).get("public_trust_page_enabled")) if business else False,
    )


@router.get("/agent-settings", response_model=AgentSettingsOut)
async def get_agent_settings(
    current_user: OwnerOrAdminDep, db: DbDep, connection_id: uuid.UUID | None = Query(default=None),
) -> AgentSettingsOut:
    connection = await _voice_connection(current_user.business, db, connection_id)
    business = await db.get(Business, current_user.business)
    return _agent_settings_out(connection, business)


class KeypadOptionIO(BaseModel):
    digit: str
    label: str
    action: str  # transfer | say | ai
    number: str | None = None
    message: str | None = None


class KeypadMenuIO(BaseModel):
    enabled: bool = False
    intro: str = ""
    options: list[KeypadOptionIO] = []


class KeypadMenuOut(KeypadMenuIO):
    # What a caller will hear after the greeting, so the owner can read it back.
    preview: str = ""


def _menu_out(connection: ChannelConnection) -> KeypadMenuOut:
    extra = connection.extra or {}
    menu = extra.get("keypad_menu") or {"enabled": False, "intro": "", "options": []}
    return KeypadMenuOut(
        enabled=bool(menu.get("enabled")),
        intro=menu.get("intro") or "",
        options=[KeypadOptionIO(**o) for o in menu.get("options") or []],
        preview=keypad_menu.spoken_prompt(menu, extra.get("language", "en-IN")),
    )


@router.get("/keypad-menu", response_model=KeypadMenuOut)
async def get_keypad_menu(
    current_user: OwnerOrAdminDep, db: DbDep, connection_id: uuid.UUID | None = Query(default=None),
) -> KeypadMenuOut:
    return _menu_out(await _voice_connection(current_user.business, db, connection_id))


@router.put("/keypad-menu", response_model=KeypadMenuOut)
async def put_keypad_menu(
    body: KeypadMenuIO, current_user: OwnerOrAdminDep, db: DbDep, connection_id: uuid.UUID | None = Query(default=None),
) -> KeypadMenuOut:
    """Replace the whole menu of one number: the screen always sends every key, so what you see is what is stored."""
    connection = await _voice_connection(current_user.business, db, connection_id)
    try:
        menu = keypad_menu.clean(body.model_dump(), normalise_phone)
    except keypad_menu.MenuError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    connection.extra = {**(connection.extra or {}), "keypad_menu": menu}
    await db.commit()
    logger.info(
        "voice keypad menu saved business=%s enabled=%s keys=%s",
        current_user.business, menu["enabled"], [o["digit"] for o in menu["options"]],
    )
    return _menu_out(connection)


class AgentSettingsIn(BaseModel):
    # This number's name ("Sales"), what it is for, and whether outgoing calls default to it.
    label: str | None = None
    use: str | None = None
    default_outbound: bool | None = None
    greeting: str | None = None
    language: str | None = None
    language_mode: str | None = None
    speaker: str | None = None
    # Send "" to clear a previously-set number - distinct from omitting the
    # field, which leaves whatever is already saved untouched, same
    # optional-field convention every other field on this model follows.
    staff_phone_number: str | None = None
    copilot_mode: bool | None = None
    copilot_live_suggestions: bool | None = None
    # Same "" clears / omit leaves untouched convention as
    # staff_phone_number - lives on Business, not this connection's
    # extra, since it identifies a person, not a per-number setting.
    owner_phone: str | None = None
    public_trust_page_enabled: bool | None = None


@router.patch("/agent-settings", response_model=AgentSettingsOut)
async def update_agent_settings(
    body: AgentSettingsIn, current_user: OwnerOrAdminDep, db: DbDep,
    connection_id: uuid.UUID | None = Query(default=None),
) -> AgentSettingsOut:
    """
    Every field is optional and only what's sent gets changed - a business
    picking just a voice shouldn't have to resend its own greeting text to
    avoid losing it. `connection_id` says which number; the greeting, voice, language,
    staff phone and the rest belong to that number alone.
    """
    connection = await _voice_connection(current_user.business, db, connection_id)

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

    normalised_staff_number: str | None = None
    if body.staff_phone_number is not None and body.staff_phone_number.strip():
        try:
            normalised_staff_number = normalise_phone(body.staff_phone_number)
        except InvalidIdentifier as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"staff_phone_number: {exc}",
            ) from exc

    normalised_owner_number: str | None = None
    if body.owner_phone is not None and body.owner_phone.strip():
        try:
            normalised_owner_number = normalise_phone(body.owner_phone)
        except InvalidIdentifier as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"owner_phone: {exc}",
            ) from exc

    extra = dict(connection.extra or {})

    # Checked against the state this request actually RESULTS in, not just
    # what's stored before it - a request that clears staff_phone_number
    # while leaving (or setting) copilot_mode true must be rejected too,
    # not just a bare "turn copilot_mode on with nothing ever configured".
    # Confirmed this distinction matters by testing it directly: checking
    # only the pre-request extra let exactly that combination through.
    resulting_staff_number = (
        normalised_staff_number if body.staff_phone_number is not None
        else extra.get("staff_phone_number")
    )
    resulting_copilot_mode = (
        body.copilot_mode if body.copilot_mode is not None
        else bool(extra.get("copilot_mode", False))
    )
    from shared.channels.voice import line_profile

    has_list = bool(line_profile.read(extra)["pickup"]["levels"])
    if resulting_copilot_mode and not resulting_staff_number and not has_list:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Add someone under \"Who answers\" (or a staff phone) before turning Live Copilot on",
        )

    if body.greeting is not None:
        extra["greeting"] = body.greeting.strip()
    if body.language is not None:
        extra["language"] = body.language
    if body.language_mode is not None:
        extra["language_mode"] = body.language_mode
    if body.speaker is not None:
        extra["speaker"] = body.speaker
    if body.staff_phone_number is not None:
        extra["staff_phone_number"] = normalised_staff_number
    if body.copilot_mode is not None:
        extra["copilot_mode"] = body.copilot_mode
    if body.copilot_live_suggestions is not None:
        extra["copilot_live_suggestions"] = body.copilot_live_suggestions
    try:
        if body.label is not None:
            extra["label"] = voice_numbers.clean_label(body.label)
        if body.use is not None:
            extra["use"] = voice_numbers.clean_use(body.use)
            if extra["use"] == "inbound":
                extra["default_outbound"] = False  # an incoming-only number cannot be the outgoing default
        connection.extra = extra
        if body.default_outbound is not None:
            voice_numbers.apply_default(
                await voice_numbers.active(db, current_user.business), connection, body.default_outbound,
            )
    except voice_numbers.NumberError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    business = await db.get(Business, current_user.business)
    if body.owner_phone is not None and business is not None:
        business.owner_phone = normalised_owner_number
    if body.public_trust_page_enabled is not None and business is not None:
        business.settings = {
            **(business.settings or {}),
            "public_trust_page_enabled": body.public_trust_page_enabled,
        }

    await db.commit()

    logger.info(
        "voice agent settings updated business=%s fields=%s",
        current_user.business,
        [k for k, v in body.model_dump().items() if v is not None],
    )

    return _agent_settings_out(connection, business)


# 37 real speaker names tell a business owner nothing - this is what lets
# them actually hear one before picking it. Deliberately not gated behind
# _voice_connection: choosing a voice is a reasonable thing to explore
# while still mid-compliance, before any number is bought yet.
#
# Translations beyond en-IN/hi-IN are a short, generic greeting only -
# reviewed for plausibility, not confirmed by a native speaker of each
# language. Worth a real spot-check before leaning on this for anything
# beyond letting a business owner sample a voice.
#
# Hindi, Marathi and Punjabi mark the speaker's own gender on the verb
# ("can help" - सकता/सकती, शकतो/शकते, ਸਕਦਾ/ਸਕਦੀ) - a fixed masculine text
# spoken by a female voice (e.g. "priya", "ritu") would be grammatically
# wrong on every single call, not a translation nuance. Those three carry
# both forms, picked by _preview_text()/_default_greeting() below via
# MALE_SPEAKERS/FEMALE_SPEAKERS. Gujarati's own present-tense "can"
# construction may or may not need the same split - left as one text,
# same honesty as every other language here: not confirmed native.
# Bengali, Odia, and the four Dravidian languages (Kannada, Malayalam,
# Tamil, Telugu) do not mark gender on a first-person verb this way, so
# one text genuinely covers both speaker genders in those.
_PREVIEW_TEXT: dict[str, str] = {
    "en-IN": "Hello! Thank you for calling. How can I help you today?",
    "bn-IN": "নমস্কার! কল করার জন্য ধন্যবাদ। আমি আপনাকে কীভাবে সাহায্য করতে পারি?",
    "gu-IN": "નમસ્તે! કૉલ કરવા બદલ આભાર. હું તમારી કેવી રીતે મદદ કરી શકું?",
    "kn-IN": "ನಮಸ್ಕಾರ! ಕರೆ ಮಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
    "ml-IN": "നമസ്കാരം! വിളിച്ചതിന് നന്ദി. ഞാൻ നിങ്ങളെ എങ്ങനെ സഹായിക്കും?",
    "od-IN": "ନମସ୍କାର! କଲ୍ କରିଥିବାରୁ ଧନ୍ୟବାଦ। ମୁଁ ଆପଣଙ୍କୁ କିପରି ସାହାଯ୍ୟ କରିପାରିବି?",
    "ta-IN": "வணக்கம்! அழைத்ததற்கு நன்றி. நான் உங்களுக்கு எப்படி உதவ முடியும்?",
    "te-IN": "నమస్కారం! కాల్ చేసినందుకు ధన్యవాదాలు. నేను మీకు ఎలా సహాయం చేయగలను?",
}
_PREVIEW_TEXT_GENDERED: dict[str, dict[str, str]] = {
    "hi-IN": {
        "male": "नमस्ते! कॉल करने के लिए धन्यवाद। मैं आपकी कैसे मदद कर सकता हूं?",
        "female": "नमस्ते! कॉल करने के लिए धन्यवाद। मैं आपकी कैसे मदद कर सकती हूं?",
    },
    "mr-IN": {
        "male": "नमस्कार! कॉल केल्याबद्दल धन्यवाद. मी तुम्हाला कशी मदत करू शकतो?",
        "female": "नमस्कार! कॉल केल्याबद्दल धन्यवाद. मी तुम्हाला कशी मदत करू शकते?",
    },
    "pa-IN": {
        "male": "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ! ਕਾਲ ਕਰਨ ਲਈ ਧੰਨਵਾਦ। ਮੈਂ ਤੁਹਾਡੀ ਕਿਵੇਂ ਮਦਦ ਕਰ ਸਕਦਾ ਹਾਂ?",
        "female": "ਸਤਿ ਸ੍ਰੀ ਅਕਾਲ! ਕਾਲ ਕਰਨ ਲਈ ਧੰਨਵਾਦ। ਮੈਂ ਤੁਹਾਡੀ ਕਿਵੇਂ ਮਦਦ ਕਰ ਸਕਦੀ ਹਾਂ?",
    },
}


def _speaker_gender(speaker: str) -> str:
    return "female" if speaker in FEMALE_SPEAKERS else "male"


def _localized_text(language: str, speaker: str) -> str:
    """The gender-correct text for this language/speaker pair, falling
    back to en-IN for anything neither table covers."""
    gendered = _PREVIEW_TEXT_GENDERED.get(language)
    if gendered is not None:
        return gendered[_speaker_gender(speaker)]
    return _PREVIEW_TEXT.get(language, _PREVIEW_TEXT["en-IN"])


class VoicePreviewIn(BaseModel):
    speaker: str
    language: str = "en-IN"


class VoicePreviewOut(BaseModel):
    audio_base64: str
    format: str = "wav"


@router.post("/preview-voice", response_model=VoicePreviewOut)
async def preview_voice(body: VoicePreviewIn, current_user: OwnerOrAdminDep) -> VoicePreviewOut:
    if body.speaker not in VALID_SPEAKERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unrecognised speaker - see male_speakers/female_speakers on GET /agent-settings",
        )
    if body.language not in VALID_LANGUAGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"language must be one of {sorted(VALID_LANGUAGES)}",
        )

    text = _localized_text(body.language, body.speaker)
    try:
        audio_base64 = await sarvam.generate_preview(
            text=text, speaker=body.speaker, language=body.language
        )
    except sarvam.PreviewError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return VoicePreviewOut(audio_base64=audio_base64, format="wav")


# ── 140/160-series number requests ──────────────────────────────────────
#
# Neither is Plivo's normal self-serve inventory the way a regular local
# number is (confirmed: nothing listed under 160 in Plivo's own number
# search) - getting one needs direct coordination with Plivo, which
# nobody can automate. This is a request/queue, not a purchase flow.
#
# Deliberately no per-request approval gate: a business submits, it lands
# here, and the platform operator works through pending requests in
# batches whenever real Plivo coordination happens - not gated by a
# review step per submission (confirmed with Nikhil directly, twice).
# bfsi_declaration is self-certified and never validated against
# request_type here - the real eligibility check happens with Plivo
# during actual provisioning, not in this code.

VALID_NUMBER_REQUEST_TYPES = {t.value for t in NumberRequestType}


def _is_platform_admin(current_user: CurrentUserDep) -> bool:
    """
    PLATFORM_ADMIN_EMAIL is a comma-separated list, not a single address -
    Nikhil hit this directly needing a second admin and having to swap the
    one email already there. Case-insensitive on both sides: User.email is
    stored lowercased by service.normalise_email at registration, but
    nothing guarantees the env var was typed that way too.
    """
    admins = {
        e.strip().lower()
        for e in settings.platform_admin_email.split(",")
        if e.strip()
    }
    # A phone-only account (no email at all) is never a platform admin -
    # there is nothing to compare against the allowlist.
    return bool(current_user.email) and current_user.email.lower() in admins


def _require_platform_admin(current_user: CurrentUserDep) -> None:
    if not _is_platform_admin(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised")


class NumberRequestIn(BaseModel):
    request_type: str
    justification: str
    bfsi_declaration: bool = False


class NumberRequestOut(BaseModel):
    id: str
    business_id: str
    request_type: str
    justification: str
    bfsi_declaration: bool
    status: str
    admin_notes: str | None
    provisioned_number: str | None
    created_at: str


def _number_request_out(row: NumberRequest) -> NumberRequestOut:
    return NumberRequestOut(
        id=str(row.id),
        business_id=str(row.business_id),
        request_type=row.request_type.value if hasattr(row.request_type, "value") else row.request_type,
        justification=row.justification,
        bfsi_declaration=row.bfsi_declaration,
        status=row.status.value if hasattr(row.status, "value") else row.status,
        admin_notes=row.admin_notes,
        provisioned_number=row.provisioned_number,
        created_at=row.created_at.isoformat(),
    )


@router.post("/number-requests", response_model=NumberRequestOut, status_code=status.HTTP_201_CREATED)
async def create_number_request(
    body: NumberRequestIn, current_user: OwnerOrAdminDep, db: DbDep
) -> NumberRequestOut:
    if body.request_type not in VALID_NUMBER_REQUEST_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"request_type must be one of {sorted(VALID_NUMBER_REQUEST_TYPES)}",
        )
    if not body.justification.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="justification is required")

    row = NumberRequest(
        business_id=current_user.business,
        request_type=NumberRequestType(body.request_type),
        justification=body.justification.strip(),
        bfsi_declaration=body.bfsi_declaration,
        status=NumberRequestStatus.requested,
        requested_by_user_id=current_user.id,
    )
    db.add(row)
    await db.commit()
    logger.info(
        "number request created business=%s type=%s", current_user.business, body.request_type
    )
    return _number_request_out(row)


@router.get("/number-requests", response_model=list[NumberRequestOut])
async def list_own_number_requests(current_user: OwnerOrAdminDep, db: DbDep) -> list[NumberRequestOut]:
    rows = (
        await db.execute(
            select(NumberRequest)
            .where(NumberRequest.business_id == current_user.business)
            .order_by(NumberRequest.created_at.desc())
        )
    ).scalars().all()
    return [_number_request_out(r) for r in rows]


@router.get("/number-requests/all", response_model=list[NumberRequestOut])
async def list_all_number_requests(current_user: OwnerOrAdminDep, db: DbDep) -> list[NumberRequestOut]:
    """The platform operator's own queue - every business's requests, not just current_user's."""
    _require_platform_admin(current_user)
    rows = (
        await db.execute(select(NumberRequest).order_by(NumberRequest.created_at.desc()))
    ).scalars().all()
    return [_number_request_out(r) for r in rows]


class NumberRequestUpdateIn(BaseModel):
    status: str | None = None
    admin_notes: str | None = None
    provisioned_number: str | None = None


@router.patch("/number-requests/{request_id}", response_model=NumberRequestOut)
async def update_number_request(
    request_id: uuid.UUID, body: NumberRequestUpdateIn, current_user: OwnerOrAdminDep, db: DbDep
) -> NumberRequestOut:
    _require_platform_admin(current_user)

    row = await db.get(NumberRequest, request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such number request")

    if body.status is not None:
        valid_statuses = {s.value for s in NumberRequestStatus}
        if body.status not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"status must be one of {sorted(valid_statuses)}",
            )
        row.status = NumberRequestStatus(body.status)
    if body.admin_notes is not None:
        row.admin_notes = body.admin_notes
    if body.provisioned_number is not None:
        row.provisioned_number = body.provisioned_number

    await db.commit()
    logger.info("number request updated id=%s by=%s", request_id, current_user.email)
    return _number_request_out(row)

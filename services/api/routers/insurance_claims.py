"""
The TPA/Insurance Claim Tracking capability's door: record a claim, keep its
status current, see what's outstanding. See shared/db/models/claim.py for
why this is not the Case Tracking capability with different labels.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared import verticals
from shared.db.models import Business, ClaimStatus, Customer, InsuranceClaim
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/insurance-claims", tags=["insurance_claims"])


async def _require_tpa_claim_tracking(business_id: uuid.UUID, db: DbDep) -> Business:
    """
    Every endpoint here needs this - unlike Queue there is no single shared
    "issue a token" choke point to gate once, so each entry point checks for
    itself. The sidebar hides the /claims link for a business without the
    capability, but that was the only boundary before this: a business
    could otherwise hit these endpoints directly regardless of vertical.
    """
    business = await db.get(Business, business_id)
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found")
    if not verticals.has_capability(business, "tpa_claim_tracking"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This business does not have the tpa_claim_tracking capability"
        )
    return business


class ClaimIn(BaseModel):
    customer_id: str
    insurer_or_tpa_name: str | None = Field(default=None, max_length=255)
    policy_number: str | None = Field(default=None, max_length=100)
    claim_number: str | None = Field(default=None, max_length=100)
    claim_amount_paise: int | None = None
    submitted_at: datetime | None = None
    notes: str | None = None


class ClaimPatch(BaseModel):
    insurer_or_tpa_name: str | None = None
    policy_number: str | None = None
    claim_number: str | None = None
    status: ClaimStatus | None = None
    claim_amount_paise: int | None = None
    approved_amount_paise: int | None = None
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    notes: str | None = None


class ClaimOut(BaseModel):
    id: str
    customer_id: str
    insurer_or_tpa_name: str | None
    policy_number: str | None
    claim_number: str | None
    status: str
    claim_amount_paise: int | None
    approved_amount_paise: int | None
    submitted_at: datetime | None
    decided_at: datetime | None
    notes: str | None


def _out(c: InsuranceClaim) -> ClaimOut:
    return ClaimOut(
        id=str(c.id), customer_id=str(c.customer_id),
        insurer_or_tpa_name=c.insurer_or_tpa_name, policy_number=c.policy_number,
        claim_number=c.claim_number,
        status=c.status.value if hasattr(c.status, "value") else str(c.status),
        claim_amount_paise=c.claim_amount_paise, approved_amount_paise=c.approved_amount_paise,
        submitted_at=c.submitted_at, decided_at=c.decided_at, notes=c.notes,
    )


@router.get("", response_model=list[ClaimOut])
async def list_claims(
    current_user: CurrentUserDep,
    db: DbDep,
    customer_id: str | None = None,
    status_filter: ClaimStatus | None = Query(default=None, alias="status"),
) -> list[ClaimOut]:
    await _require_tpa_claim_tracking(current_user.business, db)
    query = select(InsuranceClaim).where(
        InsuranceClaim.business_id == current_user.business
    ).order_by(InsuranceClaim.submitted_at.desc().nullslast())
    if customer_id:
        query = query.where(InsuranceClaim.customer_id == uuid.UUID(customer_id))
    if status_filter:
        query = query.where(InsuranceClaim.status == status_filter)
    rows = await db.execute(query)
    return [_out(c) for c in rows.scalars().all()]


@router.post("", response_model=ClaimOut, status_code=status.HTTP_201_CREATED)
async def create_claim(body: ClaimIn, current_user: CurrentUserDep, db: DbDep) -> ClaimOut:
    await _require_tpa_claim_tracking(current_user.business, db)
    customer = await db.get(Customer, uuid.UUID(body.customer_id))
    if customer is None or customer.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")

    claim = InsuranceClaim(
        business_id=current_user.business,
        customer_id=customer.id,
        insurer_or_tpa_name=body.insurer_or_tpa_name,
        policy_number=body.policy_number,
        claim_number=body.claim_number,
        claim_amount_paise=body.claim_amount_paise,
        submitted_at=body.submitted_at,
        notes=body.notes,
    )
    db.add(claim)
    await db.flush()
    logger.info("insurance claim created id=%s business=%s", claim.id, current_user.business)
    return _out(claim)


@router.patch("/{claim_id}", response_model=ClaimOut)
async def update_claim(claim_id: uuid.UUID, body: ClaimPatch, current_user: CurrentUserDep, db: DbDep) -> ClaimOut:
    await _require_tpa_claim_tracking(current_user.business, db)
    claim = await db.get(InsuranceClaim, claim_id)
    if claim is None or claim.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Claim not found")
    for field in (
        "insurer_or_tpa_name", "policy_number", "claim_number", "status",
        "claim_amount_paise", "approved_amount_paise", "submitted_at", "decided_at", "notes",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(claim, field, value)
    await db.flush()
    return _out(claim)

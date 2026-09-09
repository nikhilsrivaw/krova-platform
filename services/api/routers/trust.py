"""
The public voice trust page's door - a business opts in
(Business.settings["public_trust_page_enabled"]), then anyone with the
link can see their real call-answering numbers, no login.

Same "public but not just anyone" model as widget.py/kiosk.py: resolve
the business from an opaque-enough public identifier, 404 rather than a
leaky distinguishing error when it isn't opted in. business_id itself is
the identifier here - no nicer public slug exists on Business today, a
known v1 compromise, not a silently invented one. Deliberately no
CurrentUserDep anywhere in this file - DbDep only.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from services.api.dependencies import DbDep
from shared.care import voice_trust
from shared.db.models import Business

router = APIRouter(prefix="/trust", tags=["trust"])


class VoiceTrustOut(BaseModel):
    business_name: str
    window_days: int
    total_calls: int
    avg_ring_to_answer_seconds: float | None
    escalation_rate: float | None
    avg_duration_seconds: float | None


@router.get("/voice/{business_id}", response_model=VoiceTrustOut)
async def voice_trust_page(business_id: uuid.UUID, db: DbDep) -> VoiceTrustOut:
    business = await db.get(Business, business_id)
    if business is None or not (business.settings or {}).get("public_trust_page_enabled"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    s = await voice_trust.stats(business_id, db)
    return VoiceTrustOut(
        business_name=business.name,
        window_days=s.window_days,
        total_calls=s.total_calls,
        avg_ring_to_answer_seconds=s.avg_ring_to_answer_seconds,
        escalation_rate=s.escalation_rate,
        avg_duration_seconds=s.avg_duration_seconds,
    )

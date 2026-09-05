"""
The TPA/Insurance Claim Tracking capability: a patient's cashless or
reimbursement claim, and where it stands.

Deliberately not the Case Tracking capability reused with different labels
(see shared/db/models/case.py's own docstring for that same argument) - a
claim has money and an external insurer/TPA party, and a lifecycle
(submitted -> under_review -> query_raised -> approved/rejected -> settled)
that a law firm's intake/active/on_hold/closed shape cannot honestly express.
Approving or rejecting a claim is always the insurer's/TPA's decision, never
the agent's or the business's - clinic.json's known_gaps says so - this
table only ever records what actually happened, never predicts an outcome.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class ClaimStatus(str, enum.Enum):
    submitted = "submitted"
    under_review = "under_review"
    query_raised = "query_raised"   # insurer/TPA needs more documents
    approved = "approved"
    rejected = "rejected"
    settled = "settled"


class InsuranceClaim(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "insurance_claims"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )

    insurer_or_tpa_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    policy_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claim_number: Mapped[str | None] = mapped_column(String(100), nullable=True)

    status: Mapped[ClaimStatus] = mapped_column(
        EnumType(ClaimStatus, 20), nullable=False, default=ClaimStatus.submitted
    )

    # Integer paise, same convention as Commitment - never a float.
    claim_amount_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_amount_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_claims_business_status", "business_id", "status"),
        Index("idx_claims_customer", "customer_id"),
    )

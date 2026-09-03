"""
Requests for a 140-series (promotional) or 160-series (BFSI-only
transactional/service) Plivo number.

Neither is Plivo's normal self-serve inventory the way a regular local
number is (confirmed: nothing listed under 160 in Plivo's own number
search) - getting one needs direct coordination with Plivo, which nobody
can automate. This table exists to track that request and its outcome,
not to provision anything itself.

Deliberately no per-request approval gate: a business submits, it lands
here, and Krova's own operator works through pending requests in batches
whenever real Plivo coordination happens - not gated by a review step per
submission. `bfsi_declaration` is self-certified for the same reason:
checked nowhere against `request_type` in code, since the actual
eligibility check happens with Plivo during real provisioning, not here.
"""

import enum
import uuid

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class NumberRequestType(str, enum.Enum):
    promotional_140 = "promotional_140"
    transactional_160 = "transactional_160"


class NumberRequestStatus(str, enum.Enum):
    requested = "requested"
    submitted_to_plivo = "submitted_to_plivo"
    provisioned = "provisioned"
    rejected = "rejected"


class NumberRequest(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "number_requests"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )

    request_type: Mapped[NumberRequestType] = mapped_column(
        EnumType(NumberRequestType, 24), nullable=False
    )
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    # Self-certified, only meaningful for transactional_160 - never
    # validated against request_type in code. See module docstring.
    bfsi_declaration: Mapped[bool] = mapped_column(nullable=False, default=False)

    status: Mapped[NumberRequestStatus] = mapped_column(
        EnumType(NumberRequestStatus, 20), nullable=False, default=NumberRequestStatus.requested
    )
    # The platform operator's own working notes - a rejection reason, a
    # Plivo ticket reference. Never shown to the business as anything
    # other than plain status text; this is Krova's own scratch space.
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The real number, once Nikhil actually gets one from Plivo. Wiring
    # this into a live ChannelConnection so the business can use it is a
    # deliberate v1 boundary - a manual follow-up, not done by this table.
    provisioned_number: Mapped[str | None] = mapped_column(Text, nullable=True)

    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("idx_number_requests_business", "business_id", "created_at"),
    )

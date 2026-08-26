"""
The Property Listings capability: an agency's own inventory of what it has
for sale or rent.

Unlike Order (mirrored from a store platform someone else runs) or Case
(opened by a human recording something a court already decided), a
Property is nothing anyone else's system tells us about - it exists only
because the business itself maintains it, the same relationship Doctor has
to a clinic's roster. That is why this is a plain owner-maintained table,
not a webhook target.

Site visits deliberately do NOT get a new booking mechanism. An agent
showing a property is, mechanically, identical to a doctor holding an
appointment slot - a person with recurring hours, a customer, a time. This
vertical reuses shared/scheduling/* (Doctor, AvailabilityRule, Appointment)
wholesale rather than duplicating slot computation and conflict-checking a
second time; "Doctor" is what the table is called, "Agent" is what the
business and its customers see, via the vertical template and the AI
prompts built from it - the same split between generic infrastructure and
vertical-facing language already used everywhere else in this schema.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin
from shared.db.types import EnumType


class ListingType(str, enum.Enum):
    sale = "sale"
    rent = "rent"


class PropertyStatus(str, enum.Enum):
    available = "available"
    under_offer = "under_offer"
    sold = "sold"
    rented = "rented"
    withdrawn = "withdrawn"


class Property(UUIDMixin, TimestampMixin, Base):
    """
    One listing. price_paise means the sale price when listing_type=sale, or
    the rent amount per price_period when listing_type=rent - one column,
    because a listing is only ever one or the other, never both, and a
    separate rent column would sit null half the time.

    rera_registration_number is what the vertical's own policy ("never
    state a property is RERA-registered unless its registration number is
    on file") checks against - null is a real, common, honest state (many
    smaller resale listings genuinely fall outside RERA's registration
    requirement), not a placeholder waiting to be filled in.
    """

    __tablename__ = "properties"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    listing_type: Mapped[ListingType] = mapped_column(EnumType(ListingType, 10), nullable=False)
    # Free text, not an enum - "apartment", "villa", "plot", "office", "1RK"
    # varies too much by city and market to force into a fixed list.
    property_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    locality: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)

    bedrooms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    area_sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)

    price_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "one_time" for a sale price, "monthly" for rent - kept as a string
    # rather than reusing ListingType so a future "per_sqft" or "yearly"
    # doesn't need a schema change.
    price_period: Mapped[str | None] = mapped_column(String(20), nullable=True)

    rera_registration_number: Mapped[str | None] = mapped_column(String(100), nullable=True)

    status: Mapped[PropertyStatus] = mapped_column(
        EnumType(PropertyStatus, 20), nullable=False, default=PropertyStatus.available
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_properties_business", "business_id"),
        Index("idx_properties_business_status", "business_id", "status"),
    )

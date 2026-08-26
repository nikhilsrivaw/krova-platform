"""
The door an agency actually walks through to use Property Listings: add,
update, and browse its own inventory.

Without this, shared/ai/context.py has nothing real to quote a price or a
status from - a listing only becomes something the agent can honestly
answer questions about once staff has recorded it here, the same relation
this has to the AI as Doctors and Cases have to theirs.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import ListingType, Property, PropertyStatus
from shared.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/properties", tags=["properties"])


class PropertyIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    listing_type: ListingType
    property_type: str | None = Field(default=None, max_length=100)
    locality: str | None = Field(default=None, max_length=255)
    address: str | None = None
    bedrooms: int | None = Field(default=None, ge=0)
    area_sqft: int | None = Field(default=None, gt=0)
    price_paise: int | None = Field(default=None, ge=0)
    price_period: str | None = Field(default=None, max_length=20)
    rera_registration_number: str | None = Field(default=None, max_length=100)
    notes: str | None = None


class PropertyPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    property_type: str | None = None
    locality: str | None = None
    address: str | None = None
    bedrooms: int | None = Field(default=None, ge=0)
    area_sqft: int | None = Field(default=None, gt=0)
    price_paise: int | None = Field(default=None, ge=0)
    price_period: str | None = None
    rera_registration_number: str | None = None
    status: PropertyStatus | None = None
    active: bool | None = None
    notes: str | None = None


class PropertyOut(BaseModel):
    id: str
    title: str
    listing_type: str
    property_type: str | None
    locality: str | None
    address: str | None
    bedrooms: int | None
    area_sqft: int | None
    price_paise: int | None
    price_period: str | None
    rera_registration_number: str | None
    status: str
    active: bool
    notes: str | None


def _out(p: Property) -> PropertyOut:
    return PropertyOut(
        id=str(p.id), title=p.title,
        listing_type=p.listing_type.value if hasattr(p.listing_type, "value") else str(p.listing_type),
        property_type=p.property_type, locality=p.locality, address=p.address,
        bedrooms=p.bedrooms, area_sqft=p.area_sqft, price_paise=p.price_paise,
        price_period=p.price_period, rera_registration_number=p.rera_registration_number,
        status=p.status.value if hasattr(p.status, "value") else str(p.status),
        active=p.active, notes=p.notes,
    )


@router.get("", response_model=list[PropertyOut])
async def list_properties(
    current_user: CurrentUserDep,
    db: DbDep,
    status_filter: PropertyStatus | None = Query(default=None, alias="status"),
    listing_type: ListingType | None = None,
    include_inactive: bool = False,
) -> list[PropertyOut]:
    query = select(Property).where(Property.business_id == current_user.business).order_by(
        Property.created_at.desc()
    )
    if not include_inactive:
        query = query.where(Property.active == True)  # noqa: E712
    if status_filter:
        query = query.where(Property.status == status_filter)
    if listing_type:
        query = query.where(Property.listing_type == listing_type)
    rows = await db.execute(query)
    return [_out(p) for p in rows.scalars().all()]


@router.post("", response_model=PropertyOut, status_code=status.HTTP_201_CREATED)
async def create_property(body: PropertyIn, current_user: CurrentUserDep, db: DbDep) -> PropertyOut:
    prop = Property(
        business_id=current_user.business,
        title=body.title,
        listing_type=body.listing_type,
        property_type=body.property_type,
        locality=body.locality,
        address=body.address,
        bedrooms=body.bedrooms,
        area_sqft=body.area_sqft,
        price_paise=body.price_paise,
        price_period=body.price_period,
        rera_registration_number=body.rera_registration_number,
        notes=body.notes,
    )
    db.add(prop)
    await db.flush()
    logger.info("property created id=%s business=%s", prop.id, current_user.business)
    return _out(prop)


@router.patch("/{property_id}", response_model=PropertyOut)
async def update_property(
    property_id: uuid.UUID, body: PropertyPatch, current_user: CurrentUserDep, db: DbDep
) -> PropertyOut:
    prop = await db.get(Property, property_id)
    if prop is None or prop.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    for field in (
        "title", "property_type", "locality", "address", "bedrooms", "area_sqft",
        "price_paise", "price_period", "rera_registration_number", "status", "active", "notes",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(prop, field, value)
    await db.flush()
    return _out(prop)


@router.delete("/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_property(property_id: uuid.UUID, current_user: CurrentUserDep, db: DbDep) -> None:
    prop = await db.get(Property, property_id)
    if prop is None or prop.business_id != current_user.business:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    # Deactivate rather than delete: past viewings (Appointment.property_id)
    # still reference this listing, same reasoning as Doctor's soft delete.
    prop.active = False
    await db.flush()

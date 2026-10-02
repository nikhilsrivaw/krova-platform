"""
Saved, reusable Instagram carousels.

services/api/routers/messages.py's POST /instagram/carousel already sends a
one-off carousel a staff member composes on the spot. This is the other
half: a carousel saved under a name once, so the AI agent can offer it
again in a live reply - the same shape as shared/ai/agent.py's own
share_catalog decision (a business connects one catalog once, the agent
decides when to send it), except a business can have several named
carousels, so the agent has to be told which ones exist and pick one by
name rather than a single on/off flag.

Instagram only. WhatsApp already has its own carousel mechanism - a
Message Template with a CAROUSEL component, Meta-reviewed before it can
ever be sent - and that is a structurally different thing from this: a
Generic Template attachment Meta never reviews, sent live.
"""

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.base import Base, TimestampMixin, UUIDMixin


class InstagramCarousel(UUIDMixin, TimestampMixin, Base):
    """
    One saved carousel. `elements` is the same card shape the one-off
    sender already builds (title, subtitle, image_url, buttons) - stored
    whole, the same "components exactly as sent to Meta" convention
    MessageTemplate.components already uses, since a card's shape can grow
    without a schema change.
    """

    __tablename__ = "instagram_carousels"

    business_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )

    # What the agent refers to it by, and what a staff member picks from a
    # list - lowercase/underscore by convention (not enforced at the DB
    # level, enforced where it's created, the same discipline
    # shared/channels/whatsapp/templates.py's normalise_name already uses
    # for template names).
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # One line telling the agent what this carousel is for ("our 4
    # bestselling kurtas" / "gym membership plans") - this, not the name,
    # is what the agent actually reasons from when deciding whether a
    # customer's question matches it.
    description: Mapped[str] = mapped_column(String(300), nullable=False, default="")

    elements: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint("business_id", "name", name="uq_instagram_carousel_name"),
    )

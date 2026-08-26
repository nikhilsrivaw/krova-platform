"""Every model, imported here so Alembic and SQLAlchemy see the full metadata."""

from shared.db.base import Base
from shared.db.models.billing import UsageEvent, UsageEventType
from shared.db.models.case import Case, CaseStatus
from shared.db.models.campaign import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
)
from shared.db.models.channel import (
    Call,
    Channel,
    ChannelConnection,
    ConnectionStatus,
    Direction,
    Message,
    VoiceProvisioning,
    VoiceProvisioningStatus,
)
from shared.db.models.identity import (
    Business,
    BusinessMember,
    BusinessRole,
    Customer,
    CustomerIdentity,
    IdentityKind,
    RefreshToken,
    User,
)
from shared.db.models.intelligence import (
    BusinessDNA,
    Commitment,
    CommitmentDirection,
    CommitmentKind,
    CommitmentStatus,
    CustomerIntelligence,
    Insight,
)
from shared.db.models.draft import DraftAction, DraftStatus, MessageDraft
from shared.db.models.job import Job, JobStatus
from shared.db.models.knowledge import KnowledgeItem, KnowledgeKind, KnowledgeSource
from shared.db.models.order import Order, OrderStatus, StoreConnection
from shared.db.models.property import ListingType, Property, PropertyStatus
from shared.db.models.scheduling import (
    Appointment,
    AppointmentStatus,
    AvailabilityException,
    AvailabilityRule,
    Department,
    Doctor,
    IntakeChannel,
)
from shared.db.models.template import (
    MessageTemplate,
    TemplateCategory,
    TemplateStatus,
)

__all__ = [
    "Base",
    "Appointment",
    "AppointmentStatus",
    "Audience",
    "AvailabilityException",
    "AvailabilityRule",
    "Business",
    "Case",
    "CaseStatus",
    "BusinessDNA",
    "BusinessMember",
    "BusinessRole",
    "Call",
    "Campaign",
    "CampaignRecipient",
    "CampaignStatus",
    "Channel",
    "ChannelConnection",
    "Commitment",
    "CommitmentDirection",
    "CommitmentKind",
    "CommitmentStatus",
    "ConnectionStatus",
    "DraftAction",
    "DraftStatus",
    "Customer",
    "CustomerIdentity",
    "CustomerIntelligence",
    "Department",
    "Direction",
    "Doctor",
    "IdentityKind",
    "IntakeChannel",
    "Insight",
    "Job",
    "KnowledgeItem",
    "KnowledgeKind",
    "KnowledgeSource",
    "JobStatus",
    "ListingType",
    "Message",
    "MessageDraft",
    "MessageTemplate",
    "Order",
    "OrderStatus",
    "Property",
    "PropertyStatus",
    "RefreshToken",
    "StoreConnection",
    "TemplateCategory",
    "TemplateStatus",
    "UsageEvent",
    "UsageEventType",
    "User",
    "VoiceProvisioning",
    "VoiceProvisioningStatus",
]

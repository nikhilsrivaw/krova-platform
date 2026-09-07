"""Every model, imported here so Alembic and SQLAlchemy see the full metadata."""

from shared.db.base import Base
from shared.db.models.billing import UsageEvent, UsageEventType
from shared.db.models.canned_response import CannedResponse
from shared.db.models.case import Case, CaseStatus
from shared.db.models.claim import ClaimStatus, InsuranceClaim
from shared.db.models.campaign import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
)
from shared.db.models.call_campaign import (
    CallCampaign,
    CallCampaignRecipient,
    CallCampaignRecipientStatus,
    CallCampaignStatus,
)
from shared.db.models.crm import CustomerNote, CustomerTag, TagStatus
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
from shared.db.models.integrations import ApiKey, CalendarConnection, OutboundWebhook, WebhookEventType
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
    Escalation,
    Insight,
)
from shared.db.models.draft import DraftAction, DraftStatus, MessageDraft
from shared.db.models.flow import FlowSendLog, FlowStatus, WhatsAppFlow
from shared.db.models.job import Job, JobStatus
from shared.db.models.knowledge import KnowledgeItem, KnowledgeKind, KnowledgeSource
from shared.db.models.number_request import NumberRequest, NumberRequestStatus, NumberRequestType
from shared.db.models.order import AbandonedCheckout, Order, OrderStatus, ShippingConnection, StoreConnection
from shared.db.models.property import ListingType, Property, PropertyStatus
from shared.db.models.queue import QueueEntry, QueueStatus
from shared.db.models.shift import Shift, ShiftSession
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
from shared.db.models.web_widget import WebSession, WebWidgetConfig

__all__ = [
    "Base",
    "AbandonedCheckout",
    "ApiKey",
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
    "CalendarConnection",
    "CallCampaign",
    "CallCampaignRecipient",
    "CallCampaignRecipientStatus",
    "CallCampaignStatus",
    "Campaign",
    "CampaignRecipient",
    "CampaignStatus",
    "CannedResponse",
    "Channel",
    "ChannelConnection",
    "ClaimStatus",
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
    "CustomerNote",
    "CustomerTag",
    "TagStatus",
    "Department",
    "Direction",
    "Doctor",
    "Escalation",
    "FlowSendLog",
    "FlowStatus",
    "IdentityKind",
    "InsuranceClaim",
    "IntakeChannel",
    "Insight",
    "Job",
    "KnowledgeItem",
    "KnowledgeKind",
    "KnowledgeSource",
    "JobStatus",
    "ListingType",
    "Message",
    "NumberRequest",
    "NumberRequestStatus",
    "NumberRequestType",
    "MessageDraft",
    "MessageTemplate",
    "Order",
    "OrderStatus",
    "OutboundWebhook",
    "Property",
    "PropertyStatus",
    "QueueEntry",
    "QueueStatus",
    "RefreshToken",
    "Shift",
    "ShiftSession",
    "ShippingConnection",
    "StoreConnection",
    "TemplateCategory",
    "TemplateStatus",
    "UsageEvent",
    "UsageEventType",
    "User",
    "VoiceProvisioning",
    "VoiceProvisioningStatus",
    "WebSession",
    "WebWidgetConfig",
    "WebhookEventType",
    "WhatsAppFlow",
]

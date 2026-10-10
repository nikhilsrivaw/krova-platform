"""
The activity log - who on the team did what, kept for the owner.

Two ways a row gets written, and why:

1. **Automatically, for every state-changing request** (services/api/
   audit_middleware.py). Not each handler remembering to: a route added next
   month is logged without anyone thinking of it, and a route nobody labelled
   still gets a row (a generic one) rather than slipping through. A refused
   request is a row too - an agent trying to download the customer list is
   exactly what the owner wants to see.
2. **By hand, for sign-ins** (record_login), because signing in has no
   signed-in actor for the middleware to read yet.

A handler can add what the URL alone does not say with `note(...)` - "42
recipients", "to ****3210" - and only ever ids, counts and masked values:
never message text or customer details (see ActivityLog's docstring).

Labelling is data: LABELS maps a route to a stable action key, a sentence the
owner can read, and a `kind`:
  work      day-to-day for one customer (replies, approvals, assignments, CRM)
  config    changes how the business runs (campaigns, automations, templates,
            settings, features, channels)
  security  data leaving or access being granted (exports, API keys, webhooks,
            credentials, team changes, sign-ins)
"""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shared.utils.logging import get_logger

logger = get_logger(__name__)

API = "/api/v1"

WORK, CONFIG, SECURITY = "work", "config", "security"


# ── Per-request context ─────────────────────────────────────────────────────

@dataclass(slots=True)
class RequestMeta:
    ip: str | None = None
    user_agent: str | None = None
    notes: dict = field(default_factory=dict)


_meta: ContextVar[RequestMeta | None] = ContextVar("activity_meta", default=None)


def begin_request(ip: str | None, user_agent: str | None) -> RequestMeta:
    meta = RequestMeta(ip=ip, user_agent=(user_agent or "")[:300] or None)
    _meta.set(meta)
    return meta


def current_meta() -> RequestMeta | None:
    return _meta.get()


def note(**detail) -> None:
    """
    Add detail to this request's log row. Ids, counts, masked values - never
    message text or customer details. A no-op outside a request.
    """
    meta = _meta.get()
    if meta is not None:
        meta.notes.update({k: v for k, v in detail.items() if v is not None})


def mask_phone(value: str | None) -> str | None:
    """'919876543210' -> '****3210' - enough to recognise, not to dial."""
    if not value:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"


# ── What a route is called ──────────────────────────────────────────────────

LABELS: dict[tuple[str, str], tuple[str, str, str]] = {}


def _add(method: str, path: str, action: str, summary: str, kind: str) -> None:
    LABELS[(method, API + path)] = (action, summary, kind)


# Day-to-day work, one customer at a time.
for _path, _what in [
    ("/messages/text", "a WhatsApp message"),
    ("/messages/template", "a WhatsApp template message"),
    ("/messages/interactive-buttons", "a WhatsApp message with buttons"),
    ("/messages/interactive-list", "a WhatsApp list message"),
    ("/messages/product", "a product on WhatsApp"),
    ("/messages/products", "products on WhatsApp"),
    ("/messages/catalog", "the catalogue on WhatsApp"),
    ("/messages/instagram/text", "an Instagram message"),
]:
    _add("POST", _path, "message_sent", f"Sent {_what}", WORK)
_add("POST", "/messages/instagram/carousel", "carousel_sent", "Sent an Instagram carousel", WORK)
_add("POST", "/messages/instagram/carousels/{carousel_id}/send", "carousel_sent", "Sent a saved Instagram carousel", WORK)
_add("POST", "/messages/instagram/carousels", "carousel_saved", "Saved an Instagram carousel", WORK)
_add("DELETE", "/messages/instagram/carousels/{carousel_id}", "carousel_deleted", "Deleted a saved Instagram carousel", WORK)
_add("POST", "/messages/instagram/publish", "instagram_post_published", "Published an Instagram post", CONFIG)
_add("POST", "/approvals/{draft_id}/approve", "draft_approved", "Approved an AI reply", WORK)
_add("POST", "/approvals/{draft_id}/reject", "draft_rejected", "Rejected an AI reply", WORK)
_add("POST", "/conversations/{customer_id}/assign", "conversation_assigned", "Assigned a conversation", WORK)
_add("PUT", "/team/me/availability", "availability_changed", "Changed own availability", WORK)
_add("POST", "/team/transfer-ownership", "ownership_transferred", "Transferred ownership of the business", SECURITY)
_add("PUT", "/team/settings", "team_settings_changed", "Changed team settings", CONFIG)
_add("POST", "/conversations/{customer_id}/take-over", "conversation_taken_over", "Took over a conversation", WORK)
_add("POST", "/escalations/{escalation_id}/claim", "escalation_claimed", "Took an escalation", WORK)
_add("POST", "/escalations/{escalation_id}/release", "escalation_released", "Handed an escalation back", WORK)
_add("PATCH", "/escalations/{escalation_id}/status", "escalation_updated", "Updated an escalation", WORK)
_add("POST", "/escalations/{escalation_id}/acknowledge", "escalation_updated", "Acknowledged an escalation", WORK)
_add("POST", "/flows/{flow_id}/send", "flow_sent", "Sent a WhatsApp Flow to a customer", WORK)
_add("POST", "/leads/manual", "lead_added", "Added a lead by hand", WORK)
_add("POST", "/knowledge", "knowledge_changed", "Added to the knowledge base", WORK)
_add("POST", "/knowledge/upload", "knowledge_changed", "Uploaded to the knowledge base", WORK)
_add("DELETE", "/knowledge/{item_id}", "knowledge_changed", "Removed from the knowledge base", WORK)

# Reaches many customers, or keeps acting on its own.
_add("POST", "/campaigns", "campaign_created", "Created a WhatsApp campaign", CONFIG)
_add("POST", "/campaigns/{campaign_id}/send", "campaign_sent", "Sent a WhatsApp campaign", CONFIG)
_add("POST", "/campaigns/{campaign_id}/steps", "campaign_changed", "Added a follow-up step to a campaign", CONFIG)
_add("DELETE", "/campaigns/{campaign_id}/steps/{step_id}", "campaign_changed", "Removed a campaign follow-up step", CONFIG)
_add("POST", "/call-campaigns", "call_campaign_created", "Created a voice campaign", CONFIG)
_add("POST", "/call-campaigns/{campaign_id}/send", "call_campaign_sent", "Launched a voice campaign", CONFIG)
_add("POST", "/leads/import", "leads_imported", "Imported leads in bulk", CONFIG)
_add("POST", "/post-call-rules", "automation_created", "Created an automation", CONFIG)
_add("PATCH", "/post-call-rules/{rule_id}", "automation_changed", "Changed an automation", CONFIG)
_add("DELETE", "/post-call-rules/{rule_id}", "automation_deleted", "Deleted an automation", CONFIG)
for _m, _p, _a, _s in [
    ("POST", "/call-scripts", "call_script_changed", "Created a call script"),
    ("PATCH", "/call-scripts/{script_id}", "call_script_changed", "Edited a call script"),
    ("DELETE", "/call-scripts/{script_id}", "call_script_changed", "Deleted a call script"),
    ("POST", "/flows", "flow_changed", "Created a WhatsApp Flow"),
    ("POST", "/flows/{flow_id}/publish", "flow_changed", "Published a WhatsApp Flow"),
    ("POST", "/flows/{flow_id}/deprecate", "flow_changed", "Retired a WhatsApp Flow"),
    ("POST", "/flows/{flow_id}/refresh", "flow_changed", "Refreshed a WhatsApp Flow"),
    ("POST", "/flows/{flow_id}/enable-live-data", "flow_changed", "Enabled live data on a Flow"),
    ("POST", "/forms", "form_changed", "Created a lead form"),
    ("PATCH", "/forms/{form_id}", "form_changed", "Edited a lead form"),
    ("DELETE", "/forms/{form_id}", "form_changed", "Deleted a lead form"),
    ("POST", "/forms/{form_id}/logo", "form_changed", "Changed a lead form's logo"),
    ("POST", "/templates", "template_created", "Submitted a WhatsApp template to Meta"),
    ("DELETE", "/templates/{template_id}", "template_deleted", "Deleted a WhatsApp template"),
    ("POST", "/capabilities/{key}/templates", "template_created", "Created the templates a feature needs"),
    ("PUT", "/capabilities/{key}", "feature_changed", "Switched a feature on or off"),
    ("PUT", "/queue/settings", "settings_changed", "Changed the walk-in queue settings"),
    ("POST", "/approvals/autonomy", "autonomy_changed", "Changed how much the AI does on its own"),
    ("PATCH", "/approvals/auto-send-rules", "autonomy_changed", "Changed the auto-send rules"),
    ("POST", "/auth/me", "settings_changed", "Changed business or profile details"),
    ("PATCH", "/escalations/settings", "settings_changed", "Changed escalation settings"),
]:
    _add(_m, _p, _a, _s, CONFIG)

# Money, numbers and the business's channels.
for _m, _p, _a, _s in [
    ("POST", "/voice-onboarding/subaccount", "voice_setup", "Set up the voice account"),
    ("POST", "/voice-onboarding/compliance/end-user", "voice_setup", "Submitted voice KYC details"),
    ("POST", "/voice-onboarding/compliance/documents", "voice_setup", "Uploaded a voice KYC document"),
    ("POST", "/voice-onboarding/compliance/application", "voice_setup", "Submitted the voice KYC application"),
    ("POST", "/voice-onboarding/compliance/resubmit", "voice_setup", "Resubmitted the voice KYC application"),
    ("POST", "/voice-onboarding/numbers/buy", "number_bought", "Bought a phone number"),
    ("POST", "/voice-onboarding/numbers/{number}/release", "number_released", "Released a phone number"),
    ("PATCH", "/voice-onboarding/agent-settings", "voice_agent_changed", "Changed the voice agent's settings"),
    ("POST", "/voice-onboarding/number-requests", "voice_setup", "Requested a phone number"),
    ("PATCH", "/voice-onboarding/number-requests/{request_id}", "voice_setup", "Updated a phone number request"),
    ("POST", "/channels/whatsapp/embedded-signup", "channel_changed", "Connected WhatsApp"),
    ("DELETE", "/channels/whatsapp", "channel_changed", "Disconnected WhatsApp"),
    ("POST", "/channels/whatsapp/ad-tracking", "channel_changed", "Changed WhatsApp ad tracking"),
    ("POST", "/channels/whatsapp/catalog-id", "channel_changed", "Changed the WhatsApp catalogue"),
    ("POST", "/channels/whatsapp/payment-config", "channel_changed", "Changed WhatsApp payments"),
    ("POST", "/channels/gmail/backfill", "channel_changed", "Imported old Gmail"),
    ("POST", "/account/whatsapp/profile", "whatsapp_profile_changed", "Changed the WhatsApp business profile"),
    ("POST", "/account/whatsapp/profile/picture", "whatsapp_profile_changed", "Changed the WhatsApp profile photo"),
    ("POST", "/account/whatsapp/request-code", "whatsapp_security", "Requested a WhatsApp verification code"),
    ("POST", "/account/whatsapp/verify-code", "whatsapp_security", "Verified the WhatsApp number"),
    ("POST", "/account/whatsapp/two-step-pin", "whatsapp_security", "Changed the WhatsApp two-step PIN"),
    ("POST", "/migration/whatsapp/start", "whatsapp_migration", "Started moving a WhatsApp number"),
    ("POST", "/migration/whatsapp/request-code", "whatsapp_migration", "Requested a number-move code"),
    ("POST", "/migration/whatsapp/verify-code", "whatsapp_migration", "Verified a number-move code"),
    ("POST", "/migration/whatsapp/finish", "whatsapp_migration", "Finished moving a WhatsApp number"),
    ("POST", "/justdial/token", "lead_source_changed", "Generated a Justdial lead link"),
    ("POST", "/indiamart/token", "lead_source_changed", "Generated an IndiaMART lead link"),
    ("POST", "/lead-sources/{key}/token", "lead_source_changed", "Generated a lead-source link"),
    ("POST", "/email-leads/token", "lead_source_changed", "Generated the email-lead address"),
    ("POST", "/zoho/sync", "integration_changed", "Synced Zoho Books"),
    ("DELETE", "/zoho/connection", "integration_changed", "Disconnected Zoho Books"),
]:
    _add(_m, _p, _a, _s, CONFIG)

# Data leaving, credentials and access.
for _m, _p, _a, _s in [
    ("POST", "/integrations/api-keys", "api_key_created", "Created an API key"),
    ("DELETE", "/integrations/api-keys/{key_id}", "api_key_deleted", "Revoked an API key"),
    ("POST", "/integrations/webhooks", "webhook_changed", "Created a webhook"),
    ("PATCH", "/integrations/webhooks/{webhook_id}", "webhook_changed", "Changed a webhook"),
    ("DELETE", "/integrations/webhooks/{webhook_id}", "webhook_changed", "Deleted a webhook"),
    ("POST", "/integrations/github", "integration_changed", "Connected GitHub"),
    ("DELETE", "/integrations/github", "integration_changed", "Disconnected GitHub"),
    ("POST", "/integrations/email-connection", "integration_changed", "Connected the outbound email sender"),
    ("DELETE", "/integrations/email-connection", "integration_changed", "Disconnected the outbound email sender"),
    ("POST", "/integrations/stripe", "integration_changed", "Connected Stripe"),
    ("DELETE", "/integrations/stripe", "integration_changed", "Disconnected Stripe"),
    ("POST", "/integrations/google-calendar/disconnect", "integration_changed", "Disconnected Google Calendar"),
    ("GET", "/integrations/google-calendar/connect-url", "integration_changed", "Started connecting Google Calendar"),
    ("GET", "/export/customers", "data_export", "Downloaded the customer list"),
    ("GET", "/export/conversations", "data_export", "Downloaded conversations"),
    ("GET", "/ledger/export/tally", "data_export", "Downloaded the Tally export"),
    ("POST", "/team/members", "team_member_added", "Added a team member"),
    ("PATCH", "/team/members/{user_id}", "team_role_changed", "Changed a team member's role"),
    ("DELETE", "/team/members/{user_id}", "team_member_removed", "Removed a team member"),
    ("POST", "/team/members/{user_id}/reset-password", "team_password_reset", "Reset a team member's password"),
]:
    _add(_m, _p, _a, _s, SECURITY)

# Reads worth a row: bulk data leaving. Every other GET is left out.
LOGGED_GETS = {key for key, (_a, _s, kind) in LABELS.items() if key[0] == "GET"}

# POSTs that only ask a question or look something up - not a change.
NOT_ACTIVITY = {
    ("POST", API + "/campaigns/preview"), ("POST", API + "/call-campaigns/preview"),
    ("POST", API + "/post-call-rules/test"), ("POST", API + "/templates/sync"),
    ("POST", API + "/templates/carousel/draft"), ("POST", API + "/commands/understand"),
    ("POST", API + "/owner/ask"), ("POST", API + "/voice-onboarding/preview-voice"),
    # Heartbeat of an open thread, every few seconds - not a decision anyone made.
    ("POST", API + "/conversations/{customer_id}/presence"),
}
NOT_ACTIVITY_PREFIXES = (API + "/push/", API + "/auth/")
# /auth/me is a change; the rest of /auth is signing in and out (recorded by hand).
NOT_ACTIVITY_EXCEPT = {("POST", API + "/auth/me")}

# When a route has no label of its own, its area still says what it is.
SEGMENTS: dict[str, tuple[str, str, str]] = {
    "crm": ("customer_record_changed", "Changed customer records (notes, tags, stages)", WORK),
    "scheduling": ("scheduling_changed", "Changed bookings, staff or availability", WORK),
    "queue": ("queue_changed", "Changed the walk-in queue", WORK),
    "orders": ("order_changed", "Changed orders", WORK),
    "products": ("product_changed", "Changed products", WORK),
    "quotations": ("quotation_changed", "Changed quotations", WORK),
    "insurance-claims": ("claim_changed", "Changed claims", WORK),
    "cases": ("case_changed", "Changed cases", WORK),
    "properties": ("property_changed", "Changed property listings", WORK),
    "ledger": ("ledger_changed", "Changed commitments in the ledger", WORK),
    "receivables": ("ledger_changed", "Imported receivables", CONFIG),
    "canned-responses": ("canned_response_changed", "Changed saved replies", WORK),
    "signals": ("signal_changed", "Updated a signal", WORK),
    "conversations": ("conversation_changed", "Changed a conversation", WORK),
    "messages": ("message_sent", "Sent a message", WORK),
    "approvals": ("approval_changed", "Changed the approvals queue", WORK),
    "commands": ("owner_command", "Ran an owner command", CONFIG),
    "team": ("team_changed", "Changed the team", SECURITY),
    "capabilities": ("feature_changed", "Changed features", CONFIG),
}

_VERBS = {"POST": "add or change", "PUT": "edit", "PATCH": "edit", "DELETE": "delete"}


def describe(method: str, route_path: str) -> tuple[str, str, str]:
    """
    (action, summary, kind) for a route. Always answers: a route nobody
    labelled falls back to its area, then to the route itself - so it is
    logged, readably if not prettily.
    """
    method = method.upper()
    hit = LABELS.get((method, route_path))
    if hit:
        return hit
    parts = route_path.removeprefix(API).strip("/").split("/")
    segment = parts[0] if parts and parts[0] else ""
    if segment in SEGMENTS:
        action, summary, kind = SEGMENTS[segment]
        return action, f"{summary} ({_VERBS.get(method, method.lower())})", kind
    return f"{method.lower()}:{route_path.removeprefix(API)}", f"{method} {route_path.removeprefix(API)}", CONFIG


def _build_action_kinds() -> dict[str, str]:
    kinds = {action: kind for (action, _summary, kind) in LABELS.values()}
    kinds.update({action: kind for (action, _summary, kind) in SEGMENTS.values()})
    kinds["login"] = SECURITY
    return kinds


_ACTION_KINDS = _build_action_kinds()


def kind_of(action: str) -> str:
    """work | config | security. An unlabelled action counts as config - when unsure, show it."""
    return _ACTION_KINDS.get(action, CONFIG)


def actions_of_kind(kind: str) -> list[str]:
    return sorted(a for a, k in _ACTION_KINDS.items() if k == kind)


def should_log(method: str, route_path: str) -> bool:
    method = method.upper()
    key = (method, route_path)
    if method in ("GET", "HEAD", "OPTIONS"):
        return key in LOGGED_GETS
    if key in NOT_ACTIVITY:
        return False
    if key in NOT_ACTIVITY_EXCEPT:
        return True
    if any(route_path.startswith(prefix) for prefix in NOT_ACTIVITY_PREFIXES):
        return False
    return route_path.startswith(API)


# Client errors that mean "nothing happened" - not worth a row.
_SKIP_STATUS = {401, 404, 405, 422, 429}


def outcome_for(status_code: int | None) -> str | None:
    """ok | denied | failed, or None if the request is not worth recording."""
    if status_code is None or status_code in _SKIP_STATUS:
        return None
    if status_code == 403:
        return "denied"
    if status_code >= 400:
        return "failed"
    return "ok"


# ── Writing ─────────────────────────────────────────────────────────────────

def label_for(user) -> str:
    return (
        getattr(user, "full_name", None)
        or getattr(user, "email", None)
        or getattr(user, "phone", None)
        or getattr(user, "username", None)
        or "Unknown"
    )[:255]


async def write(
    *, business_id: uuid.UUID, user_id: uuid.UUID | None, user_label: str, role: str | None,
    action: str, summary: str, outcome: str, method: str = "", path: str = "",
    status_code: int | None = None, detail: dict | None = None,
    ip: str | None = None, user_agent: str | None = None,
) -> None:
    """Write one row in its own transaction. Never raises - auditing must not break the request."""
    from shared.db.models import ActivityLog
    from shared.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            db.add(ActivityLog(
                business_id=business_id, user_id=user_id, user_label=user_label, role=role,
                action=action, summary=summary[:300], outcome=outcome, method=method,
                path=path[:200], status_code=status_code, detail=detail or {},
                ip=ip, user_agent=user_agent, occurred_at=datetime.now(timezone.utc),
            ))
            await db.commit()
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("could not write activity log action=%s business=%s", action, business_id)


def add_login(db, *, user, business, role: str | None) -> None:
    """
    Record a sign-in on the caller's own session, so the row commits with the
    session it belongs to. Silent if there is no business to attach it to.
    """
    if business is None:
        return
    from shared.db.models import ActivityLog

    meta = current_meta()
    db.add(ActivityLog(
        business_id=business.id, user_id=user.id, user_label=label_for(user), role=role,
        action="login", summary="Signed in", outcome="ok", method="POST", path="/auth/login",
        status_code=200, detail={},
        ip=meta.ip if meta else None, user_agent=meta.user_agent if meta else None,
        occurred_at=datetime.now(timezone.utc),
    ))

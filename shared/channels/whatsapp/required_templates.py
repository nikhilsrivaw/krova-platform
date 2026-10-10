"""
The WhatsApp templates each feature quietly depends on.

Switching on Appointments, the walk-in queue or Orders does not, by itself,
send anyone a reminder: every proactive message goes out as an APPROVED
template with a fixed name (shared/scheduling/notify.py), and when the
business has none, the send is skipped with an info-level log line. Nothing
fails, nothing is shown - the feature just looks switched on and does nothing.

This is the list that closes that gap. For each template: which capability
needs it, what each {{number}} stands for (the exact order the sender passes
its values in), and wording that fits those values. The Features screen uses
it to show what is missing, and to submit the missing ones in one click.

Two kinds of entry:
  one_click  the wording and variables are fully determined by the sender,
             so KROVA can submit it for the owner.
  manual     something the sender needs that cannot be created from here, so
             it is listed (the owner must know it is required) but never
             submitted - `note` says why.

Wording rules that matter to Meta's reviewer, kept here on purpose:
  * a body never starts or ends with a variable;
  * UTILITY only for what is genuinely transactional - reminders,
    confirmations, delivery problems. Cart recovery, repeat-purchase and
    upgrade nudges are promotions and are filed as MARKETING, which Meta
    always charges for. Filing them as UTILITY to save the owner money would
    be dishonest and gets templates re-categorised or rejected.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.channels.whatsapp import template_service
from shared.channels.whatsapp import templates as meta
from shared.db.models import MessageTemplate


@dataclass(frozen=True, slots=True)
class RequiredTemplate:
    name: str
    capability: str
    category: str  # "UTILITY" | "MARKETING"
    body: str
    # What {{1}}, {{2}}, ... stand for, in the order the sender fills them.
    variables: tuple[str, ...]
    # A realistic sample for each variable - Meta rejects a template without.
    examples: tuple[str, ...]
    purpose: str
    one_click: bool = True
    note: str | None = None
    language: str = "en"

    def draft(self) -> meta.TemplateDraft:
        return meta.TemplateDraft(
            name=self.name,
            category=self.category,  # type: ignore[arg-type]
            body=self.body,
            language=self.language,
            examples={str(i): example for i, example in enumerate(self.examples, start=1)},
        )


CATALOGUE: tuple[RequiredTemplate, ...] = (
    # ── scheduling ──────────────────────────────────────────────────────────
    RequiredTemplate(
        "appointment_confirmed", "scheduling", "UTILITY",
        "Your appointment with {{1}} is confirmed for {{2}}.",
        ("Staff member's name", "Date and time"),
        ("Dr. Sharma", "Monday, 12 May at 10:00 AM"),
        "Sent when an appointment is booked.",
    ),
    RequiredTemplate(
        "appointment_reminder", "scheduling", "UTILITY",
        "Reminder: your appointment with {{1}} is {{2}}.",
        ("Staff member's name", "Date and time"),
        ("Dr. Sharma", "Monday, 12 May at 10:00 AM"),
        "Sent shortly before an appointment.",
    ),
    # ── walk-in queue ───────────────────────────────────────────────────────
    RequiredTemplate(
        "queue_checkin_confirmation", "opd_queue", "UTILITY",
        "You're number {{1}} in line at {{2}}. We'll message you as your turn gets close.",
        ("Queue number", "Business name"),
        ("12", "Sharma Clinic"),
        "Sent when someone joins the queue.",
    ),
    RequiredTemplate(
        "queue_turn_near", "opd_queue", "UTILITY",
        "You're number {{1}} in line - about {{2}} people ahead of you at {{3}}. Your turn is coming up soon.",
        ("Queue number", "People ahead", "Business name"),
        ("12", "3", "Sharma Clinic"),
        "Sent when their turn is close.",
    ),
    # ── claims ──────────────────────────────────────────────────────────────
    RequiredTemplate(
        "claim_status_update", "tpa_claim_tracking", "UTILITY",
        "Update on your claim with {{1}}: it {{2}}.",
        ("Insurer or manufacturer", "Status phrase, e.g. \"has been approved\""),
        ("Star Health", "has been approved"),
        "Sent when a claim's status changes.",
    ),
    # ── recall ──────────────────────────────────────────────────────────────
    RequiredTemplate(
        "recall_reminder", "care_recall", "UTILITY",
        "Reminder from {{1}}: it's time for your scheduled follow-up. Reply or call us to book a convenient time.",
        ("Business name",),
        ("Sharma Clinic",),
        "Sent when a promised follow-up falls due. Kept generic on purpose - no condition or drug names.",
    ),
    # ── orders ──────────────────────────────────────────────────────────────
    RequiredTemplate(
        "ndr_reschedule_request", "order_sync", "UTILITY",
        "Hi {{1}}, we tried to deliver your order #{{2}} but missed you. Reply to let us know when you'll be available.",
        ("Customer's name", "Order number"),
        ("Asha", "1042"),
        "Sent when a delivery attempt fails.",
    ),
    RequiredTemplate(
        "abandoned_cart_recovery", "order_sync", "MARKETING",
        "Hi {{1}}, you left something in your cart at {{2}}. Pick up where you left off: {{3}} Need help? Just reply here.",
        ("Customer's name", "Business name", "Checkout link"),
        ("Asha", "Kalyan Sarees", "https://shop.example.com/checkout/abc123"),
        "Sent when someone leaves a cart. A promotion, so Meta charges for it.",
    ),
    RequiredTemplate(
        "repeat_purchase_nudge", "order_sync", "MARKETING",
        "Hi {{1}}, hope you're enjoying your order from {{2}}! Come back any time - we'd love to see you again.",
        ("Customer's name", "Business name"),
        ("Asha", "Kalyan Sarees"),
        "Sent to bring a past buyer back. A promotion, so Meta charges for it.",
    ),
    RequiredTemplate(
        "cod_confirmation", "order_sync", "UTILITY",
        "Hi {{1}}, please confirm your Cash on Delivery order #{{2}} ({{3}}).",
        ("Customer's name", "Order number", "Order amount"),
        ("Asha", "1042", "₹1,499"),
        "Asks a Cash-on-Delivery buyer to confirm before it ships.",
        one_click=False,
        note=(
            "Needs two quick-reply buttons - \"Confirm Order\" and \"Cancel Order\" - whose taps KROVA "
            "reads. Create it in WhatsApp Manager. How the buttons' replies reach KROVA has not been "
            "tested against a live account, so test it with a real order before relying on it."
        ),
    ),
    # ── product feedback loop ───────────────────────────────────────────────
    RequiredTemplate(
        "bug_fixed_notification", "product_feedback", "UTILITY",
        "Hi {{1}}, good news - the issue you reported (\"{{2}}\") has been fixed.",
        ("Customer's name", "What they reported"),
        ("Asha", "Export button not working"),
        "Sent when the GitHub issue behind a promised fix is closed.",
    ),
    RequiredTemplate(
        "onboarding_nudge", "product_feedback", "MARKETING",
        "Hi {{1}}, we noticed you haven't finished setting up yet. Need a hand? Just reply and we'll help.",
        ("Customer's name",),
        ("Asha",),
        "Sent when a trial user stalls. Falls back to email if you have a verified sender.",
    ),
    RequiredTemplate(
        "expansion_nudge", "product_feedback", "MARKETING",
        "Hi {{1}}, it looks like you're getting real value from us. Want to talk about upgrading? Just reply and we'll set it up.",
        ("Customer's name",),
        ("Asha",),
        "Sent when your product reports a usage milestone. Falls back to email.",
    ),
    RequiredTemplate(
        "payment_failed_reminder", "product_feedback", "UTILITY",
        "Hi {{1}}, your last payment didn't go through. You can update your payment method here: {{2}} Reply here if you need help.",
        ("Customer's name", "Link to update payment"),
        ("Asha", "https://billing.example.com/update"),
        "Sent when a Stripe payment fails.",
        one_click=False,
        note=(
            "When Stripe gives no invoice link KROVA sends an empty value, which Meta refuses. "
            "Create it only if your failed payments always carry a link."
        ),
    ),
)

BY_NAME: dict[str, RequiredTemplate] = {t.name: t for t in CATALOGUE}


def for_capability(capability: str) -> list[RequiredTemplate]:
    return [t for t in CATALOGUE if t.capability == capability]


# Best state wins when a name exists in several languages.
_RANK = {"approved": 0, "pending": 1}


async def statuses(db: AsyncSession, business_id: uuid.UUID) -> dict[str, tuple[str, str | None]]:
    """
    For every catalogue name: (state, rejection reason) where state is
    approved | pending | rejected | missing | other. One query for all.
    """
    rows = (
        await db.execute(
            select(MessageTemplate).where(
                MessageTemplate.business_id == business_id,
                MessageTemplate.name.in_(list(BY_NAME)),
            )
        )
    ).scalars().all()

    best: dict[str, tuple[int, str, str | None]] = {}
    for row in rows:
        value = row.status.value if hasattr(row.status, "value") else str(row.status)
        state = value.lower()
        if state not in ("approved", "pending", "rejected"):
            state = "other"
        rank = _RANK.get(state, 2 if state == "other" else 3)
        if row.name not in best or rank < best[row.name][0]:
            best[row.name] = (rank, state, row.rejection_reason)

    return {name: (best[name][1], best[name][2]) if name in best else ("missing", None) for name in BY_NAME}


@dataclass(slots=True)
class CreateReport:
    created: list[str] = field(default_factory=list)
    already_there: list[str] = field(default_factory=list)
    needs_manual: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)


async def create_missing(db: AsyncSession, business_id: uuid.UUID, capability: str) -> CreateReport:
    """
    Submit every one-click template this capability needs that the business
    does not have in any form. A template that exists in any state -
    pending, approved, even rejected - is left alone: resubmitting under the
    same name would only be refused, and a rejected one needs a person to
    read why.

    Each template is its own Meta call, so one refusal does not stop the rest.
    Raises template_service.WhatsAppNotReady if there is no WhatsApp to submit through.
    """
    connection, waba_id = await template_service.get_connection(db, business_id)
    current = await statuses(db, business_id)
    report = CreateReport()

    for required in for_capability(capability):
        if not required.one_click:
            report.needs_manual.append(required.name)
        elif current[required.name][0] != "missing":
            report.already_there.append(required.name)
        else:
            try:
                await template_service.submit(
                    db, business_id=business_id, connection=connection, waba_id=waba_id,
                    draft=required.draft(),
                )
            except meta.TemplateError as exc:
                report.failed.append({"name": required.name, "error": str(exc)})
            else:
                report.created.append(required.name)
    return report

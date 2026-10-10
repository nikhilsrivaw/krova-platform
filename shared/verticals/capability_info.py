"""
What each switchable capability is, in words an owner can act on.

A vertical template declares the capabilities a business starts with
(shared/verticals/__init__.py); this is the other half - the names, the
plain-language promise and the one-time setup of each - so Settings can show
an owner a list they can switch on or off, instead of the list living only in
a JSON file nobody reads.

Not every capability belongs here, on purpose:
  conversation_intelligence  every business has it; there is nothing to switch.
  voice_booking              declared in the frontend's type but referenced by
                             no backend code - it does nothing, so offering a
                             switch for it would be a switch wired to nothing.

Adding a capability to this list is data, not a new code path: the gate
itself is still `verticals.has_capability(...)` wherever the module lives.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapabilityInfo:
    key: str
    label: str
    # One sentence: what turning it on gives the business.
    description: str
    # Concrete things that appear or start working.
    adds: tuple[str, ...]
    # What the owner still has to do once, after switching it on. None if nothing.
    setup: str | None = None


_CAPABILITIES = (
    CapabilityInfo(
        "scheduling",
        "Appointments & staff calendar",
        "Customers book a time with you, and the AI can offer free slots and book them in chat and on calls.",
        ("Scheduling page: staff, weekly hours, the appointment book",
         "Booking, cancelling and rescheduling in conversations",
         "Appointment reminders"),
        "Add at least one staff member and their weekly hours.",
    ),
    CapabilityInfo(
        "opd_queue",
        "Walk-in queue",
        "Walk-ins get a token and a live position, and the AI can add someone to the line.",
        ("Queue page and token check-in", "Kiosk link for the counter"),
        "Open a shift each day you take walk-ins.",
    ),
    CapabilityInfo(
        "quotations",
        "Quotations",
        "Every quote you send is tracked, chased before it goes stale, and closed as won or lost.",
        ("Quotations page", "Follow-up reminders on quotes that go quiet"),
        None,
    ),
    CapabilityInfo(
        "order_sync",
        "Orders & products",
        "Orders from your store flow in, so the AI can answer where an order is and nudge on COD and abandoned carts.",
        ("Orders and Products pages",
         "Order status in AI replies",
         "COD confirmation, abandoned-cart and reorder nudges"),
        "Connect your store so orders start arriving.",
    ),
    CapabilityInfo(
        "photo_product_match",
        "Photo -> product match",
        "A customer sends a photo and the AI replies with the matching product, price and availability.",
        ("Automatic product match on photos customers send",),
        "Works with Orders & products. Add your Meta catalog ID in the Photo -> Product Match card below.",
    ),
    CapabilityInfo(
        "tpa_claim_tracking",
        "Claims & warranty tracking",
        "Track a claim or warranty case with a manufacturer or insurer until it closes.",
        ("Claims page", "Customer updates when a claim's status changes"),
        None,
    ),
    CapabilityInfo(
        "case_tracking",
        "Cases",
        "A case per client with its status and next hearing or step, so the AI answers from what you recorded.",
        ("Cases page",),
        "Record each case yourself - the AI only knows what you enter.",
    ),
    CapabilityInfo(
        "property_listings",
        "Property listings",
        "Your listings are known to the AI, so it quotes real prices and availability.",
        ("Properties page",),
        "Add your listings.",
    ),
    CapabilityInfo(
        "product_feedback",
        "Product feedback loop",
        "For a software product: bug reports, feature requests and usage milestones turn into follow-ups.",
        ("GitHub, outbound email and Stripe dunning integrations in Settings",
         "Onboarding drop-off and expansion nudges",
         "A trust report on the AI's replies"),
        "Connect GitHub / Stripe if you use them.",
    ),
    CapabilityInfo(
        "care_recall",
        "Recall reminders",
        "Customers on a repeat schedule get a reminder before their next visit is due.",
        ("Automatic recall reminders",),
        None,
    ),
)

SWITCHABLE: dict[str, CapabilityInfo] = {c.key: c for c in _CAPABILITIES}

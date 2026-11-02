"""
One business must never read or change another's records. Every API route that loads a record by an id it was given
has to check the record belongs to the signed-in business - a forgotten check is a data leak between customers of
KROVA, and nothing else would catch it.

This scans services/api/routers for `await db.get(Model, some_id)` and fails on any that is not followed (within a few
lines) by a business check. A lookup that is safe for another reason - the id came from a record already checked, the
route is KROVA's own admin panel, the request is signature-verified - is listed in REVIEWED with that reason. A new
line must either do the check or be added here after a person has read it.
"""

import pathlib
import re

ROUTERS = pathlib.Path(__file__).resolve().parents[2] / "services" / "api" / "routers"
LOOKUP = re.compile(r"await db\.get\((\w+),\s*([\w\.\[\]\"']+)\)")
CHECKED = re.compile(r"business_id\s*(!=|==)|\.business\b|_owned|_mine|_get_owned|_load_")
# Models looked up by the signed-in business's own id, or that are not per-business data.
NOT_PER_BUSINESS = {"Business", "User", "Subscription", "Wallet", "BusinessDNA"}

REVIEWED = {
    "admin.py": {
        "row = await db.get(NumberRequest, request_id)": "KROVA staff admin panel - every route needs an admin login",
        "ticket = await db.get(SupportTicket, ticket_id)": "KROVA staff admin panel - every route needs an admin login",
    },
    "analytics.py": {
        "customer = await db.get(Customer, customer_id)": "id taken from this business's own commitments just queried",
    },
    "approvals.py": {
        "customer = await db.get(Customer, draft.customer_id)": "from a draft already checked against the business",
        "source = await db.get(Message, draft.in_reply_to_id)": "from a draft already checked against the business",
    },
    "campaigns.py": {
        "step = await db.get(CampaignStep, step_id)": "checked against a campaign loaded with _owned_campaign",
    },
    "flow_exchange.py": {
        "flow = await db.get(WhatsAppFlow, flow_id)": "Meta request, signature verified before the lookup",
        "customer = await db.get(Customer, send_log.customer_id)": "from the flow's own send log",
    },
    "insurance_claims.py": {
        "customer = await db.get(Customer, claim.customer_id)": "from a claim already checked against the business",
    },
    "knowledge.py": {
        "source = await db.get(Message, d.in_reply_to_id)": "from this business's own drafts just queried",
    },
    "ledger.py": {
        "customer = await db.get(Customer, commitment.customer_id)": "from a commitment already checked against the business",
    },
    "messages.py": {
        "customer = await db.get(Customer, found.customer_id)": "from a message already checked against the business",
    },
    "queue.py": {
        "customer = await db.get(Customer, near.customer_id)": "from a queue entry of this business",
    },
    "quotations.py": {
        "customer = await db.get(Customer, quotation.customer_id)": "from a quotation already checked against the business",
        "customer = await db.get(Customer, revision.customer_id)": "from a revision of a checked quotation",
    },
    "routing_rules.py": {
        "team = await db.get(Team, rule.team_id)": "from a rule already checked against the business",
    },
    "scheduling.py": {
        "doctor = await db.get(Doctor, appointment.doctor_id)": "from an appointment already checked against the business",
    },
    "team_calls.py": {
        "customer = await db.get(Customer, call.customer_id) if call.customer_id else None": "from a call already checked against the business",
    },
    "voice_provisioning.py": {
        "row = await db.get(NumberRequest, request_id)": "platform-admin only route (_require_platform_admin)",
    },
    "webhooks.py": {
        "customer = await db.get(Customer, customer_id)": "internal helper called with a business's own customer id",
        "customer = await db.get(Customer, commitment.customer_id)": "from a commitment of the webhook's business",
    },
}


def _unchecked() -> list[tuple[str, str, int]]:
    found = []
    for path in sorted(ROUTERS.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            m = LOOKUP.search(line)
            if not m:
                continue
            model, key = m.groups()
            if model in NOT_PER_BUSINESS or "business" in key.lower():
                continue
            if CHECKED.search("\n".join(lines[i:i + 6])):
                continue
            found.append((path.name, line.strip(), i + 1))
    return found


def test_every_lookup_by_id_checks_the_business_or_was_reviewed():
    new = [f"{name}:{n}: {code}" for name, code, n in _unchecked() if code not in REVIEWED.get(name, {})]
    assert not new, (
        "These routes load a record by id without checking it belongs to the signed-in business. Add the check "
        "(record.business_id != current_user.business -> 404), or, if it is safe for another reason, add it to "
        "REVIEWED with that reason:\n" + "\n".join(new)
    )


def test_the_reviewed_list_has_nothing_stale():
    current = {(name, code) for name, code, _ in _unchecked()}
    stale = [f"{name}: {code}" for name, codes in REVIEWED.items() for code in codes if (name, code) not in current]
    assert not stale, "Remove these from REVIEWED - the line changed or now has its own check:\n" + "\n".join(stale)

"""
A business's own customers and conversations, out - plain CSV, no vendor
lock-in. Mirrors shared/reports/tally_export.py's own shape (build the
file in memory, return bytes, let the router handle the HTTP response) -
stdlib csv module, not a dependency, since neither export needs anything
a spreadsheet app can't already open.
"""

import csv
import io
import uuid


def build_customers_csv(
    customers: list, identities_by_customer: dict[uuid.UUID, list[tuple[str, str]]]
) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["customer_id", "display_name", "phone", "email", "first_seen_at", "last_contact_at"])
    for customer in customers:
        idents = identities_by_customer.get(customer.id, [])
        phone = next((v for k, v in idents if k == "phone"), "")
        email = next((v for k, v in idents if k == "email"), "")
        writer.writerow([
            str(customer.id), customer.display_name or "", phone, email,
            customer.first_seen_at.isoformat() if customer.first_seen_at else "",
            customer.last_contact_at.isoformat() if customer.last_contact_at else "",
        ])
    return buffer.getvalue().encode("utf-8")


def build_conversations_csv(messages: list) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["customer_id", "channel", "direction", "occurred_at", "content"])
    for message in messages:
        channel = message.channel.value if hasattr(message.channel, "value") else message.channel
        direction = message.direction.value if hasattr(message.direction, "value") else message.direction
        writer.writerow([
            str(message.customer_id), channel, direction,
            message.occurred_at.isoformat() if message.occurred_at else "",
            (message.content or "").replace("\n", " ").strip(),
        ])
    return buffer.getvalue().encode("utf-8")

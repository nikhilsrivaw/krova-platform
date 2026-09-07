"""
A business's own data, out - plain CSV downloads. Kept separate from
ledger.py (financial exports specifically, /ledger/export/tally) since
customers/conversations are a different concern entirely; bolting this
onto /ledger would read as a category error.
"""

from datetime import datetime

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from services.api.dependencies import CurrentUserDep, DbDep
from shared.db.models import Customer, CustomerIdentity, Message
from shared.reports import csv_export

router = APIRouter(prefix="/export", tags=["export"])


@router.get("/customers")
async def export_customers(current_user: CurrentUserDep, db: DbDep) -> Response:
    customers = (
        await db.execute(select(Customer).where(Customer.business_id == current_user.business))
    ).scalars().all()

    identity_rows = (
        await db.execute(
            select(CustomerIdentity.customer_id, CustomerIdentity.kind, CustomerIdentity.value).where(
                CustomerIdentity.business_id == current_user.business
            )
        )
    ).all()
    identities_by_customer: dict = {}
    for customer_id, kind, value in identity_rows:
        kind_value = kind.value if hasattr(kind, "value") else kind
        identities_by_customer.setdefault(customer_id, []).append((kind_value, value))

    csv_bytes = csv_export.build_customers_csv(list(customers), identities_by_customer)
    return Response(
        content=csv_bytes, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=krova_customers.csv"},
    )


@router.get("/conversations")
async def export_conversations(
    current_user: CurrentUserDep, db: DbDep, since: datetime | None = Query(default=None),
) -> Response:
    query = select(Message).where(Message.business_id == current_user.business)
    if since:
        query = query.where(Message.occurred_at >= since)
    query = query.order_by(Message.occurred_at.asc())

    messages = (await db.execute(query)).scalars().all()
    csv_bytes = csv_export.build_conversations_csv(list(messages))
    return Response(
        content=csv_bytes, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=krova_conversations.csv"},
    )

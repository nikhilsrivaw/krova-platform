"""
What each Phase 2 tool does against the real tables. Every query is scoped
to the caller's business. Reads return plain dicts. Writes add rows and let
the caller commit, so one confirm is one transaction.
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.care import ledger_queries
from shared.commands.tools import ToolRefused
from shared.db.models import (
    Appointment, AvailabilityException, Business, Customer, CustomerNote, CustomerTag,
    Doctor, Order, QueueEntry,
)
from shared.db.models.crm import TagStatus

LOCAL_TZ = ZoneInfo("Asia/Kolkata")  # business day for "aaj"; per-business timezone is a later refinement
REPORT_CAP_DAYS = 365


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _local_day_bounds() -> tuple[datetime, datetime]:
    today = datetime.now(LOCAL_TZ).date()
    start = datetime.combine(today, time.min, tzinfo=LOCAL_TZ)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


async def find(db: AsyncSession, business: Business, args: dict[str, Any]) -> dict[str, Any]:
    entity = args["entity"]
    limit = min(int(args.get("limit", 20)), 50)

    if entity == "customers":
        rows = (await db.execute(
            select(Customer).where(Customer.business_id == business.id)
            .order_by(Customer.last_contact_at.desc().nullslast()).limit(limit)
        )).scalars().all()
        return {"entity": entity, "items": [
            {"id": str(c.id), "name": c.display_name, "last_contact_at": _iso(c.last_contact_at)} for c in rows
        ]}

    if entity == "bookings":
        now = datetime.now(timezone.utc)
        rows = (await db.execute(
            select(Appointment).where(
                Appointment.business_id == business.id,
                Appointment.starts_at >= now, Appointment.starts_at < now + timedelta(days=7),
            ).order_by(Appointment.starts_at).limit(limit)
        )).scalars().all()
        return {"entity": entity, "items": [
            {"id": str(a.id), "customer_id": str(a.customer_id), "starts_at": _iso(a.starts_at),
             "status": a.status.value} for a in rows
        ]}

    if entity == "orders":
        rows = (await db.execute(
            select(Order).where(Order.business_id == business.id)
            .order_by(Order.placed_at.desc()).limit(limit)
        )).scalars().all()
        return {"entity": entity, "items": [
            {"id": str(o.id), "order_number": o.order_number, "placed_at": _iso(o.placed_at),
             "status": o.status.value, "total_paise": o.total_paise} for o in rows
        ]}

    raise ToolRefused("Messages ka search abhi nahi chalega.")


async def report(db: AsyncSession, business: Business, args: dict[str, Any]) -> dict[str, Any]:
    name = args["name"]
    days = min(int(args.get("period_days", 30)), REPORT_CAP_DAYS)

    if name == "no_show_summary":
        since = datetime.now(timezone.utc) - timedelta(days=days)
        base = select(func.count(Appointment.id)).where(
            Appointment.business_id == business.id, Appointment.starts_at >= since,
        )
        total = (await db.execute(base)).scalar_one()
        no_show = (await db.execute(base.where(Appointment.status == "no_show"))).scalar_one()
        return {"period_days": days, "total": total, "no_show": no_show,
                "rate_pct": round(100 * no_show / total, 1) if total else 0.0}

    if name == "todays_bookings":
        start, end = _local_day_bounds()
        rows = (await db.execute(
            select(Appointment.status, func.count(Appointment.id)).where(
                Appointment.business_id == business.id,
                Appointment.starts_at >= start, Appointment.starts_at < end,
            ).group_by(Appointment.status)
        )).all()
        by_status = {status.value: count for status, count in rows}
        return {"total": sum(by_status.values()), "by_status": by_status}

    if name == "waitlist":
        start, end = _local_day_bounds()
        waiting = (await db.execute(
            select(func.count(QueueEntry.id)).where(
                QueueEntry.business_id == business.id,
                QueueEntry.created_at >= start, QueueEntry.created_at < end,
            )
        )).scalar_one()
        return {"walk_ins_today": waiting}

    if name == "ledger_summary":
        totals = await ledger_queries.totals(business.id, db)
        return {"owed_to_us_paise": totals.owed_to_us_paise, "owed_by_us_paise": totals.owed_by_us_paise,
                "overdue_they_owe_paise": totals.overdue_they_owe_paise,
                "overdue_they_owe_count": totals.overdue_they_owe_count}

    raise ToolRefused("Ye report abhi list mein nahi hai.")


async def _customer(db: AsyncSession, business: Business, customer_id: str) -> Customer:
    try:
        cid = uuid.UUID(customer_id)
    except ValueError as exc:
        raise ToolRefused("Customer ka id galat hai") from exc
    customer = await db.get(Customer, cid)
    if customer is None or customer.business_id != business.id:
        raise ToolRefused("Ye customer aapke business mein nahi mila")
    return customer


async def create(db: AsyncSession, business: Business, user_id: uuid.UUID, args: dict[str, Any]) -> dict[str, Any]:
    customer = await _customer(db, business, args["customer_id"])
    text = (args.get("text") or "").strip()
    if not text:
        raise ToolRefused("Note ya tag ka text khaali nahi ho sakta")

    if args["entity"] == "note":
        note = CustomerNote(business_id=business.id, customer_id=customer.id, body=text, author_user_id=user_id)
        db.add(note)
        await db.flush()
        return {"entity": "note", "id": str(note.id)}

    if args["entity"] == "tag":
        now = datetime.now(timezone.utc)
        tag = CustomerTag(
            business_id=business.id, customer_id=customer.id, label=text[:60], status=TagStatus.confirmed,
            created_by_user_id=user_id, decided_by_user_id=user_id, decided_at=now,
        )
        db.add(tag)
        await db.flush()
        return {"entity": "tag", "id": str(tag.id), "label": tag.label}

    raise ToolRefused("Booking aur reminder ka create agle phase mein aayega.")


async def update(db: AsyncSession, business: Business, args: dict[str, Any]) -> dict[str, Any]:
    if args["field"] != "display_name":
        raise ToolRefused("Abhi customer ka sirf naam badla ja sakta hai")
    customer = await _customer(db, business, args["record_id"])
    value = args["value"].strip()
    if not value:
        raise ToolRefused("Naam khaali nahi ho sakta")
    customer.display_name = value[:255]
    return {"id": str(customer.id), "display_name": customer.display_name}


async def block_slot(db: AsyncSession, business: Business, args: dict[str, Any]) -> dict[str, Any]:
    doctors = (await db.execute(select(Doctor).where(Doctor.business_id == business.id))).scalars().all()
    if args.get("doctor_id"):
        doctor = next((d for d in doctors if str(d.id) == args["doctor_id"]), None)
        if doctor is None:
            raise ToolRefused("Ye staff/resource aapke business mein nahi mila")
    elif len(doctors) == 1:
        doctor = doctors[0]
    else:
        raise ToolRefused("Kaunse staff ka slot band karna hai, ye batayein")

    try:
        day = date.fromisoformat(args["date"])
        start = time.fromisoformat(args["start"])
        end = time.fromisoformat(args["end"])
    except ValueError as exc:
        raise ToolRefused("Date ya time ka format galat hai (YYYY-MM-DD, HH:MM)") from exc
    if end <= start:
        raise ToolRefused("End time start time ke baad hona chahiye")

    exception = AvailabilityException(
        business_id=business.id, doctor_id=doctor.id, date=day, is_unavailable=True,
        start_time=start, end_time=end, reason=(args.get("reason") or None),
    )
    db.add(exception)
    await db.flush()
    return {"id": str(exception.id), "doctor_id": str(doctor.id), "date": day.isoformat()}

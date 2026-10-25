"""
A call nobody on the "who answers" list could take - kept as a promise, not lost.

When the last step of a number's list rings out (shared/channels/voice/answer.py), and the number's owner chose
"callback" or "voicemail", the caller is told someone will call back. This module makes that true: an open
callback commitment (we owe) goes on the Ledger with a due time, a voicemail's recording link goes on the
customer's notes, and the owner, admins and level-1 people get a push. Nothing here can break the call - it runs
after the caller has already been answered.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from shared.channels.voice import numbers
from shared.db.models import (
    BusinessMember,
    BusinessRole,
    Call,
    ChannelConnection,
    Commitment,
    CommitmentDirection,
    CommitmentKind,
    CommitmentStatus,
    Customer,
    CustomerIdentity,
    CustomerNote,
    IdentityKind,
)
from shared.db.session import AsyncSessionLocal
from shared.identity.normalise import InvalidIdentifier, normalise_phone
from shared.utils.logging import get_logger

logger = get_logger(__name__)

SOURCE = "missed_call"


async def _customer(db, business_id: uuid.UUID, call_id: uuid.UUID | None, caller: str | None) -> Customer | None:
    if call_id is not None:
        call = await db.get(Call, call_id)
        if call is not None and call.customer_id is not None:
            return await db.get(Customer, call.customer_id)
    if not caller:
        return None
    try:
        phone = normalise_phone(caller)
    except InvalidIdentifier:
        return None
    customer_id = (await db.execute(
        select(CustomerIdentity.customer_id).where(
            CustomerIdentity.business_id == business_id, CustomerIdentity.kind == IdentityKind.phone,
            CustomerIdentity.value == phone,
        )
    )).scalar_one_or_none()
    return await db.get(Customer, customer_id) if customer_id else None


def describe(*, who: str, line: str, hours: int, recording_url: str | None, seconds: int) -> str:
    base = f"Call back {who} - they rang {line} and nobody could take the call"
    if recording_url:
        base += f"; they left a {seconds}s voicemail" if seconds > 0 else "; they left a voicemail"
    return base + f" (promised within {hours} hour{'s' if hours != 1 else ''})."


async def promise_callback(
    *, connection_id: uuid.UUID, call_id: uuid.UUID | None, caller: str | None, notify: list[uuid.UUID],
    hours: int, recording_url: str | None = None, recording_seconds: int = 0,
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            connection = await db.get(ChannelConnection, connection_id)
            if connection is None:
                return
            business_id = connection.business_id
            line = f'the "{numbers.label_of(connection)}" line'
            customer = await _customer(db, business_id, call_id, caller)
            who = (customer.display_name if customer and customer.display_name else None) or (f"+{caller}" if caller else "the caller")
            text = describe(who=who, line=line, hours=hours, recording_url=recording_url, seconds=recording_seconds)
            now = datetime.now(timezone.utc)
            if customer is not None:
                ref = str(call_id) if call_id else f"{caller}:{now:%Y%m%d%H%M}"
                try:
                    async with db.begin_nested():
                        db.add(Commitment(
                            business_id=business_id, customer_id=customer.id, direction=CommitmentDirection.we_owe,
                            kind=CommitmentKind.callback, description=text[:500], due_at=now + timedelta(hours=hours),
                            due_at_explicit=True, status=CommitmentStatus.open, confidence=1.0, source_message_ids=[],
                            source_quote="Nobody on the call list picked up"[:300], source_system=SOURCE,
                            external_ref=ref[:255],
                        ))
                except IntegrityError:
                    logger.info("missed-call callback already filed ref=%s", ref)
                if recording_url:
                    db.add(CustomerNote(
                        business_id=business_id, customer_id=customer.id, author_user_id=None,
                        body=f"Voicemail on {line}: {recording_url} (Plivo keeps it for 30 days)"[:2000],
                    ))
            await db.commit()
            await _tell_team(db, business_id, notify, text, customer)
            logger.info("missed call kept business=%s customer=%s voicemail=%s", business_id,
                        customer.id if customer else None, bool(recording_url))
    except Exception:  # noqa: BLE001 - after the caller has hung up there is no one to tell; log it
        logger.exception("could not keep a missed call connection=%s call=%s", connection_id, call_id)


async def _tell_team(db, business_id: uuid.UUID, notify: list[uuid.UUID], text: str, customer: Customer | None) -> None:
    from shared.integrations import web_push

    supervisors = (await db.execute(
        select(BusinessMember.user_id).where(
            BusinessMember.business_id == business_id,
            BusinessMember.role.in_((BusinessRole.owner, BusinessRole.admin)),
        )
    )).scalars().all()
    people = list(dict.fromkeys([*notify, *supervisors]))
    try:
        await web_push.send_to_users(
            db, business_id=business_id, user_ids=people, kind="calls",
            payload={"title": "Missed call - call back", "body": text[:140],
                     "url": f"/app/inbox/{customer.id}" if customer else "/app/today"},
        )
    except Exception:  # noqa: BLE001
        logger.exception("missed-call push failed business=%s", business_id)

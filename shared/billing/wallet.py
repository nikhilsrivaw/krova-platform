"""
The rupee wallet: top-ups in, voice (calls and number rent) out.

Every change is a WalletEntry posted together with the balance, under a row lock,
and keyed by (kind, ref) so running the same charge twice - a retried sweep, a
webhook delivered twice - changes nothing the second time.
"""

import math
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.billing import plans
from shared.db.models import (
    ChannelConnection,
    ConnectionStatus,
    Channel,
    UsageEvent,
    Wallet,
    WalletEntry,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Usage newer than this is left for the next run, so a call still writing its cost rows is not half-billed.
SETTLE_LAG = timedelta(minutes=3)


class InsufficientBalance(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _locked(db: AsyncSession, business_id: uuid.UUID) -> Wallet:
    await db.execute(
        pg_insert(Wallet)
        .values(business_id=business_id, balance_paise=0, calls_settled_through=_now())
        .on_conflict_do_nothing(index_elements=["business_id"])
    )
    row = await db.execute(select(Wallet).where(Wallet.business_id == business_id).with_for_update())
    return row.scalar_one()


async def balance(db: AsyncSession, business_id: uuid.UUID) -> int | None:
    """None = this business has no wallet (it has never topped up)."""
    return (await db.execute(select(Wallet.balance_paise).where(Wallet.business_id == business_id))).scalar_one_or_none()


async def post(
    db: AsyncSession, *, business_id: uuid.UUID, amount_paise: int, kind: str,
    ref: str | None = None, note: str | None = None, allow_negative: bool = True,
) -> WalletEntry | None:
    """Add (+) or take (-) money. Returns None if this (kind, ref) was already posted."""
    wallet = await _locked(db, business_id)
    if ref is not None:
        seen = await db.execute(
            select(WalletEntry.id).where(
                WalletEntry.business_id == business_id, WalletEntry.kind == kind, WalletEntry.ref == ref
            )
        )
        if seen.first() is not None:
            return None
    if not allow_negative and wallet.balance_paise + amount_paise < 0:
        raise InsufficientBalance("Not enough balance in the wallet")
    wallet.balance_paise += amount_paise
    wallet.updated_at = _now()
    entry = WalletEntry(
        business_id=business_id, amount_paise=amount_paise, balance_after_paise=wallet.balance_paise,
        kind=kind, ref=ref, note=note,
    )
    db.add(entry)
    await db.flush()
    return entry


async def settle_voice_usage(db: AsyncSession) -> int:
    """Charge each wallet for the voice usage since last time: real cost + 25%. Returns wallets charged."""
    cutoff = _now() - SETTLE_LAG
    charged = 0
    wallets = (await db.execute(select(Wallet.business_id))).scalars().all()
    for business_id in wallets:
        wallet = await _locked(db, business_id)
        start = wallet.calls_settled_through or cutoff
        if start >= cutoff:
            continue
        cost = (await db.execute(
            select(func.coalesce(func.sum(UsageEvent.krova_cost_paise), 0)).where(
                UsageEvent.business_id == business_id, UsageEvent.channel == "voice",
                UsageEvent.created_at > start, UsageEvent.created_at <= cutoff,
            )
        )).scalar_one()
        wallet.calls_settled_through = cutoff
        if cost and cost > 0:
            await post(
                db, business_id=business_id, amount_paise=-plans.call_charge(int(cost)), kind="calls",
                ref=cutoff.isoformat(), note="Voice calls (cost + 25%)",
            )
            charged += 1
    return charged


def month_ref(now: datetime | None = None) -> str:
    return (now or _now()).strftime("%Y-%m")


async def charge_number_rent(db: AsyncSession, *, business_id: uuid.UUID, number: str) -> bool:
    """This month's rent for one number. True if paid (now or already); False if the wallet cannot cover it."""
    try:
        await post(
            db, business_id=business_id, amount_paise=-plans.NUMBER_RENT_PAISE, kind="number_rent",
            ref=f"{number}:{month_ref()}", note=f"Phone number {number} - rent", allow_negative=False,
        )
    except InsufficientBalance:
        return False
    return True


async def collect_number_rent(db: AsyncSession) -> tuple[int, int]:
    """Monthly sweep: every active voice number pays its rent. Returns (paid_or_already, short_of_money)."""
    rows = (await db.execute(
        select(ChannelConnection).where(
            ChannelConnection.channel == Channel.voice, ChannelConnection.status == ConnectionStatus.active
        )
    )).scalars().all()
    ok = short = 0
    for connection in rows:
        if await balance(db, connection.business_id) is None:
            continue  # a number bought before wallets existed: not billed retroactively
        if await charge_number_rent(db, business_id=connection.business_id, number=connection.external_account_id):
            ok += 1
        else:
            short += 1
            wallet = await _locked(db, connection.business_id)
            if wallet.low_balance_alerted_at is None or wallet.low_balance_alerted_at < _now() - timedelta(hours=24):
                wallet.low_balance_alerted_at = _now()
                await _tell_owner(
                    db, connection.business_id, "Add money to keep your phone number",
                    f"Rent for {connection.external_account_id} (Rs {plans.NUMBER_RENT_PAISE // 100}) could not be taken.",
                )
    return ok, short


async def alert_low_balances(db: AsyncSession) -> int:
    """Ping the owner when the wallet runs low (at most once a day)."""
    sent = 0
    now = _now()
    rows = (await db.execute(select(Wallet).where(Wallet.balance_paise < plans.LOW_BALANCE_PAISE))).scalars().all()
    for wallet in rows:
        if wallet.low_balance_alerted_at and wallet.low_balance_alerted_at > now - timedelta(hours=24):
            continue
        wallet.low_balance_alerted_at = now
        await _tell_owner(
            db, wallet.business_id, "Wallet is low",
            f"Balance Rs {math.floor(wallet.balance_paise / 100)}. Calls stop when it reaches zero.",
        )
        sent += 1
    return sent


async def _tell_owner(db: AsyncSession, business_id: uuid.UUID, title: str, body: str) -> None:
    from shared.db.models import BusinessMember, BusinessRole
    from shared.integrations import web_push

    try:
        ids = (await db.execute(
            select(BusinessMember.user_id).where(
                BusinessMember.business_id == business_id,
                BusinessMember.role.in_((BusinessRole.owner, BusinessRole.admin)),
            )
        )).scalars().all()
        await web_push.send_to_users(
            db, business_id=business_id, user_ids=list(ids),
            payload={"title": title, "body": body, "url": "/app/more"},
        )
    except Exception:
        logger.exception("billing ping failed business=%s", business_id)

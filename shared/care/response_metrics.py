"""
How fast a business actually replies, and whether it costs them money.

The one analytics question this codebase can answer that a WhatsApp tool
and a store platform cannot answer separately: it holds both sides - the
message timestamps AND the orders - on one resolved customer identity. So
"you reply in 3 hours; when you reply in 15 minutes, four times as many
people buy" is a fact derivable here and nowhere else in the stack.

Two modelling decisions worth stating plainly, because both are judgement
calls rather than facts the schema hands over:

1. **There is no Conversation table** - messages are grouped per customer,
   nothing more. So an "ask" is defined here as a *burst*: one or more
   inbound messages with no more than _BURST_GAP between them. A customer
   sending three messages in a row is waiting once, not three times, and
   counting it as three would flatter slow businesses and punish chatty
   customers.

2. **Conversion is attributed at customer level, within a window.** An
   order counts against an ask if it was placed by that customer within
   _CONVERSION_WINDOW of it. That is an approximation - a customer who was
   always going to order gets credited to whatever ask happened to precede
   it - and it is stated in the UI rather than hidden. The alternative
   (true per-conversation attribution) is not available without a
   conversation concept that does not exist.

Deliberately not computed here: anything requiring delivery outcome
(delivered vs RTO). Order *placed* is the outcome this module measures;
whether that order became real money is a different question, answered
once OrderStatus carries a real rto state.
"""

import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Direction, Message, Order

# Inbound messages closer together than this are one ask, not several.
_BURST_GAP = timedelta(minutes=30)

# How long after an ask an order still counts as having followed from it.
_CONVERSION_WINDOW = timedelta(days=7)

# An outbound message further out than this is not a reply to that ask in
# any meaningful sense - the customer has long since given up. Counting it
# would make a business that answers a week later look merely "slow"
# rather than absent.
_MAX_REPLY_LAG = timedelta(days=3)

# Upper bound on messages pulled for one report. A business messaging more
# than this in the window gets a truncated - but still directionally
# honest - answer rather than a timeout.
_MESSAGE_CAP = 50_000

_BUCKETS: tuple[tuple[str, timedelta | None], ...] = (
    ("under_15m", timedelta(minutes=15)),
    ("15m_to_1h", timedelta(hours=1)),
    ("1h_to_4h", timedelta(hours=4)),
    ("4h_to_24h", timedelta(hours=24)),
    ("over_24h", None),
)


@dataclass(slots=True)
class ResponseBucket:
    """One response-speed band, and how often it ended in an order."""

    label: str
    asks: int
    converted: int

    @property
    def conversion_rate(self) -> float | None:
        if self.asks == 0:
            return None
        return self.converted / self.asks


@dataclass(slots=True)
class ChannelResponse:
    channel: str
    answered: int
    median_seconds: int | None


@dataclass(slots=True)
class ResponseMetrics:
    """The full picture for one business over one window."""

    asks: int
    answered: int
    unanswered: int
    median_seconds: int | None
    p90_seconds: int | None
    buckets: list[ResponseBucket] = field(default_factory=list)
    by_channel: list[ChannelResponse] = field(default_factory=list)

    @property
    def answer_rate(self) -> float | None:
        if self.asks == 0:
            return None
        return self.answered / self.asks


@dataclass(slots=True)
class _Ask:
    """One burst of inbound messages awaiting a reply."""

    customer_id: uuid.UUID
    channel: str
    asked_at: datetime
    replied_at: datetime | None = None

    @property
    def wait_seconds(self) -> int | None:
        if self.replied_at is None:
            return None
        return int((self.replied_at - self.asked_at).total_seconds())


def _bucket_for(seconds: int) -> str:
    elapsed = timedelta(seconds=seconds)
    for label, upper in _BUCKETS:
        if upper is None or elapsed < upper:
            return label
    return _BUCKETS[-1][0]


def _percentile(values: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile. Avoids a numpy dependency for two numbers."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _build_asks(rows: list[tuple[uuid.UUID, str, Direction, datetime]]) -> list[_Ask]:
    """
    Walk one business's messages in time order and pair each inbound burst
    with the first outbound that followed it.

    Rows must already be sorted by customer then time - the caller's query
    does that, so this stays a single pass rather than re-sorting per
    customer.
    """
    asks: list[_Ask] = []
    open_ask: _Ask | None = None
    current_customer: uuid.UUID | None = None
    last_inbound_at: datetime | None = None

    for customer_id, channel, direction, occurred_at in rows:
        if customer_id != current_customer:
            # New customer - whatever was open stays unanswered.
            current_customer = customer_id
            open_ask = None
            last_inbound_at = None

        if direction == Direction.inbound:
            continues_burst = (
                open_ask is not None
                and last_inbound_at is not None
                and occurred_at - last_inbound_at <= _BURST_GAP
            )
            if not continues_burst:
                open_ask = _Ask(
                    customer_id=customer_id, channel=channel, asked_at=occurred_at
                )
                asks.append(open_ask)
            last_inbound_at = occurred_at
            continue

        # Outbound: closes the open ask, if one is still genuinely waiting.
        if open_ask is not None and open_ask.replied_at is None:
            if occurred_at - open_ask.asked_at <= _MAX_REPLY_LAG:
                open_ask.replied_at = occurred_at
            open_ask = None

    return asks


async def _converted_customers(
    business_id: uuid.UUID, since: datetime, db: AsyncSession
) -> dict[uuid.UUID, list[datetime]]:
    """When each customer placed orders in (and just after) the window."""
    result = await db.execute(
        select(Order.customer_id, Order.placed_at).where(
            Order.business_id == business_id,
            Order.customer_id.is_not(None),
            Order.placed_at >= since,
        )
    )
    placed: dict[uuid.UUID, list[datetime]] = {}
    for customer_id, placed_at in result.all():
        placed.setdefault(customer_id, []).append(placed_at)
    return placed


async def response_metrics(
    business_id: uuid.UUID, db: AsyncSession, *, days: int = 30
) -> ResponseMetrics:
    """
    First-response speed for one business, and the conversion rate at each
    speed. `days` bounds how far back messages are read.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    result = await db.execute(
        select(
            Message.customer_id, Message.channel, Message.direction, Message.occurred_at
        )
        .where(Message.business_id == business_id, Message.occurred_at >= since)
        .order_by(Message.customer_id, Message.occurred_at)
        .limit(_MESSAGE_CAP)
    )
    rows = [
        (customer_id, channel.value if hasattr(channel, "value") else str(channel), direction, occurred_at)
        for customer_id, channel, direction, occurred_at in result.all()
    ]

    asks = _build_asks(rows)
    if not asks:
        return ResponseMetrics(asks=0, answered=0, unanswered=0, median_seconds=None, p90_seconds=None)

    placed = await _converted_customers(business_id, since, db)

    waits: list[int] = []
    buckets: dict[str, ResponseBucket] = {
        label: ResponseBucket(label=label, asks=0, converted=0) for label, _ in _BUCKETS
    }
    per_channel: dict[str, list[int]] = {}

    for ask in asks:
        seconds = ask.wait_seconds
        if seconds is None:
            continue
        waits.append(seconds)
        per_channel.setdefault(ask.channel, []).append(seconds)

        bucket = buckets[_bucket_for(seconds)]
        bucket.asks += 1
        if any(
            ask.asked_at <= placed_at <= ask.asked_at + _CONVERSION_WINDOW
            for placed_at in placed.get(ask.customer_id, ())
        ):
            bucket.converted += 1

    answered = len(waits)
    return ResponseMetrics(
        asks=len(asks),
        answered=answered,
        unanswered=len(asks) - answered,
        median_seconds=int(statistics.median(waits)) if waits else None,
        p90_seconds=_percentile(waits, 0.9),
        buckets=[buckets[label] for label, _ in _BUCKETS],
        by_channel=[
            ChannelResponse(
                channel=channel,
                answered=len(values),
                median_seconds=int(statistics.median(values)),
            )
            for channel, values in sorted(per_channel.items())
        ],
    )

"""
The three real gaps a research pass found in the widget as first shipped:
no rate limiting (a scripted attacker could run up a business's own Claude
bill), no cap on how long one conversation can run (cost scales with turns,
not just requests), and no DPDP consent notice before a visitor's contact
info is ever collected. None of these are hardening for a hypothetical
attack - each is a documented, real pattern (OWASP's own LLM-app guidance,
DPDP Act 2023's own text) this widget was missing outright.

Deliberately approximate where "real" would mean more infrastructure: the
rate limiter is a fixed window, not a true sliding one, and lives as two
columns on WebWidgetConfig rather than a new table or Redis - this
codebase's own standing preference (see shared/db/queue.py's own docstring
elsewhere) is Postgres over new infrastructure, and a rate limiter does not
need the same correctness guarantee a booking does.
"""

from datetime import datetime, timedelta, timezone

from shared.db.models import WebSession, WebWidgetConfig

# Business-wide (every visitor session combined) - generous for real usage
# from a small clinic/salon's actual traffic, tight enough to block a
# scripted attacker running up their Claude bill.
_RATE_LIMIT_WINDOW = timedelta(minutes=1)
_RATE_LIMIT_MAX_REQUESTS = 20

# Real industry practice found: bound conversation depth, not just request
# rate, since a single long-running conversation is itself a cost driver.
# Counts both user and assistant turns, so this is ~12-13 real exchanges.
TURN_CAP = 25

TURN_CAP_MESSAGE = (
    "I've reached the limit for this conversation - someone from our team "
    "will follow up with you directly. Thanks for your patience!"
)


def check_rate_limit(config: WebWidgetConfig) -> bool:
    """
    True if this request is allowed, mutating config's own counter either
    way - the caller is responsible for persisting it (a plain attribute
    mutation on an already-loaded ORM row, flushed by the caller like any
    other change in the same request).
    """
    now = datetime.now(timezone.utc)
    window_started = config.rate_limit_window_started_at

    if window_started is None or now - window_started > _RATE_LIMIT_WINDOW:
        config.rate_limit_window_started_at = now
        config.rate_limit_count = 1
        return True

    if config.rate_limit_count >= _RATE_LIMIT_MAX_REQUESTS:
        return False

    config.rate_limit_count += 1
    return True


def check_turn_cap(session: WebSession) -> bool:
    """True if this session may still take another turn."""
    return session.turn_count < TURN_CAP


def record_turn(session: WebSession) -> None:
    session.turn_count += 1

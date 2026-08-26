"""
Database engine and session factory.

The engine is built lazily rather than at import.

asyncpg connections belong to the event loop that opened them. An engine
created at import time binds its pool to whichever loop touches it first, and
any later loop inherits connections it cannot use - the symptom is a confident
"database is unreachable" from a database that is perfectly healthy. Services
only ever run one loop so this never bites in production, but it makes the
whole codebase untestable, which is worse than a production bug because it
stops you finding them.

Building on first use, plus reset() for anything that legitimately spans
loops, costs nothing at runtime and keeps the thing testable.
"""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from shared.config.settings import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            # A connection killed while idle - by RDS, a NAT gateway, a laptop
            # sleeping - fails once here instead of on a real query.
            pool_pre_ping=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _sessionmaker


async def reset() -> None:
    """
    Drop the engine so the next use builds a fresh one.

    For tests and scripts that run more than one event loop. Disposing first
    closes the old pool cleanly rather than leaving sockets to be collected.
    """
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def reset_sync() -> None:
    """
    Forget the engine without awaiting disposal.

    Used when the loop that owned the connections is already gone, so there is
    nothing left to await on - the sockets die with their loop.
    """
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


class _LazySessionFactory:
    """Keeps `async with AsyncSessionLocal() as db:` working, built on demand."""

    def __call__(self, **kwargs) -> AsyncSession:
        return get_sessionmaker()(**kwargs)


AsyncSessionLocal = _LazySessionFactory()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. Commits on success, rolls back on any exception."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_db_connection() -> bool:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False

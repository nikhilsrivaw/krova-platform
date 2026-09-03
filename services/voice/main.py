"""
Krova Voice.

A separate process from the API, on purpose. A phone call holds a WebSocket
open for the length of the call and the agent must keep answering inside a
strict latency budget the whole time - deploying that alongside the HTTP API,
which restarts on every code push and shares the same worker pool, would mean
an unrelated API deploy dropping live calls.

It shares everything else: the same Postgres models, the same identity
resolution, the same ingest() every other channel writes through. A caller's
words land in the same customer record as their WhatsApp messages because
this process is a client of the platform's shared code, not a fork of it.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from shared.channels.voice import answer, copilot, outbound, relay
from shared.config.settings import settings
from shared.db.session import check_db_connection, get_engine
from shared.utils.logging import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Krova Voice starting (version=%s)", settings.app_version)

    if not await check_db_connection():
        raise RuntimeError("Cannot start: database is unreachable")

    missing = [
        name
        for name, value in (
            ("PLIVO_AUTH_ID", settings.plivo_auth_id),
            ("PLIVO_AUTH_TOKEN", settings.plivo_auth_token),
            ("SARVAM_API_KEY", settings.sarvam_api_key),
        )
        if not value
    ]
    if missing:
        # Not fatal - the process can still serve /health while waiting on
        # credentials - but every call will fail until these are set, so it
        # is worth saying loudly rather than discovering it on a live call.
        logger.warning(
            "Krova Voice starting WITHOUT: %s - no call will succeed until "
            "these are configured",
            ", ".join(missing),
        )

    logger.info("Krova Voice ready")
    yield

    await get_engine().dispose()
    logger.info("Krova Voice stopped")


app = FastAPI(
    title="Krova Voice",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.is_development else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.is_development else None,
)

app.include_router(answer.router)
app.include_router(relay.router)
app.include_router(copilot.router)
app.include_router(outbound.router)


@app.get("/health", tags=["ops"])
async def health() -> JSONResponse:
    db_ok = await check_db_connection()
    configured = bool(
        settings.plivo_auth_id and settings.plivo_auth_token and settings.sarvam_api_key
    )
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "degraded",
            "version": settings.app_version,
            "database": db_ok,
            "voice_configured": configured,
        },
    )

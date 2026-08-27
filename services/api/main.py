"""
Krova API.

One FastAPI app serving the dashboard, onboarding and channel webhooks.
The workers and the voice service are separate processes that share this
codebase's models - not separate systems with their own idea of a tenant.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from services.api import voice_proxy
from services.api.routers import (
    account,
    analytics,
    approvals,
    auth,
    campaigns,
    cases,
    channels,
    conversations,
    knowledge,
    dashboard,
    flows,
    gmail_channel,
    ledger,
    migration,
    messages,
    onboarding,
    orders,
    properties,
    scheduling,
    signals,
    team,
    templates,
    voice_provisioning,
    webhooks,
)
from shared.config.settings import settings
from shared.db.session import check_db_connection, get_engine
from shared.utils.logging import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(
        "Krova API starting (version=%s environment=%s)",
        settings.app_version,
        settings.environment,
    )

    if not await check_db_connection():
        # Refuse to start rather than serve 500s. A container that exits is
        # visible; one that accepts traffic and fails every request is not.
        raise RuntimeError("Cannot start: database is unreachable")

    # Scheduled work. Token refresh is the one that matters most: without it
    # every client goes silent 60 days after they onboard, each on a different
    # day, with no error anywhere.
    from services.api.scheduler import build as build_scheduler

    scheduler = build_scheduler()
    scheduler.start()
    logger.info(
        "scheduler started - token refresh 03:30 IST, analysis 22:00 IST, "
        "stalled-job reclaim every 5m"
    )

    logger.info("Krova API ready")
    yield

    scheduler.shutdown(wait=False)

    await get_engine().dispose()
    logger.info("Krova API stopped")


app = FastAPI(
    title="Krova API",
    version=settings.app_version,
    lifespan=lifespan,
    # The schema describes every endpoint we have. That is a map for us in
    # development and a map for everyone else in production.
    docs_url="/docs" if settings.is_development else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.is_development else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"] if settings.is_development else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """
    Last resort. Log everything, tell the caller nothing.

    Stack traces and driver errors in a response body describe our schema to
    whoever is probing it.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500, content={"detail": "Something went wrong on our side"}
    )


API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(channels.router, prefix=API_PREFIX)
app.include_router(ledger.router, prefix=API_PREFIX)
app.include_router(templates.router, prefix=API_PREFIX)
app.include_router(account.router, prefix=API_PREFIX)
app.include_router(messages.router, prefix=API_PREFIX)
app.include_router(conversations.router, prefix=API_PREFIX)
app.include_router(approvals.router, prefix=API_PREFIX)
app.include_router(knowledge.router, prefix=API_PREFIX)
app.include_router(campaigns.router, prefix=API_PREFIX)
app.include_router(analytics.router, prefix=API_PREFIX)
app.include_router(migration.router, prefix=API_PREFIX)
app.include_router(voice_provisioning.router, prefix=API_PREFIX)
app.include_router(scheduling.router, prefix=API_PREFIX)
app.include_router(orders.router, prefix=API_PREFIX)
app.include_router(properties.router, prefix=API_PREFIX)
app.include_router(cases.router, prefix=API_PREFIX)
app.include_router(signals.router, prefix=API_PREFIX)
app.include_router(team.router, prefix=API_PREFIX)
app.include_router(gmail_channel.router, prefix=API_PREFIX)
app.include_router(flows.router, prefix=API_PREFIX)

# Webhooks sit at the root, not under the API prefix: the URL is
# registered with Meta and changing it later means reconfiguring every
# connected business.
app.include_router(webhooks.router)

# The onboarding screen sits at the root too: Meta's Embedded Signup
# requires the page that opens the dialog to be on an allowlisted origin,
# and a short path is what gets pasted into their settings.
app.include_router(onboarding.router)
app.include_router(dashboard.router)

# Voice runs as its own real process (services/voice/main.py, port 8100) -
# a call holds its WebSocket open for the call's length and cannot share a
# deploy cycle with the HTTP API. This process only forwards to it: the free
# ngrok plan gives one public domain, and this is the one already wired into
# Plivo/Meta, so the API is what has to sit in front. See voice_proxy.py.
app.include_router(voice_proxy.router)


@app.get("/health", tags=["ops"])
async def health() -> JSONResponse:
    """Liveness plus a real dependency check, for the load balancer."""
    db_ok = await check_db_connection()
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "degraded",
            "version": settings.app_version,
            "environment": settings.environment,
            "database": db_ok,
        },
    )

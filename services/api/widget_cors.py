"""
Dynamic, per-tenant CORS for the website widget only.

main.py's blanket CORSMiddleware is a static allowlist
(krova.space/www.krova.space, fixed at deploy time via an env var) - that
is correct for the dashboard's own app-to-API traffic, and cannot support
a widget embedded on an arbitrary clinic's own domain, unknown until that
business registers it via WebWidgetConfig. This middleware handles CORS
for /api/v1/widget/* only; every other route is untouched, still governed
by the existing blanket CORSMiddleware exactly as before.

No cookies/credentials on these endpoints (the session token travels in
the request body, not a cookie) - so Access-Control-Allow-Credentials is
never needed here, and echoing back the one exact matching origin (never
a wildcard) is the whole safeguard.

Must be registered in main.py AFTER the blanket CORSMiddleware
(app.add_middleware calls wrap outside-in in reverse order - the last one
added runs first) so this middleware's own OPTIONS handling for widget
paths runs before the blanket middleware ever sees them, rather than the
two fighting over the same preflight request.
"""

import re
from urllib.parse import urlparse

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from shared.db.session import AsyncSessionLocal
from shared.utils.logging import get_logger

logger = get_logger(__name__)

_WIDGET_PATH = re.compile(r"^/api/v1/widget/([^/]+)/")


async def _matching_origin(site_key: str, origin: str | None) -> str | None:
    """The exact Origin to echo back, or None if it doesn't match this
    site_key's registered domain - never a wildcard, never a guess."""
    if not origin:
        return None

    from shared.db.models import WebWidgetConfig

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(WebWidgetConfig.allowed_domain).where(
                WebWidgetConfig.site_key == site_key,
                WebWidgetConfig.active.is_(True),
            )
        )
        allowed_domain = result.scalar_one_or_none()

    if not allowed_domain:
        return None

    origin_host = urlparse(origin).hostname
    return origin if origin_host == allowed_domain else None


class WidgetCORSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        match = _WIDGET_PATH.match(request.url.path)
        if not match:
            return await call_next(request)

        site_key = match.group(1)
        origin = request.headers.get("origin")
        allowed = await _matching_origin(site_key, origin)

        if request.method == "OPTIONS":
            # The browser's preflight - answered directly here, never
            # forwarded to a route (there is no OPTIONS handler on the
            # widget router itself, by design - this is the one place
            # that owns it).
            headers = (
                {
                    "Access-Control-Allow-Origin": allowed,
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Max-Age": "600",
                }
                if allowed
                else {}
            )
            return Response(status_code=204, headers=headers)

        response = await call_next(request)
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = allowed
        return response

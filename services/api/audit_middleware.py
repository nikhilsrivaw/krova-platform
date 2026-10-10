"""
Records what each signed-in person does, without any handler having to ask.

A plain ASGI middleware, not Starlette's BaseHTTPMiddleware: it has to see the
route FastAPI matched (to name the action by its path template, not the raw
URL) and the status the app answered with, and it must not buffer or reshape
the response.

Order of events for a request:
  1. note the caller's address and browser (so a sign-in deep in a service can
     record them too);
  2. let the app run - get_current_user fills `state["actor"]` if there is a
     signed-in person;
  3. after the response has gone out, write one row if the request changed
     something (or was a refused attempt, or downloaded data) and had an actor.

Writing happens after the response so it adds no latency, and a failure to
write is logged and swallowed - auditing never breaks a request.
"""

from shared.audit import activity
from shared.utils.logging import get_logger

logger = get_logger(__name__)


def _client_ip(scope) -> str | None:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        # The first hop is the caller as the proxy saw it. It is what the proxy
        # reported, which is all a log can honestly say.
        return forwarded.split(",")[0].strip()[:64] or None
    client = scope.get("client")
    return client[0][:64] if client else None


class ActivityLogMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        meta = activity.begin_request(_client_ip(scope), headers.get("user-agent"))
        status_holder: dict = {}

        async def capture(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        await self.app(scope, receive, capture)

        try:
            await self._record(scope, status_holder.get("status"), meta)
        except Exception:  # noqa: BLE001 - auditing must never break a request
            logger.exception("activity logging failed")

    async def _record(self, scope, status_code, meta) -> None:
        actor = (scope.get("state") or {}).get("actor")
        route = scope.get("route")
        if actor is None or route is None:
            return
        method = scope["method"]
        path = route.path
        if not activity.should_log(method, path):
            return
        outcome = activity.outcome_for(status_code)
        if outcome is None:
            return

        action, summary, _kind = activity.describe(method, path)
        detail = {k: str(v) for k, v in (scope.get("path_params") or {}).items()}
        detail.update(meta.notes)
        if outcome == "denied":
            summary = f"Tried to: {summary[:1].lower()}{summary[1:]} (not allowed)"

        await activity.write(
            business_id=actor["business_id"], user_id=actor["user_id"],
            user_label=actor["label"], role=actor["role"],
            action=action, summary=summary, outcome=outcome, method=method, path=path,
            status_code=status_code, detail=detail, ip=meta.ip, user_agent=meta.user_agent,
        )

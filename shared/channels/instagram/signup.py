"""
Instagram Business Login - connecting a business's own Instagram account.

The flow, per Meta's Instagram API with Instagram Login docs:

  1. The browser completes Meta's authorisation dialog and hands us a
     short-lived code.
  2. We exchange it server-side for a short-lived access token (~1 hour) -
     a different host (api.instagram.com) from every other call here, which
     all run on graph.instagram.com.
  3. We exchange that short-lived token for a long-lived one (60 days) -
     the one actually worth storing.
  4. We read which account was granted (id, username).
  5. We subscribe our app to this account's webhooks explicitly.

Step 5 mirrors the WhatsApp signup's most commonly missed step, on the same
theory: if it is not confirmed, assume it silently didn't happen rather than
assume it was automatic. Unlike WhatsApp's subscribed_apps call, this one has
not been verified against a live account from this environment - the first
real connection is the actual test, and this should be watched closely then.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class SignupError(Exception):
    """Signup could not be completed. The message is shown to the business."""


@dataclass(slots=True)
class GraphCall:
    method: str
    path: str
    status: int


@dataclass(slots=True)
class InstagramSignupResult:
    access_token: str
    token_expires_at: datetime | None
    ig_user_id: str
    username: str | None
    account_type: str | None
    webhook_subscribed: bool
    calls: list[GraphCall] = field(default_factory=list)


async def complete_signup(code: str) -> InstagramSignupResult:
    """Turn the Business Login dialog's authorisation code into a working connection."""
    if not settings.meta_instagram_app_id or not settings.meta_instagram_app_secret:
        raise SignupError("Instagram signup is not configured on this server")

    code = code.strip()
    if not code:
        raise SignupError("No authorisation code was provided")

    calls: list[GraphCall] = []

    def record(method: str, path: str, response: httpx.Response) -> None:
        calls.append(GraphCall(method, path, response.status_code))

    async with httpx.AsyncClient(timeout=25.0) as client:
        # 1 - code -> short-lived token. Note: api.instagram.com, not
        # graph.instagram.com - the one call on a different host.
        short_res = await client.post(
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": settings.meta_instagram_app_id,
                "client_secret": settings.meta_instagram_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": settings.instagram_redirect_uri,
                "code": code,
            },
        )
        record("POST", "/oauth/access_token", short_res)
        if short_res.status_code != 200:
            logger.error("instagram code exchange failed: %s", short_res.text[:300])
            raise SignupError("Could not complete the connection with Meta")

        short_body = short_res.json()
        short_token = short_body.get("access_token")
        ig_user_id = str(short_body.get("user_id") or "")
        if not short_token or not ig_user_id:
            raise SignupError("Meta did not return an access token")

        # 2 - short-lived -> long-lived (60 day) token
        long_res = await client.get(
            f"{settings.instagram_graph_base_url}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.meta_instagram_app_secret,
                "access_token": short_token,
            },
        )
        record("GET", "/access_token", long_res)
        if long_res.status_code != 200:
            logger.error("instagram long-lived exchange failed: %s", long_res.text[:300])
            raise SignupError("Could not finish setting up this connection")

        long_body = long_res.json()
        access_token = long_body.get("access_token")
        if not access_token:
            raise SignupError("Meta did not return a long-lived access token")

        expires_in = long_body.get("expires_in")  # seconds, ~60 days
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            if expires_in
            else None
        )

        auth = {"Authorization": f"Bearer {access_token}"}

        # 3 - which account this actually is
        me_res = await client.get(
            f"{settings.instagram_graph_base_url}/me",
            params={"fields": "user_id,username,account_type"},
            headers=auth,
        )
        record("GET", "/me", me_res)
        me = me_res.json() if me_res.status_code == 200 else {}

        # 4 - subscribe to this account's webhooks explicitly - see module
        # docstring on why this is not assumed to be automatic.
        sub_res = await client.post(
            f"{settings.instagram_graph_base_url}/{ig_user_id}/subscribed_apps",
            params={"subscribed_fields": "messages,comments"},
            headers=auth,
        )
        record("POST", f"/{ig_user_id}/subscribed_apps", sub_res)
        subscribed = sub_res.status_code == 200 and bool(sub_res.json().get("success", False))
        if not subscribed:
            logger.warning(
                "instagram subscribed_apps did not confirm success for account=%s: %s",
                ig_user_id, sub_res.text[:300],
            )

        return InstagramSignupResult(
            access_token=access_token,
            token_expires_at=expires_at,
            ig_user_id=ig_user_id,
            username=me.get("username"),
            account_type=me.get("account_type"),
            webhook_subscribed=subscribed,
            calls=calls,
        )

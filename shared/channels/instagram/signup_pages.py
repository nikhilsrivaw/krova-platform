"""
Instagram messaging via Facebook Login for Business - the older, Page-based
route, as opposed to signup.py's standalone "Instagram API with Instagram
Login" flow.

Kept as a genuinely separate module rather than a branch inside signup.py:
different host (graph.facebook.com throughout, not api.instagram.com /
graph.instagram.com), different credentials (the main app's meta_app_id /
meta_app_secret, the same ones WhatsApp's signup.py already uses - not the
separate Instagram app identity), and a different identifier entirely. The
account that receives messages is identified by its Facebook Page id, not
an Instagram-scoped user id - that Page id is what should appear as
entry.id in the webhook payload, per the long-standing Messenger Platform
convention this route inherits.

The flow:

  1. code -> a short-lived user access token.
  2. Exchange for a long-lived user token (fb_exchange_token) - a Page
     token derived from a long-lived user token does not itself expire,
     which is what makes this worth doing before step 3.
  3. GET /me/accounts - every Page this user manages, each with its own
     Page access token already included in the response.
  4. For each Page, check its instagram_business_account field to find
     which one (if any) has an Instagram Professional account linked.
  5. Subscribe that Page to messages + comments webhooks explicitly, using
     the Page's own access token - the same "do not assume it happened"
     discipline as every other signup module here.
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
class PageSignupResult:
    page_id: str
    page_name: str | None
    page_access_token: str
    # A Page access token derived from a long-lived user token does not
    # expire on its own - null here means exactly that, not "unknown".
    token_expires_at: datetime | None
    ig_business_account_id: str | None
    ig_username: str | None
    webhook_subscribed: bool
    calls: list[GraphCall] = field(default_factory=list)


async def complete_signup(code: str) -> PageSignupResult:
    """Turn the Facebook Login dialog's authorisation code into a working Page connection."""
    if not settings.meta_app_id or not settings.meta_app_secret:
        raise SignupError("Instagram (Facebook Login) signup is not configured on this server")

    code = code.strip()
    if not code:
        raise SignupError("No authorisation code was provided")

    base = settings.graph_base_url
    calls: list[GraphCall] = []

    def record(method: str, path: str, response: httpx.Response) -> None:
        calls.append(GraphCall(method, path, response.status_code))

    async with httpx.AsyncClient(timeout=25.0) as client:
        # 1 - code -> short-lived user token
        token_res = await client.get(
            f"{base}/oauth/access_token",
            params={
                "client_id": settings.meta_app_id,
                "client_secret": settings.meta_app_secret,
                "redirect_uri": settings.instagram_fb_redirect_uri,
                "code": code,
            },
        )
        record("GET", "/oauth/access_token", token_res)
        if token_res.status_code != 200:
            logger.error("instagram (fb login) code exchange failed: %s", token_res.text[:300])
            raise SignupError("Could not complete the connection with Meta")

        short_token = token_res.json().get("access_token")
        if not short_token:
            raise SignupError("Meta did not return an access token")

        # 2 - short-lived -> long-lived user token
        long_res = await client.get(
            f"{base}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": settings.meta_app_id,
                "client_secret": settings.meta_app_secret,
                "fb_exchange_token": short_token,
            },
        )
        record("GET", "/oauth/access_token (exchange)", long_res)
        long_body = long_res.json() if long_res.status_code == 200 else {}
        user_token = long_body.get("access_token") or short_token
        expires_in = long_body.get("expires_in")
        user_token_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            if expires_in else None
        )

        # 3 - every Page this user manages, each with its own Page token
        pages_res = await client.get(
            f"{base}/me/accounts",
            params={"fields": "id,name,access_token", "access_token": user_token},
        )
        record("GET", "/me/accounts", pages_res)
        if pages_res.status_code != 200:
            logger.error("me/accounts failed: %s", pages_res.text[:300])
            raise SignupError("Could not read the Facebook Pages this account manages")

        pages = pages_res.json().get("data", [])
        if not pages:
            raise SignupError(
                "No Facebook Page was shared during login. Please try again and select a Page."
            )

        # 4 - find the first Page with an Instagram Professional account linked.
        # Three different fields can carry this depending on how the link was
        # made - instagram_business_account only covers the original
        # "Instagram business conversion flow"; a link made or switched from
        # Instagram's own Page setting (as most businesses do today) shows up
        # under connected_instagram_account instead, and a legacy Page-backed
        # IG account under connected_page_backed_instagram_account. Checking
        # only the first of these silently misses the other two.
        ig_fields = (
            "instagram_business_account{id,username},"
            "connected_instagram_account{id,username},"
            "connected_page_backed_instagram_account{id,username}"
        )
        chosen_page = None
        ig_account_id = None
        ig_username = None
        for pg in pages:
            ig_res = await client.get(
                f"{base}/{pg['id']}",
                params={"fields": ig_fields, "access_token": pg["access_token"]},
            )
            record("GET", f"/{pg['id']} (instagram account fields)", ig_res)
            if ig_res.status_code != 200:
                continue
            body = ig_res.json()
            ig_data = (
                body.get("instagram_business_account")
                or body.get("connected_instagram_account")
                or body.get("connected_page_backed_instagram_account")
            )
            if ig_data:
                chosen_page = pg
                ig_account_id = ig_data.get("id")
                ig_username = ig_data.get("username")
                break

        if chosen_page is None:
            # The Page-node fields above can lag Meta's own consent screen by
            # a long margin - seen in practice: a Page-Instagram link fully
            # confirmed on every Meta surface (Instagram's own settings, the
            # Page's own settings, the OAuth consent screen's asset picker)
            # while instagram_business_account/connected_instagram_account/
            # connected_page_backed_instagram_account all still returned
            # nothing, for hours. debug_token's granular_scopes is the
            # authoritative record of what this specific OAuth grant actually
            # covered, independent of that lag - fall back to it before
            # giving up.
            debug_res = await client.get(
                f"{base}/debug_token",
                params={
                    "input_token": user_token,
                    "access_token": f"{settings.meta_app_id}|{settings.meta_app_secret}",
                },
            )
            record("GET", "/debug_token", debug_res)
            debug_body = debug_res.json() if debug_res.status_code == 200 else {}
            granular = debug_body.get("data", {}).get("granular_scopes", [])
            logger.info(
                "instagram (fb login) debug_token status=%s granular_scopes=%s raw=%s",
                debug_res.status_code, granular, debug_res.text[:500],
            )
            granted_ig_ids: list[str] = []
            for entry in granular:
                if entry.get("scope") == "instagram_manage_comments":
                    granted_ig_ids = [str(t) for t in (entry.get("target_ids") or [])]
                    break
            logger.info("instagram (fb login) candidate ig ids from granular scopes: %s", granted_ig_ids)

            for candidate_ig_id in granted_ig_ids:
                for pg in pages:
                    user_res = await client.get(
                        f"{base}/{candidate_ig_id}",
                        params={"fields": "username", "access_token": pg["access_token"]},
                    )
                    record("GET", f"/{candidate_ig_id} (via granular scope)", user_res)
                    logger.info(
                        "instagram (fb login) probe ig=%s page=%s status=%s body=%s",
                        candidate_ig_id, pg["id"], user_res.status_code, user_res.text[:300],
                    )
                    if user_res.status_code == 200:
                        chosen_page = pg
                        ig_account_id = candidate_ig_id
                        ig_username = user_res.json().get("username")
                        break
                if chosen_page is not None:
                    break

        if chosen_page is None:
            raise SignupError(
                "None of the Facebook Pages shared have an Instagram Professional "
                "account linked. Link one in Instagram's own settings, then try again."
            )

        page_id = chosen_page["id"]
        page_token = chosen_page["access_token"]

        # 5 - subscribe this Page to messages + comments explicitly
        sub_res = await client.post(
            f"{base}/{page_id}/subscribed_apps",
            params={"subscribed_fields": "messages,comments", "access_token": page_token},
        )
        record("POST", f"/{page_id}/subscribed_apps", sub_res)
        subscribed = sub_res.status_code == 200 and bool(sub_res.json().get("success", False))
        if not subscribed:
            logger.warning(
                "page subscribed_apps did not confirm success for page=%s: %s",
                page_id, sub_res.text[:300],
            )

        return PageSignupResult(
            page_id=page_id,
            page_name=chosen_page.get("name"),
            page_access_token=page_token,
            token_expires_at=user_token_expires_at,
            ig_business_account_id=ig_account_id,
            ig_username=ig_username,
            webhook_subscribed=subscribed,
            calls=calls,
        )

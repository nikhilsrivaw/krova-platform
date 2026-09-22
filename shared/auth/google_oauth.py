"""
Google sign-in/sign-up - identity only, not shared/channels/email/gmail.py.

Deliberately its own module with its own minimal scope. gmail.py needs
gmail.readonly, a RESTRICTED scope requiring Google's app verification and a
security assessment before it can be used by anyone outside the 100 allowed
test users. Signing in only needs openid/email/profile - non-sensitive scopes
that work for any Google account from day one, no verification wait. Sharing
one module between "log in" and "read someone's inbox" would mean login
itself inherits Gmail's review requirement, which is the wrong trade for a
feature every visitor needs on day one.
"""

from urllib.parse import urlencode

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPES = ["openid", "email", "profile"]


class GoogleOAuthError(Exception):
    """Google refused a sign-in request."""


def authorize_url(state: str, redirect_uri: str) -> str:
    # No access_type=offline, no prompt=consent - unlike gmail.py, this never
    # needs a refresh token: a session is minted once, at sign-in, and Krova's
    # own JWT/refresh-token pair takes over from there.
    return f"{AUTH_URL}?" + urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
        }
    )


async def exchange_code(code: str, redirect_uri: str) -> dict:
    """Swap the authorisation code for an access token."""
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code != 200:
        logger.error("google sign-in code exchange failed: %s", response.text[:300])
        raise GoogleOAuthError("Could not complete Google sign-in")
    return response.json()


async def fetch_userinfo(access_token: str) -> dict:
    """{"email": ..., "email_verified": bool, "name": ..., ...}"""
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code != 200:
        logger.error("google userinfo fetch failed: %s", response.text[:300])
        raise GoogleOAuthError("Could not read the Google account's profile")
    return response.json()

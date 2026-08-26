"""
Gmail - the one channel with a past.

This is Krova's sharpest demo, and it exists because of an asymmetry in the
channels. WhatsApp gives no history: you see messages from the moment a
business connects, and never a word from before. Gmail hands over years.

So a business can connect their inbox and, within minutes, be told:

    You made 23 promises in the last 90 days. 7 are overdue.

No WhatsApp-only competitor can ever build that. It is worth leading with.

Two things about the OAuth scope are worth knowing before relying on this.
gmail.readonly is a RESTRICTED scope: Google requires app verification plus a
third-party security assessment before it can be used with the public, because
we store the data on our servers. An app in Testing mode supports 100 test
users without that, which is enough for design partners while verification
runs - but not enough to be self-serve.
"""

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlencode

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Read-only. We never send, delete or modify - the narrowest scope that lets us
# read what was promised, which is also the easiest to justify at review.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]

# Quoted replies and signatures triple the token count and add nothing: the
# promise is in what someone just wrote, not in the thread they quoted.
_QUOTE_MARKERS = re.compile(
    r"^(On .{5,80} wrote:|-{2,}\s*Original Message|_{5,}|From:\s)", re.M
)
_SIGNATURE = re.compile(r"^--\s*$", re.M)


class GmailError(Exception):
    """Gmail refused a request."""


@dataclass(slots=True)
class GmailMessage:
    external_id: str
    thread_id: str
    from_email: str
    from_name: str | None
    to_emails: list[str]
    subject: str | None
    body: str
    occurred_at: datetime
    is_outbound: bool
    raw: dict[str, Any] = field(default_factory=dict)


# ── OAuth ────────────────────────────────────────────────────────────────────

def authorize_url(state: str, redirect_uri: str) -> str:
    """
    Where to send the owner to grant access.

    access_type=offline and prompt=consent together are what produce a refresh
    token. Google returns one only on the first consent, so an app that omits
    prompt=consent works in development and then silently cannot refresh for
    anyone who has connected before.
    """
    return f"{AUTH_URL}?" + urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )


async def exchange_code(code: str, redirect_uri: str) -> dict:
    """Swap the authorisation code for access and refresh tokens."""
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
        logger.error("gmail code exchange failed: %s", response.text[:300])
        raise GmailError("Could not complete the Google connection")

    payload = response.json()
    if not payload.get("refresh_token"):
        # Without this we can read the inbox for an hour and then go blind.
        logger.warning("google returned no refresh_token - re-consent required")
    return payload


async def refresh_access_token(refresh_token: str) -> dict:
    """
    Get a new access token. Google's expire hourly.

    Unlike Meta's, these do not need the user back - the refresh token keeps
    working until revoked.
    """
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code != 200:
        logger.error("gmail token refresh failed: %s", response.text[:300])
        raise GmailError("Google access has expired and must be reconnected")
    return response.json()


# ── Parsing ──────────────────────────────────────────────────────────────────

# Addresses no human reads or replies to. A promise requires two people, so a
# message from one of these can never contain one - yet a real inbox is mostly
# these, and storing them means paying to analyse a bank alert on every signup.
_MACHINE_SENDER = re.compile(
    r"(^|[.\-_+])(no[.\-_]?reply|donot[.\-_]?reply|do[.\-_]?not[.\-_]?reply|"
    r"notification|notifications|alerts?|updates?|mailer|mailer[.\-_]?daemon|"
    r"postmaster|bounce|automated|auto[.\-_]?confirm|newsletter|news|"
    r"marketing|campaign|noreply)([.\-_+]|@|$)",
    re.I,
)

# Whole domains that only ever send machine mail.
_MACHINE_DOMAIN = re.compile(
    r"@(.*\.)?(mailchimp|sendgrid|mandrillapp|sparkpostmail|amazonses|"
    r"bounces\.google|facebookmail|linkedin)\.", re.I
)

# Bulk-sending subdomains: info@emails.payu.in is a robot, info@payu.in is a
# person. The give-away is the subdomain, not the mailbox name - so "info" and
# "hello" stay human, which matters because plenty of small businesses
# correspond from exactly those addresses.
_BULK_SUBDOMAIN = re.compile(
    r"@(emails?|mailer|mailing|send(er|grid)?|notify|notifications|"
    r"alerts?|updates?|campaigns?|newsletters?|marketing|bounce|reply)\.",
    re.I,
)


def is_machine_sender(address: str) -> bool:
    """
    True when this address is a robot.

    Deliberately conservative: a false positive silently drops a real customer's
    email, which is far worse than paying to read one more newsletter. Anything
    ambiguous is treated as human.
    """
    if not address:
        return True
    local = address.split("@", 1)[0]
    return bool(
        _MACHINE_SENDER.search(local)
        or _MACHINE_DOMAIN.search(address)
        or _BULK_SUBDOMAIN.search(address)
    )


def _decode(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="replace"
        )
    except (ValueError, TypeError):
        return ""


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)

    # Inline tags become spaces, so "<b>Tuesday</b>." would otherwise read
    # as "Tuesday ." - odd for the model, and worse for an owner reading it
    # back inside a quote, where it looks like we mangled their email.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([.,!?;:)\]])", r"\1", text)
    text = re.sub(r"([(\[]) +", r"\1", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return text


def _extract_body(payload: dict) -> str:
    """
    Pull readable text out of a MIME tree.

    Plain text is preferred; HTML is stripped only when there is no plain
    alternative. The tree nests arbitrarily - multipart/alternative inside
    multipart/mixed inside multipart/related - so this recurses rather than
    checking the first level and hoping.
    """
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}

        if mime == "text/plain":
            plain.append(_decode(body.get("data")))
        elif mime == "text/html":
            html.append(_decode(body.get("data")))

        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    text = "\n".join(p for p in plain if p).strip()
    if not text:
        text = _strip_html("\n".join(h for h in html if h)).strip()
    return text


def _trim(text: str) -> str:
    """Drop quoted history and signatures - the promise is in what was just written."""
    match = _QUOTE_MARKERS.search(text)
    if match:
        text = text[: match.start()]
    sig = _SIGNATURE.search(text)
    if sig:
        text = text[: sig.start()]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _headers(payload: dict) -> dict[str, str]:
    return {
        (h.get("name") or "").lower(): h.get("value") or ""
        for h in (payload.get("headers") or [])
    }


def parse_message(message: dict, mailbox_email: str) -> GmailMessage | None:
    """
    Turn a Gmail message resource into something the platform understands.

    Returns None when there is nothing readable - an empty calendar invite, a
    bare attachment - rather than storing a blank row.
    """
    payload = message.get("payload") or {}
    headers = _headers(payload)

    from_name, from_email = parseaddr(headers.get("from", ""))
    if not from_email:
        return None

    to_emails = [
        addr
        for _, addr in (
            parseaddr(part) for part in (headers.get("to", "")).split(",")
        )
        if addr
    ]

    body = _trim(_extract_body(payload))
    if not body:
        return None

    # internalDate is Gmail's own receipt time in milliseconds, and is more
    # reliable than the Date header, which the sender controls.
    try:
        occurred = datetime.fromtimestamp(
            int(message.get("internalDate", 0)) / 1000, tz=timezone.utc
        )
    except (TypeError, ValueError):
        occurred = datetime.now(timezone.utc)

    mailbox = mailbox_email.lower()
    return GmailMessage(
        external_id=f"gmail:{message.get('id')}",
        thread_id=message.get("threadId", ""),
        from_email=from_email.lower(),
        from_name=from_name or None,
        to_emails=[t.lower() for t in to_emails],
        subject=headers.get("subject") or None,
        body=body,
        occurred_at=occurred,
        # Sent by the business, not to it. Determines which side of a promise
        # this is - we_owe or they_owe turns on exactly this.
        is_outbound=from_email.lower() == mailbox,
        raw={"id": message.get("id"), "threadId": message.get("threadId"),
             "labelIds": message.get("labelIds"), "headers": headers},
    )


# ── API ──────────────────────────────────────────────────────────────────────

class GmailClient:
    """One mailbox, one access token."""

    def __init__(self, access_token: str, *, timeout: float = 30.0):
        self._token = access_token
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{API_BASE}/{path}", headers=self._headers, params=params or {}
            )
        if response.status_code == 401:
            raise GmailError("Google access token has expired")
        if response.status_code == 429:
            raise GmailError("Gmail rate limit reached")
        if response.status_code != 200:
            logger.error("gmail %s returned %s: %s", path, response.status_code,
                         response.text[:200])
            raise GmailError(f"Gmail returned {response.status_code}")
        return response.json()

    async def profile(self) -> dict:
        """Which mailbox this token belongs to."""
        return await self._get("profile")

    async def list_message_ids(
        self, *, query: str | None = None, page_token: str | None = None,
        limit: int = 100,
    ) -> tuple[list[str], str | None]:
        """
        Ids matching a Gmail search, plus the next page token.

        Ids only: listing is cheap, fetching is not, so the caller decides what
        is worth pulling.
        """
        params: dict[str, Any] = {"maxResults": min(limit, 500)}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token

        payload = await self._get("messages", params)
        ids = [m["id"] for m in (payload.get("messages") or [])]
        return ids, payload.get("nextPageToken")

    async def get_message(self, message_id: str) -> dict:
        return await self._get(f"messages/{message_id}", {"format": "full"})


def backfill_query(days: int, *, exclude_promotions: bool = True) -> str:
    """
    What to pull on first connection.

    Deliberately narrow. An inbox is mostly newsletters, receipts and alerts,
    and none of it contains a promise anyone made to this business. Reading it
    all would cost real money per customer and bury the signal.
    """
    parts = [f"newer_than:{days}d", "-in:chats"]
    if exclude_promotions:
        parts += ["-category:promotions", "-category:social", "-category:forums"]
    return " ".join(parts)

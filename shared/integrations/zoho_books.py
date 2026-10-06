"""
Zoho Books, read-only: connect a business's books and list what they are owed.

Server-based app flow: the browser goes to Zoho to approve, Zoho redirects back
with a one-time code, and that code is exchanged server-side for a refresh
token (the client secret never leaves the server).

Two things here are from Zoho's docs and are confirmed: the India hosts
(accounts.zoho.in, and the API host configured as ZOHO_API_DOMAIN) and the
GET /invoices filters (unpaid, overdue, partially_paid). Two are standard
Zoho Books behaviour but not re-read in this session: the page_context
pagination flag and the Zoho-oauthtoken header. The first live connect is the
check for both.
"""

from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

import httpx

from shared.config.settings import settings

SCOPE = "ZohoBooks.invoices.READ,ZohoBooks.settings.READ"
OPEN_STATUSES = ("unpaid", "overdue", "partially_paid")
PAGE_SIZE = 200
TIMEOUT = 20.0


class ZohoError(Exception):
    """Zoho refused a call. The message is safe to show to a business."""


@dataclass(slots=True)
class OpenInvoice:
    invoice_id: str
    invoice_number: str | None
    customer_name: str
    due_date: date | None
    balance_paise: int


def redirect_uri() -> str:
    if settings.zoho_redirect_uri:
        return settings.zoho_redirect_uri
    return f"{settings.public_base_url.rstrip('/')}/api/v1/zoho/callback"


def _api(path: str) -> str:
    return f"{settings.zoho_api_domain.rstrip('/')}/books/v3/{path.lstrip('/')}"


def build_auth_url(state: str) -> str:
    params = {
        "scope": SCOPE,
        "client_id": settings.zoho_client_id,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "redirect_uri": redirect_uri(),
        "state": state,
    }
    return f"{settings.zoho_accounts_domain.rstrip('/')}/oauth/v2/auth?{urlencode(params)}"


async def exchange_code(code: str) -> str:
    """One-time code from the callback -> refresh token. Returns the refresh token."""
    body = await _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
    })
    token = body.get("refresh_token")
    if not token:
        raise ZohoError("Zoho did not return a refresh token. Try connecting again.")
    return token


async def access_token_from_refresh(refresh_token: str) -> str:
    body = await _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
    token = body.get("access_token")
    if not token:
        raise ZohoError("Zoho did not return an access token. Reconnect Zoho Books.")
    return token


async def _token_request(extra: dict) -> dict:
    data = {
        "client_id": settings.zoho_client_id,
        "client_secret": settings.zoho_client_secret,
        **extra,
    }
    url = f"{settings.zoho_accounts_domain.rstrip('/')}/oauth/v2/token"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(url, data=data)
    body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
    if res.status_code != 200 or body.get("error"):
        raise ZohoError(f"Zoho login failed: {body.get('error') or res.status_code}")
    return body


async def list_organizations(access_token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.get(_api("organizations"), headers=_auth(access_token))
    if res.status_code != 200:
        raise ZohoError(f"Could not read Zoho organizations ({res.status_code})")
    return res.json().get("organizations", [])


async def list_open_invoices(access_token: str, organization_id: str) -> list[OpenInvoice]:
    """Every invoice that still has a balance, across all open statuses and pages."""
    seen: dict[str, OpenInvoice] = {}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for status in OPEN_STATUSES:
            page = 1
            while True:
                res = await client.get(
                    _api("invoices"),
                    headers=_auth(access_token),
                    params={
                        "organization_id": organization_id,
                        "status": status,
                        "page": page,
                        "per_page": PAGE_SIZE,
                    },
                )
                if res.status_code != 200:
                    raise ZohoError(f"Could not read Zoho invoices ({res.status_code})")
                body = res.json()
                for raw in body.get("invoices", []):
                    invoice = parse_invoice(raw)
                    if invoice is not None:
                        seen.setdefault(invoice.invoice_id, invoice)
                if not body.get("page_context", {}).get("has_more_page"):
                    break
                page += 1
    return list(seen.values())


def parse_invoice(raw: dict) -> OpenInvoice | None:
    """One Zoho invoice -> OpenInvoice. None when it has nothing left to collect."""
    invoice_id = str(raw.get("invoice_id") or "").strip()
    balance = raw.get("balance")
    if not invoice_id or balance is None:
        return None
    balance_paise = round(float(balance) * 100)
    if balance_paise <= 0:
        return None
    return OpenInvoice(
        invoice_id=invoice_id,
        invoice_number=raw.get("invoice_number") or None,
        customer_name=(raw.get("customer_name") or "").strip() or "Unknown customer",
        due_date=_parse_date(raw.get("due_date")),
        balance_paise=balance_paise,
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _auth(access_token: str) -> dict:
    return {"Authorization": f"Zoho-oauthtoken {access_token}"}

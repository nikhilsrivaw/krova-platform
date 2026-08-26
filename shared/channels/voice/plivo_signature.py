"""
Proving a request or WebSocket upgrade actually came from Plivo.

Plivo's real v3 scheme (confirmed against the `plivo` SDK's own
`utils/signature_v3.py`, since the docs prose undersells how fiddly this is):
for GET, sign `{scheme}://{host}{path}[?sorted-query].{nonce}`; for POST,
sign the same URL but with a bare `?` appended (even when there is no query
string) followed directly by every POST param's key+value concatenated in
sorted-by-key order (no `=`, no separators), then `.{nonce}`. HMAC-SHA256 over
that string, keyed on the auth token, base64-encoded. Multiple auth tokens on
an account produce comma-separated signatures - any match is accepted.

Without this, anyone who learns the answer/stream URL could open it directly
and feed an agent audio that never touched a real phone call - or worse, an
agent configured to act (autonomy=act) could be made to say anything to
whoever is on the other end of a spoofed connection. The URL is not the
secret; this signature is.
"""

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class InvalidSignature(Exception):
    """The request did not come from Plivo, or was altered in transit."""


def _sorted_query_string(params: dict) -> str:
    parts = []
    for key in sorted(params.keys()):
        value = params[key]
        if isinstance(value, list):
            parts.append("&".join(f"{key}={v}" for v in sorted(value)))
        else:
            parts.append(f"{key}={value}")
    return "&".join(parts)


def _sorted_params_string(params: dict) -> str:
    parts = []
    for key in sorted(params.keys()):
        value = params[key]
        if isinstance(value, list):
            parts.append("".join(f"{key}{v}" for v in sorted(value)))
        else:
            parts.append(f"{key}{value}")
    return "".join(parts)


def _base_url(uri: str, params: dict, empty_post_params: bool) -> str:
    parsed = urlparse(uri)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    merged = dict(params)
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        merged[key] = values
    query_params = _sorted_query_string(merged)
    if query_params or not empty_post_params:
        base += "?" + query_params
    if query_params and not empty_post_params:
        base += "."
    return base


def _expected(
    *, uri: str, nonce: str, auth_token: str, method: str, params: dict
) -> str:
    if method == "GET":
        base_url = _base_url(uri, params, empty_post_params=True)
    else:
        base_url = _base_url(uri, {}, empty_post_params=(len(params) == 0))
        base_url += _sorted_params_string(params)

    signed = f"{base_url}.{nonce}"
    digest = hmac.new(auth_token.encode(), signed.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify(
    *,
    uri: str,
    signature: str | None,
    nonce: str | None,
    method: str = "GET",
    params: dict | None = None,
) -> None:
    """
    Verify a Plivo-signed request. Raises InvalidSignature on any failure.

    `uri` must be the exact URL Plivo was told to call (public domain +
    path) - not the internal request path, since the request may arrive
    through a proxy that rewrites scheme/host. `params` must be every POST
    form field for a POST request; omit for GET/WebSocket upgrades that
    carry no body.
    """
    if not settings.plivo_auth_token:
        raise InvalidSignature("PLIVO_AUTH_TOKEN is not configured")

    if not signature or not nonce:
        logger.warning("plivo signature rejected: missing header(s)")
        raise InvalidSignature("Signature or nonce header missing")

    expected = _expected(
        uri=uri,
        nonce=nonce,
        auth_token=settings.plivo_auth_token,
        method=method,
        params=params or {},
    )

    candidates = [c.strip() for c in signature.split(",")]
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        logger.warning("plivo signature mismatch - possible spoofed request")
        raise InvalidSignature("Signature does not match")

"""
Sending through the WhatsApp Cloud API.

Per Meta's reference: POST /{phone-number-id}/messages, bearer token, JSON
body. Simple enough. What makes this file worth reading is everything around
the send.

The 24-hour window. A business may send free-form messages only within 24
hours of the customer's last message. Outside it, only an approved template
will deliver - a free-form send is rejected. This is the rule that decides
whether a follow-up reaches anyone, so it is checked here rather than left
for the caller to remember.

Cost follows category, not effort. Service messages inside the window are
free; utility templates are cheap; marketing templates always cost. An agent
that reaches for the right one is the difference between a platform that
saves a business money and one that quietly spends it.

Errors are Meta's, and they are specific. 131047 means the window closed.
131026 means the number cannot receive. Mapping them to something an owner
can act on is most of the value.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Meta's free-form messaging window, measured from the customer's last message.
SERVICE_WINDOW = timedelta(hours=24)

TemplateCategory = Literal["MARKETING", "UTILITY", "AUTHENTICATION"]

# The errors worth explaining rather than echoing.
_ERROR_MEANINGS = {
    131047: "The 24-hour window has closed - only an approved template will deliver now",
    131026: "That number cannot receive WhatsApp messages",
    131051: "Unsupported message type",
    131056: "Too many messages to this number too quickly",
    132000: "Template parameter count does not match the template",
    132001: "That template does not exist, or is not approved for this language",
    132005: "Template text was edited after approval and must be re-approved",
    133016: "This number is rate limited on registration - wait before retrying",
    368: "This number is temporarily blocked for policy violations",
    130472: "The recipient is in an experiment group and did not receive this",
}


class WhatsAppError(Exception):
    """A send that Meta refused."""

    def __init__(self, message: str, *, code: int | None = None, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}

    @property
    def window_closed(self) -> bool:
        """True when the fix is 'use a template', not 'retry'."""
        return self.code == 131047


@dataclass(slots=True)
class SendResult:
    external_id: str      # wamid - matches the status webhooks that follow
    recipient_wa_id: str
    status: str


def within_service_window(last_inbound_at: datetime | None) -> bool:
    """
    Whether a free-form message will deliver right now.

    False when the customer has never written to us: a business cannot open a
    conversation without a template, which is precisely why templates exist.
    """
    if last_inbound_at is None:
        return False
    if last_inbound_at.tzinfo is None:
        last_inbound_at = last_inbound_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_inbound_at < SERVICE_WINDOW


def _explain(payload: dict) -> WhatsAppError:
    error = payload.get("error") or {}
    code = error.get("code")
    data = error.get("error_data") or {}
    detail = data.get("details") or error.get("error_user_msg") or error.get("message", "")
    known = _ERROR_MEANINGS.get(code)
    message = known or detail or "WhatsApp rejected the message"
    if known and detail and detail not in known:
        message = f"{known} ({detail})"
    return WhatsAppError(message, code=code, detail=error)


class WhatsAppClient:
    """
    One client per business, holding that business's own token.

    Never a shared platform token: the whole point of Embedded Signup is that
    each business authorises us against their own account, and the token is
    what carries that boundary.
    """

    def __init__(self, access_token: str, phone_number_id: str, *, timeout: float = 20.0):
        self._token = access_token
        self._phone_number_id = phone_number_id
        self._timeout = timeout

    @property
    def _base(self) -> str:
        return f"{settings.graph_base_url}/{self._phone_number_id}"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base}/{path}", headers=self._headers, json=body
            )
        payload = response.json() if response.content else {}
        if response.status_code != 200:
            error = _explain(payload)
            logger.warning(
                "whatsapp send failed number=%s code=%s: %s",
                self._phone_number_id,
                error.code,
                error,
            )
            raise error
        return payload

    @staticmethod
    def _result(payload: dict) -> SendResult:
        message = (payload.get("messages") or [{}])[0]
        contact = (payload.get("contacts") or [{}])[0]
        return SendResult(
            external_id=message.get("id", ""),
            recipient_wa_id=contact.get("wa_id", ""),
            status=message.get("message_status", "accepted"),
        )

    async def send_text(
        self, to: str, body: str, *, preview_url: bool = False
    ) -> SendResult:
        """
        Send a free-form message. Only delivers inside the 24-hour window.

        Check within_service_window() first - reaching Meta to be told no
        costs a round trip and gives the customer nothing.
        """
        payload = await self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": preview_url, "body": body},
            },
        )
        return self._result(payload)

    async def send_template(
        self,
        to: str,
        template_name: str,
        language: str = "en",
        *,
        body_params: list[str] | None = None,
    ) -> SendResult:
        """
        Send an approved template. Works regardless of the window.

        This is the only way to reach someone who has not written in 24 hours -
        and the category chosen when the template was approved is what it
        costs. Utility is cheap; marketing is not.
        """
        components: list[dict[str, Any]] = []
        if body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in body_params],
                }
            )

        template: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language},
        }
        if components:
            template["components"] = components

        payload = await self._post(
            "messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "template",
                "template": template,
            },
        )
        return self._result(payload)

    async def mark_read(self, message_id: str) -> None:
        """
        Show the customer their message was seen.

        Small courtesy, and it costs nothing. A business that reads but never
        shows a tick feels absent.
        """
        try:
            await self._post(
                "messages",
                {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                },
            )
        except WhatsAppError as exc:
            # Never let a read receipt break message handling.
            logger.debug("could not mark %s read: %s", message_id, exc)

    async def describe_number(self) -> dict:
        """
        Read this number's own settings - quality rating, verified name, limits.

        Quality rating is the early warning nobody watches. It drops before a
        number gets restricted, and by the time messages stop it is too late.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                self._base,
                headers=self._headers,
                params={
                    "fields": "id,display_phone_number,verified_name,"
                    "quality_rating,messaging_limit_tier,platform_type,"
                    "code_verification_status"
                },
            )
        payload = response.json() if response.content else {}
        if response.status_code != 200:
            raise _explain(payload)
        return payload

"""
Every Telnyx-specific HTTP call lives in this file.

This isolation is deliberate: if we move to another provider, this is
the only module that has to change. Nothing outside it knows what
"call_control_id" means or which vendor is carrying the audio.
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TELNYX_API_BASE = "https://api.telnyx.com/v2"


class TelnyxClient:
    """Thin async wrapper over the Telnyx Call Control API."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=TELNYX_API_BASE,
            headers={
                "Authorization": f"Bearer {settings.telnyx_api_key}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    async def _command(
        self,
        call_control_id: str,
        action: str,
        body: dict[str, Any] | None = None,
    ) -> None:
        """Issue one Call Control command against a live call."""
        url = f"/calls/{call_control_id}/actions/{action}"
        try:
            response = await self._client.post(url, json=body or {})
            response.raise_for_status()
            logger.info("command ok action=%s call=%s", action, call_control_id)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "command failed action=%s status=%s body=%s",
                action,
                exc.response.status_code,
                exc.response.text,
            )
            raise

    async def answer(self, call_control_id: str) -> None:
        """Pick up an incoming call."""
        await self._command(call_control_id, "answer")

    async def hangup(self, call_control_id: str) -> None:
        await self._command(call_control_id, "hangup")

    async def speak(
        self,
        call_control_id: str,
        text: str,
        voice: str | None = None,
        language: str | None = None,
    ) -> None:
        """One-off TTS. Used for failure messages, not for the conversation."""
        await self._command(
            call_control_id,
            "speak",
            {
                "payload": text,
                "voice": voice or settings.tts_voice,
                "language": language or settings.language,
            },
        )

    async def start_conversation_relay(
        self,
        call_control_id: str,
        voice: str | None = None,
        language: str | None = None,
    ) -> None:
        """
        Hand the call over to our WebSocket.

        From here Telnyx transcribes the caller and speaks our replies;
        all we exchange is text.
        """
        await self._command(
            call_control_id,
            "conversation_relay_start",
            {
                "url": settings.relay_ws_url,
                "voice": voice or settings.tts_voice,
                "language": language or settings.language,
                "transcription_provider": settings.transcription_provider,
            },
        )

    # --- number provisioning --------------------------------------------
    #
    # These are what make the platform white-label. YOUR account buys the
    # number through the API and routes it to a tenant; the client never
    # sees Telnyx, never logs into it, and never gets billed by it.

    async def search_numbers(
        self,
        country_code: str = "US",
        area_code: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Available numbers your dashboard can offer a client."""
        params: dict[str, Any] = {
            "filter[country_code]": country_code,
            "filter[features][]": "voice",
            "filter[limit]": limit,
        }
        if area_code:
            params["filter[national_destination_code]"] = area_code

        response = await self._client.get("/available_phone_numbers", params=params)
        response.raise_for_status()

        out = []
        for item in response.json().get("data", []):
            cost = item.get("cost_information") or {}
            out.append(
                {
                    "phone_number": item.get("phone_number"),
                    "upfront_cost": cost.get("upfront_cost"),
                    "monthly_cost": cost.get("monthly_cost"),
                    "currency": cost.get("currency"),
                }
            )
        return out

    async def buy_number(
        self, phone_number: str, connection_id: str | None = None
    ) -> dict:
        """
        Purchase a number onto your account.

        `connection_id` is your Call Control Application - attaching it at
        order time means inbound calls hit your webhook immediately, with
        no second configuration step.
        """
        body: dict[str, Any] = {"phone_numbers": [{"phone_number": phone_number}]}
        connection = connection_id or settings.telnyx_connection_id
        if connection:
            body["connection_id"] = connection

        response = await self._client.post("/number_orders", json=body)
        if response.status_code >= 400:
            logger.error(
                "number order failed %s: %s", response.status_code, response.text
            )
            response.raise_for_status()

        data = response.json().get("data", {})
        logger.info("number ordered %s order=%s", phone_number, data.get("id"))
        return data

    async def assign_number_to_app(
        self, phone_number_id: str, connection_id: str
    ) -> None:
        """Point an already-owned number at your Call Control Application."""
        response = await self._client.patch(
            f"/phone_numbers/{phone_number_id}",
            json={"connection_id": connection_id},
        )
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()


telnyx_client = TelnyxClient()

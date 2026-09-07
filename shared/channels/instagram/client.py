"""
Sending Instagram DMs - "Instagram API with Instagram Login" path.

One call: POST /{ig_user_id}/messages on graph.instagram.com, with the
sender's own token and the recipient's Instagram-scoped id (IGSID). There is
no 24-hour-window check here the way shared/channels/whatsapp/client.py has
one - Meta enforces that server-side and returns an error if it's closed,
and duplicating the check client-side would need a working read path to know
when the window opened, which is exactly what isn't available yet for this
account (see the Instagram parked-investigation memory).
"""

from dataclasses import dataclass

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)


class InstagramSendError(Exception):
    """Message could not be sent. The message is shown to the business."""


@dataclass(slots=True)
class SendResult:
    external_id: str


class InstagramClient:
    def __init__(self, access_token: str, ig_user_id: str) -> None:
        self._token = access_token
        self._ig_user_id = ig_user_id

    async def send_text(self, recipient_id: str, text: str) -> SendResult:
        url = f"{settings.instagram_graph_base_url}/{self._ig_user_id}/messages"
        async with httpx.AsyncClient(timeout=25.0) as client:
            res = await client.post(
                url,
                params={"access_token": self._token},
                json={"recipient": {"id": recipient_id}, "message": {"text": text}},
            )
        if res.status_code != 200:
            logger.error(
                "instagram send failed ig_user_id=%s status=%s body=%s",
                self._ig_user_id, res.status_code, res.text[:500],
            )
            raise InstagramSendError(
                f"Meta rejected the message ({res.status_code}): {res.text[:300]}"
            )
        body = res.json()
        external_id = body.get("message_id") or body.get("id") or ""
        if not external_id:
            logger.warning("instagram send returned no message id: %s", body)
        return SendResult(external_id=external_id)

    async def send_private_reply(self, comment_id: str, text: str) -> SendResult:
        """
        Reply privately to a comment - a different Meta contract from
        send_text above, confirmed against developers.facebook.com/docs/
        messenger-platform/instagram/features/private-replies: the
        recipient is `{"comment_id": ...}`, not the commenter's IGSID,
        works only within 7 days of the comment, and Meta allows exactly
        one such reply per comment (a second attempt returns error
        subcode 2534014 - surfaced here as a normal InstagramSendError,
        not specially handled). The 7-day-window check itself lives in
        the caller (shared/channels/send_draft.py), which has the
        comment's timestamp; this method only knows the id.
        """
        url = f"{settings.instagram_graph_base_url}/{self._ig_user_id}/messages"
        async with httpx.AsyncClient(timeout=25.0) as client:
            res = await client.post(
                url,
                params={"access_token": self._token},
                json={"recipient": {"comment_id": comment_id}, "message": {"text": text}},
            )
        if res.status_code != 200:
            logger.error(
                "instagram private reply failed ig_user_id=%s comment_id=%s status=%s body=%s",
                self._ig_user_id, comment_id, res.status_code, res.text[:500],
            )
            raise InstagramSendError(
                f"Meta rejected the private reply ({res.status_code}): {res.text[:300]}"
            )
        body = res.json()
        external_id = body.get("message_id") or body.get("id") or ""
        if not external_id:
            logger.warning("instagram private reply returned no message id: %s", body)
        return SendResult(external_id=external_id)

"""
Sending media out, not reading it in - the opposite direction from media.py.

Two different Meta upload flows exist and a carousel template card needs
both, for two different moments:

  the Resumable Upload API produces a `handle` - Meta's proof it received
  the image, used once, at template-submission time, so a reviewer can see
  the card's picture while judging the template

  the ordinary /PHONE_NUMBER_ID/media endpoint produces a `media_id` - a
  live reference on THIS business's number, used every time the approved
  template is actually sent

A handle from the first flow cannot send a message, and a media_id from the
second cannot appear in a template's review example. Conflating them would
work in testing and fail silently in front of a reviewer or a customer, so
this module keeps them as two distinct, explicitly-named results rather than
one "upload" function that returns something ambiguous.
"""

from dataclasses import dataclass

import httpx

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png"}


class UploadError(Exception):
    """Meta refused the upload. The message is shown to the client."""


@dataclass(slots=True)
class Uploaded:
    header_handle: str   # for a template's HEADER example, at submission time
    media_id: str         # for actually sending, once approved


async def upload_for_carousel_card(
    content: bytes,
    mime_type: str,
    filename: str,
    *,
    access_token: str,
    phone_number_id: str,
    timeout: float = 40.0,
) -> Uploaded:
    """
    Upload one card image both ways, so the card is ready for submission
    and for sending once approved - a business picks a photo once, not twice.
    """
    if mime_type not in ALLOWED_TYPES:
        raise UploadError("Carousel card images must be JPEG or PNG")
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadError(f"Image must be under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")

    if not settings.meta_app_id:
        raise UploadError("Krova's Meta app id is not configured")

    handle = await _resumable_upload(content, mime_type, filename, settings.meta_app_id, access_token, timeout)
    media_id = await _media_upload(content, mime_type, filename, phone_number_id, access_token, timeout)
    return Uploaded(header_handle=handle, media_id=media_id)


async def _resumable_upload(
    content: bytes, mime_type: str, filename: str, app_id: str, access_token: str, timeout: float,
) -> str:
    """
    Meta's two-step Resumable Upload API: start a session, then push the
    bytes into it. The session id it returns is only good for this one file.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        start = await client.post(
            f"{settings.graph_base_url}/{app_id}/uploads",
            params={
                "file_name": filename,
                "file_length": len(content),
                "file_type": mime_type,
                "access_token": access_token,
            },
        )
        if start.status_code != 200:
            logger.warning("resumable upload session failed: %s", start.text[:300])
            raise UploadError("Could not start the upload with Meta")
        session_id = (start.json() or {}).get("id")
        if not session_id:
            raise UploadError("Meta did not return an upload session")

        push = await client.post(
            f"{settings.graph_base_url}/{session_id}",
            headers={
                "Authorization": f"OAuth {access_token}",
                "file_offset": "0",
            },
            content=content,
        )
        if push.status_code != 200:
            logger.warning("resumable upload push failed: %s", push.text[:300])
            raise UploadError("Could not finish the upload with Meta")
        handle = (push.json() or {}).get("h")
        if not handle:
            raise UploadError("Meta did not return an upload handle")
        return handle


async def _media_upload(
    content: bytes, mime_type: str, filename: str, phone_number_id: str, access_token: str, timeout: float,
) -> str:
    """The ordinary outbound-media endpoint, for a media_id an approved send can use."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{settings.graph_base_url}/{phone_number_id}/media",
            headers={"Authorization": f"Bearer {access_token}"},
            data={"messaging_product": "whatsapp"},
            files={"file": (filename, content, mime_type)},
        )
    if response.status_code != 200:
        logger.warning("outbound media upload failed: %s", response.text[:300])
        raise UploadError("Could not upload the image to WhatsApp")
    media_id = (response.json() or {}).get("id")
    if not media_id:
        raise UploadError("Meta did not return a media id")
    return media_id

"""
Hosting a business's own media at a public URL.

Instagram's content-publish API (shared/channels/instagram/client.py's
create_media_container) does not take an uploaded file - it takes a URL
and fetches it itself, server-side, from the public internet. Krova has
nowhere that already serves a business's media at a stable public
address, so this module is that: one S3 bucket, objects readable by
anyone with the link (see the bucket policy set up alongside this), never
listable, keyed by a random id so a guessed/incremented URL never finds
someone else's upload.

boto3 is synchronous - every call here runs in a thread via
asyncio.to_thread so it never blocks the event loop the rest of this
async codebase runs on.
"""

import asyncio
import mimetypes
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png",
    "video/mp4", "video/quicktime",
}


class MediaStorageError(Exception):
    """Could not host this media. The message is shown to the business."""


def _client():
    if not (settings.aws_access_key_id and settings.aws_secret_access_key and settings.aws_s3_bucket):
        raise MediaStorageError("Media hosting is not configured on this server")
    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_s3_region or None,
    )


def _put(key: str, content: bytes, content_type: str) -> None:
    client = _client()
    try:
        client.put_object(
            Bucket=settings.aws_s3_bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error("S3 upload failed key=%s: %s", key, exc)
        raise MediaStorageError("Could not upload this file") from exc


async def upload_media(content: bytes, content_type: str) -> str:
    """
    Store one file, return the public URL Meta (or anyone else) can fetch
    it from. Never overwrites - the key is always a fresh random id.
    """
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise MediaStorageError(f"Unsupported file type: {content_type}")
    if len(content) > MAX_UPLOAD_BYTES:
        raise MediaStorageError(f"File too large - max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")

    extension = mimetypes.guess_extension(content_type) or ""
    key = f"instagram-publish/{uuid.uuid4().hex}{extension}"

    await asyncio.to_thread(_put, key, content, content_type)

    logger.info("uploaded media key=%s bytes=%s content_type=%s", key, len(content), content_type)
    return f"https://{settings.aws_s3_bucket}.s3.{settings.aws_s3_region}.amazonaws.com/{key}"

"""
Reading what customers send, not just storing it.

A customer photographs an invoice and asks "is this right?". Without this,
Krova records a message with no text - the extractor finds nothing, the agent
has nothing to answer with, and the most information-dense message in the
conversation is the one we understand least.

So media is downloaded and then read. The image goes to Claude, which
describes what is in it, and that description becomes the message's content -
which means commitment extraction, the agent and the customer timeline all
work on photographs without knowing anything about images.

Two constraints from Meta shape the timing:

  media URLs expire after 5 minutes, so the two steps happen together
  webhook media ids are downloadable for 7 days, not indefinitely

Both mean this cannot be deferred. A photograph left unread for a week is
gone.
"""

import base64
from dataclasses import dataclass

import httpx

from shared.ai import client as ai
from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

# Meta's ceilings, and Claude's. Images are what matter here; a 16 MB video is
# not something we can usefully read yet.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
READABLE_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# Documents are downloaded but only text-like ones are read. A PDF needs a
# parser; saying so plainly beats silently storing an unreadable blob.
READABLE_TEXT_TYPES = {"text/plain", "text/csv"}


class MediaError(Exception):
    """Media could not be fetched or read."""


@dataclass(slots=True)
class Media:
    media_id: str
    mime_type: str
    size_bytes: int
    content: bytes
    sha256: str | None = None


DESCRIBE_SYSTEM = """You read images a customer has sent to a business on \
WhatsApp.

Describe what the image contains, factually and in full. If it contains \
text - an invoice, a receipt, a prescription, a screenshot, a handwritten \
note - transcribe the text accurately, including any amounts, dates, names \
and reference numbers.

Amounts and dates matter most. A business reading your description should be \
able to act on it without opening the image.

Do not interpret, advise or speculate. If the image is unclear or you cannot \
read part of it, say so plainly rather than guessing - an invented figure is \
far worse than an admitted gap."""

CATALOG_MATCH_SYSTEM = """You are matching a photo a customer sent on \
WhatsApp against a business's own real, current product catalog, to work \
out which product it shows.

You are given the photo and a list of the business's actual products right \
now - name, price, availability, and a retailer_id you can reference.

Describe what the photo shows, then state clearly which catalog product (if \
any) it matches - by its exact name and retailer_id from the list given, \
never a product not in that list.

Never state a price or availability you were not given in the catalog list \
- an invented price is worse than admitting no confident match. If nothing \
in the catalog matches confidently, say so plainly rather than guessing the \
closest one."""


async def fetch(media_id: str, access_token: str, phone_number_id: str | None = None) -> Media:
    """
    Download one piece of media.

    Two calls, deliberately back to back: Meta's media URL expires after five
    minutes, so fetching the URL and then downloading later does not work.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"phone_number_id": phone_number_id} if phone_number_id else {}

    async with httpx.AsyncClient(timeout=40.0) as client:
        meta_response = await client.get(
            f"{settings.graph_base_url}/{media_id}", headers=headers, params=params
        )
        if meta_response.status_code != 200:
            logger.warning(
                "media lookup failed id=%s: %s", media_id, meta_response.text[:200]
            )
            raise MediaError("Could not find that media on WhatsApp")

        info = meta_response.json()
        url = info.get("url")
        if not url:
            raise MediaError("WhatsApp returned no download URL")

        # The download needs the token too - omitting it fails, and the error
        # does not say why.
        download = await client.get(url, headers=headers)
        if download.status_code != 200:
            raise MediaError("Could not download that media")

    return Media(
        media_id=media_id,
        mime_type=info.get("mime_type", ""),
        size_bytes=int(info.get("file_size", len(download.content))),
        content=download.content,
        sha256=info.get("sha256"),
    )


def _catalog_products_text(catalog_products: list[dict]) -> str:
    lines = []
    for p in catalog_products:
        parts = [f"name: {p.get('name')}", f"retailer_id: {p.get('retailer_id')}"]
        if p.get("price"):
            parts.append(f"price: {p['price']}")
        if p.get("availability"):
            parts.append(f"availability: {p['availability']}")
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


async def describe(media: Media, catalog_products: list[dict] | None = None) -> str | None:
    """
    Turn an image into text the rest of the platform can work with.

    This is what makes a photographed invoice into a commitment. The
    description becomes the message's content, so extraction, the agent and
    the timeline all handle photographs without special-casing them.

    `catalog_products`, when given, switches the prompt to product-matching
    (the photo is matched against a business's own real, live catalog
    instead of just described) - the caller (media.read) decides when
    that's appropriate; this function stays a plain, direct branch either
    way, not two functions duplicating the fetch/encode/error-handling
    around them.
    """
    if media.mime_type not in READABLE_IMAGE_TYPES:
        return None
    if media.size_bytes > MAX_IMAGE_BYTES:
        logger.info("image too large to read: %s bytes", media.size_bytes)
        return None

    encoded = base64.standard_b64encode(media.content).decode()

    if catalog_products:
        system = CATALOG_MATCH_SYSTEM
        prompt_text = (
            f"The business's current catalog:\n{_catalog_products_text(catalog_products)}\n\n"
            "Which product does this photo show, if any?"
        )
    else:
        system = DESCRIBE_SYSTEM
        prompt_text = "What is in this image? Transcribe any text."

    try:
        completion = await ai.complete(
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media.mime_type,
                                "data": encoded,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt_text,
                        },
                    ],
                }
            ],
            speed="deep",
            max_tokens=1024,
        )
    except ai.AIError:
        logger.warning("could not read image %s", media.media_id)
        return None

    text = completion.text.strip()
    return text or None


def readable_text(media: Media) -> str | None:
    """Text documents can be read directly, without a model."""
    if media.mime_type not in READABLE_TEXT_TYPES:
        return None
    try:
        return media.content.decode("utf-8", errors="replace").strip()[:8000] or None
    except Exception:
        return None


async def read(
    media_id: str,
    access_token: str,
    *,
    phone_number_id: str | None = None,
    catalog_products: list[dict] | None = None,
) -> tuple[str | None, dict]:
    """
    Fetch a piece of media and return what it says, plus what it was.

    Never raises for an unreadable file. A PDF we cannot parse should leave
    the message stored with an honest note, not fail the whole webhook and
    make Meta retry it.

    `catalog_products`, when given, is passed straight to describe() - see
    its own docstring. The caller (services/api/routers/webhooks.py)
    decides whether this business/message qualifies; this function doesn't
    know or care why the list is or isn't there.
    """
    try:
        media = await fetch(media_id, access_token, phone_number_id)
    except MediaError as exc:
        logger.info("media %s unavailable: %s", media_id, exc)
        return None, {"media_id": media_id, "error": str(exc)}

    info = {
        "media_id": media_id,
        "mime_type": media.mime_type,
        "size_bytes": media.size_bytes,
        "sha256": media.sha256,
    }

    text = readable_text(media)
    if text is not None:
        info["read_as"] = "text"
        return text, info

    described = await describe(media, catalog_products)
    if described is not None:
        info["read_as"] = "catalog_match" if catalog_products else "image_description"
        return described, info

    # Understood as a file, not as content. Say so rather than storing an
    # empty message that looks like the customer sent nothing.
    kind = (media.mime_type or "file").split("/")[-1].upper()
    info["read_as"] = "unread"
    return f"[Sent a {kind} file]", info

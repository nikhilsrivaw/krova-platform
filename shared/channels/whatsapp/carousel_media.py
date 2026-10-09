"""
Whether a WhatsApp carousel template can be sent without a person filling
anything in - kept apart from shared/channels/carousel_send.py (which sends)
so the AI's context builder can ask the same question without importing
every channel client.
"""

from shared.channels.whatsapp import templates as wa_templates
from shared.db.models import TemplateStatus


def usable_whatsapp_carousel_media(template) -> list[str] | None:
    """
    The media ids to send a WhatsApp carousel template with, or None if it
    cannot be sent as it stands.

    Usable means: approved, really a carousel, one stored image per card, and
    no {{variables}} in the message or any card - nothing that sends a
    carousel on its own (an automation rule, the AI) has per-customer values
    to put in a card's text, and sending Meta a blank delivers the literal
    placeholder.
    """
    if template is None or template.status != TemplateStatus.approved:
        return None
    components = template.components if isinstance(template.components, list) else []
    carousel = next((c for c in components if isinstance(c, dict) and c.get("type") == "CAROUSEL"), None)
    cards = (carousel or {}).get("cards") or []
    media_ids = list((template.extra or {}).get("carousel_media_ids") or [])
    if not cards or len(media_ids) != len(cards):
        return None

    card_bodies = [
        next((c.get("text", "") for c in card.get("components", []) if c.get("type") == "BODY"), "")
        for card in cards
    ]
    if wa_templates.variables_in(template.body_text or "") or any(
        wa_templates.variables_in(body) for body in card_bodies
    ):
        return None
    return media_ids

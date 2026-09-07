"""
Verifying an inbound Stripe webhook - the one place in this codebase that
uses a vendor SDK for signature verification rather than hand-rolled HMAC
(Shopify/Instagram/GitHub all use a simple raw-HMAC-over-the-body scheme,
where hand-rolling was fine).

Confirmed against Stripe's own docs before choosing this: the
`Stripe-Signature` header carries both a timestamp and an HMAC-SHA256,
and replay-tolerance is exactly the kind of thing Stripe's own
`stripe.Webhook.construct_event` exists to get right - their docs
explicitly discourage reimplementing it.
"""

import stripe

from shared.utils.logging import get_logger

logger = get_logger(__name__)


class InvalidSignature(Exception):
    """The request did not come from Stripe, or was altered on the way."""


def verify_and_parse(raw_body: bytes, signature_header: str | None, secret: str) -> dict:
    """Verify a Stripe webhook and return the parsed event as a plain
    dict. Raises InvalidSignature on any failure - a bad signature, a
    missing header, or a malformed body all collapse to the same outcome
    for the caller.

    construct_event() returns a stripe.Event object (attribute/item
    access, no .get()) - confirmed directly against a real construct_event
    call, not assumed - so this converts to a plain dict via .to_dict()
    before returning, matching every other webhook handler in this
    codebase's own dict-shaped payload convention.
    """
    if not signature_header:
        raise InvalidSignature("Signature header missing")
    try:
        event = stripe.Webhook.construct_event(raw_body, signature_header, secret)
    except (stripe.error.SignatureVerificationError, ValueError) as exc:
        logger.warning("stripe webhook rejected: %s", exc)
        raise InvalidSignature(str(exc)) from exc
    return event.to_dict()

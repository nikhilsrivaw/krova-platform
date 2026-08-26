"""
KROVA — Email Classification Prompt
Claude Haiku reads an email and decides: business enquiry or noise?
Replaces the heuristic classifier in email_processor.py from Week 4.
Fast and cheap — Claude Haiku on a short prompt, one call per email.
"""


def build_email_classifier_prompt(
    sender_email: str,
    sender_name: str | None,
    subject: str | None,
    body_preview: str | None,
) -> str:
    """
    Build the classification prompt for a single email.
    body_preview is truncated to first 500 chars — enough to classify, cheap to process.
    """
    name_part = f" ({sender_name})" if sender_name else ""
    subject_part = subject or "(no subject)"
    body_part = (body_preview or "")[:500] or "(empty body)"

    return f"""You are classifying an email for a small Indian business.

Decide if this email is a BUSINESS ENQUIRY (someone asking about products/services, following up, or a genuine business contact) or NOISE (newsletter, promotion, automated notification, spam, no-reply, social media notification, OTP, bank statement, etc.)

Email:
From: {sender_email}{name_part}
Subject: {subject_part}
Body preview: {body_part}

Reply with ONLY one word — either "business" or "noise". No explanation."""


class EmailClassification:
    BUSINESS = "business"
    NOISE = "noise"

    @staticmethod
    def parse(claude_response: str) -> str:
        """Parse Claude's one-word response. Defaults to 'noise' if unclear."""
        cleaned = claude_response.strip().lower().strip('"').strip("'")
        if "business" in cleaned:
            return EmailClassification.BUSINESS
        return EmailClassification.NOISE

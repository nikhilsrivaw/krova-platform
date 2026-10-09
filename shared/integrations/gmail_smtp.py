"""
Outbound transactional email via a real Gmail/Google Workspace account's
own SMTP - for login/registration OTP codes only, never customer-facing
mail (that is Postmark's job, see shared/integrations/postmark.py's own
docstring on why a multi-tenant product needs per-business reputation
isolation a shared Gmail account cannot give).

Chosen for this one, narrow use deliberately: free, and Gmail's own
sending domain has excellent inbox deliverability out of the box - the
two things self-hosted Postfix (used for inbound lead email,
shared/leads/email_parse.py) cannot offer for mail the business needs to
actually land in an inbox, not just be received. The real ceiling, stated
plainly rather than glossed over: Google's own sending limits (hundreds a
day on free Gmail, a couple thousand on Workspace) and its implicit
expectation that a human account isn't being used as a bulk mailer - both
fine for OTP volume, both reasons this must never grow into a general
notification channel without moving to a real ESP first.

Requires 2-Step Verification on the sending account and an **App
Password** (not the account's real password) - Google no longer accepts
a bare password over SMTP for an account with 2FA on, which is itself
why this can't simply reuse a password already on file anywhere.
"""

import aiosmtplib
from email.message import EmailMessage

from shared.config.settings import settings
from shared.utils.logging import get_logger

logger = get_logger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class GmailSmtpError(Exception):
    """Could not send this email. The message is safe to show a person."""


async def send_email(*, to: str, subject: str, body: str) -> None:
    """Send one plain-text email through the configured Gmail account."""
    if not settings.gmail_smtp_email or not settings.gmail_smtp_app_password:
        raise GmailSmtpError("Email sending is not configured on this server")

    message = EmailMessage()
    message["From"] = settings.gmail_smtp_email
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        await aiosmtplib.send(
            message,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            start_tls=True,
            username=settings.gmail_smtp_email,
            password=settings.gmail_smtp_app_password,
            timeout=20.0,
        )
    except aiosmtplib.SMTPException as exc:
        logger.error("gmail smtp send failed to=%s: %s", to, exc)
        raise GmailSmtpError("Could not send this email") from exc

    logger.info("otp email sent to=%s", to)

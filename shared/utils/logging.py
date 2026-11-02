"""
Logging.

Plain and boring on purpose: one configuration, applied once, used by the API,
the workers and the voice service alike.
"""

import logging
import re
import sys

from shared.config.settings import settings

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    handler.addFilter(_Redact())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # These two narrate every statement and every request at INFO. Useful for
    # ten minutes of debugging, unreadable the rest of the time.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _configured = True


# A phone number: 10 to 13 digits, maybe with +, spaces or dashes. Counted on its digits so a date (8) never matches.
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d \-]{8,16}\d(?![\w-])")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def mask(message: str) -> str:
    """Phone numbers down to their last four digits, email addresses down to their first letter and domain."""
    def phone(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if not 10 <= len(digits) <= 13:
            return m.group(0)
        return ("+" if m.group(0).startswith("+") else "") + "*" * (len(digits) - 4) + digits[-4:]

    return _EMAIL.sub(r"\1***@\2", _PHONE.sub(phone, message))


class _Redact(logging.Filter):
    """Every log line passes through here: no full phone number or email address is ever written to a log."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            masked = mask(message)
            if masked != message:
                record.msg, record.args = masked, None
        except Exception:  # noqa: BLE001 - a log line must never be lost to its own redaction
            pass
        return True


def said(text: str | None) -> str:
    """What a customer said, for a log line: the words only when LOG_CUSTOMER_TEXT is on, else just how long it was."""
    text = (text or "").strip()
    return repr(text) if settings.log_customer_text else f"[{len(text)} chars]"


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(name)

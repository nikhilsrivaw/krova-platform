"""
Keeps webhook tokens out of access logs.

Lead webhook URLs carry a secret token in the path (for example
/webhooks/justdial/<token>). The web server logs every request path, so the
token would otherwise sit in the logs in plain text. This filter masks it
before the line is written.
"""

import logging
import re

# Paths whose last segment is a secret token: /webhooks/<platform>/<token>
# and /webhooks/leads/<platform>/<token>.
_TOKEN_PATH = re.compile(r"(/webhooks/(?:leads/)?[a-z0-9_-]+/)[A-Za-z0-9_\-]{16,}")
_MASK = "***"


def mask_path(path: str) -> str:
    return _TOKEN_PATH.sub(lambda m: m.group(1) + _MASK, path)


class TokenRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                mask_path(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: (mask_path(v) if isinstance(v, str) else v) for k, v in record.args.items()
            }
        if isinstance(record.msg, str):
            record.msg = mask_path(record.msg)
        return True


def install() -> None:
    """Attach the filter to the uvicorn access logger. Safe to call more than once."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, TokenRedactingFilter) for f in access.filters):
        access.addFilter(TokenRedactingFilter())

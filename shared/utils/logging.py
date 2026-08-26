"""
Logging.

Plain and boring on purpose: one configuration, applied once, used by the API,
the workers and the voice service alike.
"""

import logging
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

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # These two narrate every statement and every request at INFO. Useful for
    # ten minutes of debugging, unreadable the rest of the time.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(name)

"""No full phone number or email address in any log line, and no customer words unless LOG_CUSTOMER_TEXT is on."""

import logging
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.config.settings import settings  # noqa: E402
from shared.utils import logging as krova_logging  # noqa: E402


@pytest.mark.parametrize("line, masked", [
    ("caller +91 98765 43210 joined", "caller +********3210 joined"),
    ("to 919876543210", "to ********3210"),
    ("to 98765-43210", "to ******3210"),
    ("mail nikhil.k@aqirox.com", "mail n***@aqirox.com"),
])
def test_phones_and_emails_are_masked(line, masked):
    assert krova_logging.mask(line) == masked


@pytest.mark.parametrize("line", [
    "date 2026-11-02 at 10:00:00", "cost_paise=179982", "call=4f6ec71d-3d3a-40d5-8ef3-d197f9b33cb6", "order #1042",
    "after=1.25s of 4.00s heard=31%",
])
def test_ordinary_numbers_are_left_alone(line):
    assert krova_logging.mask(line) == line


def test_every_log_line_goes_through_the_mask():
    krova_logging.get_logger("test")
    handler = logging.getLogger().handlers[0]
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "caller %s said hi", ("+91 98765 43210",), None)
    assert any(f.filter(record) for f in handler.filters)
    assert record.getMessage() == "caller +********3210 said hi"


def test_customer_words_stay_out_unless_switched_on(monkeypatch):
    monkeypatch.setattr(settings, "log_customer_text", False)
    assert krova_logging.said("mera naam Ravi hai") == "[18 chars]"
    monkeypatch.setattr(settings, "log_customer_text", True)
    assert krova_logging.said("mera naam Ravi hai") == "'mera naam Ravi hai'"

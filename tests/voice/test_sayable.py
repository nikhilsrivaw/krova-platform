"""What the voice says: no web links read out, and a name in capitals said letter by letter."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402

from shared.ai import agent  # noqa: E402
from shared.channels.voice.sarvam import sayable  # noqa: E402


@pytest.mark.parametrize("text, said", [
    ("Thank you for calling UPRTOU.", "Thank you for calling U P R T O U."),
    ("Namaste, U.P.R.T.O.U. mein aapka swagat hai.", "Namaste, U P R T O U mein aapka swagat hai."),
    ("BCA ki fees 13000 rupaye hai.", "B C A ki fees 13000 rupaye hai."),
    ("Form yahan hai: https://uprtouadm.samarth.edu.in/index.php/site/login", "Form yahan hai"),
    ("Website www.uprtou.ac.in par dekhiye.", "Website par dekhiye."),
    ("KROVA aur Aqirox", "KROVA aur Aqirox"),
    ("Rs 4,000.50 per year", "Rs 4,000.50 per year"),
    ("https://x.com", ""),
])
def test_what_is_said(text, said):
    assert sayable(text) == said


def test_the_call_agent_is_told_not_to_read_links():
    assert "Never read out a web link" in agent.SYSTEM_STREAM

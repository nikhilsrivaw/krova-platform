import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")

from shared.care.post_call_actions import _resolve_tokens  # noqa: E402


def test_resolve_tokens_substitutes_known_fields():
    assert _resolve_tokens("lead-{{source}}", {"source": "justdial"}) == "lead-justdial"


def test_resolve_tokens_blanks_missing_or_falsy_fields():
    assert _resolve_tokens("lead-{{source}}", {}) == "lead-"
    assert _resolve_tokens("lead-{{source}}", {"source": None}) == "lead-"


def test_resolve_tokens_handles_multiple_and_repeated_tokens():
    text = "{{source}}: {{query}} ({{source}})"
    assert _resolve_tokens(text, {"source": "indiamart", "query": "bulk order"}) == (
        "indiamart: bulk order (indiamart)"
    )


def test_resolve_tokens_on_plain_text_is_unchanged():
    assert _resolve_tokens("just a plain tag", {"source": "justdial"}) == "just a plain tag"

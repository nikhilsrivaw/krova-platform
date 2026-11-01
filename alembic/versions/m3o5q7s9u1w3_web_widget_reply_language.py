"""Website chat: the language the replies are in ("Replies in") - separate from the chat window's own words.

Revision ID: m3o5q7s9u1w3
Revises: l2n4p6r8t0v2
"""

import sqlalchemy as sa
from alembic import op

revision: str = "m3o5q7s9u1w3"
down_revision: str = "l2n4p6r8t0v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # None = "auto": the visitor's own language and script.
    op.add_column("web_widget_configs", sa.Column("reply_language", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("web_widget_configs", "reply_language")

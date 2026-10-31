"""Website chat: the name, first message and colour a business picks for its widget.

Revision ID: g7i9k1m3o5q7
Revises: f6h8j0l2n4p6
"""

import sqlalchemy as sa
from alembic import op

revision: str = "g7i9k1m3o5q7"
down_revision: str = "f6h8j0l2n4p6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_widget_configs", sa.Column("title", sa.String(60), nullable=True))
    op.add_column("web_widget_configs", sa.Column("greeting", sa.String(300), nullable=True))
    op.add_column("web_widget_configs", sa.Column("accent", sa.String(7), nullable=True))


def downgrade() -> None:
    op.drop_column("web_widget_configs", "accent")
    op.drop_column("web_widget_configs", "greeting")
    op.drop_column("web_widget_configs", "title")

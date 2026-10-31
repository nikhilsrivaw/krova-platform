"""Website chat: the business's logo, its own status line, how high the button sits, how it behaves on phones, and
pages it stays off.

Revision ID: i9k1m3o5q7s9
Revises: h8j0l2n4p6r8
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "i9k1m3o5q7s9"
down_revision: str = "h8j0l2n4p6r8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_widget_configs", sa.Column("logo_url", sa.String(500), nullable=True))
    op.add_column("web_widget_configs", sa.Column("status_text", sa.String(40), nullable=True))
    op.add_column("web_widget_configs", sa.Column("offset_bottom", sa.Integer(), nullable=True))
    op.add_column("web_widget_configs", sa.Column("mobile_mode", sa.String(12), nullable=True))
    op.add_column("web_widget_configs", sa.Column(
        "hide_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    for column in ("hide_paths", "mobile_mode", "offset_bottom", "status_text", "logo_url"):
        op.drop_column("web_widget_configs", column)

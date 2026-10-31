"""Website chat refinements: launcher, starters, contact-first, language, theme, WhatsApp button, install check, and
which site and page each visitor chat came from.

Revision ID: h8j0l2n4p6r8
Revises: g7i9k1m3o5q7
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "h8j0l2n4p6r8"
down_revision: str = "g7i9k1m3o5q7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_widget_configs", sa.Column("position", sa.String(5), nullable=True))
    op.add_column("web_widget_configs", sa.Column("launcher_text", sa.String(30), nullable=True))
    op.add_column("web_widget_configs", sa.Column("teaser", sa.String(140), nullable=True))
    op.add_column("web_widget_configs", sa.Column(
        "starters", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("web_widget_configs", sa.Column("ask_contact", sa.String(12), nullable=True))
    op.add_column("web_widget_configs", sa.Column("language", sa.String(8), nullable=True))
    op.add_column("web_widget_configs", sa.Column("theme", sa.String(5), nullable=True))
    op.add_column("web_widget_configs", sa.Column(
        "whatsapp_button", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("web_widget_configs", sa.Column("last_loaded_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("web_sessions", sa.Column("site_id", sa.UUID(), nullable=True))
    op.create_foreign_key("fk_web_sessions_site", "web_sessions", "web_widget_configs", ["site_id"], ["id"],
                          ondelete="SET NULL")
    op.add_column("web_sessions", sa.Column("page_url", sa.String(500), nullable=True))
    op.add_column("web_sessions", sa.Column("needed_person_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("idx_web_sessions_business_created", "web_sessions", ["business_id", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_web_sessions_business_created", table_name="web_sessions")
    op.drop_column("web_sessions", "needed_person_at")
    op.drop_column("web_sessions", "page_url")
    op.drop_constraint("fk_web_sessions_site", "web_sessions", type_="foreignkey")
    op.drop_column("web_sessions", "site_id")
    for column in ("last_loaded_at", "whatsapp_button", "theme", "language", "ask_contact", "starters", "teaser",
                   "launcher_text", "position"):
        op.drop_column("web_widget_configs", column)

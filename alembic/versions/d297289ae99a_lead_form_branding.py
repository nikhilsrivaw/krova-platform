"""Lead form branding (logo/accent color).

Revision ID: d297289ae99a
Revises: 2a1f1a8dcf1d
"""

import sqlalchemy as sa
from alembic import op

revision: str = "d297289ae99a"
down_revision: str = "2a1f1a8dcf1d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lead_forms", sa.Column("logo_url", sa.String(1000), nullable=True))
    op.add_column("lead_forms", sa.Column("accent_color", sa.String(9), nullable=True))


def downgrade() -> None:
    op.drop_column("lead_forms", "accent_color")
    op.drop_column("lead_forms", "logo_url")

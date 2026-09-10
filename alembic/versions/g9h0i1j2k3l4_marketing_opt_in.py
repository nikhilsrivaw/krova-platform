"""Marketing opt-in tracking on Customer (DPDPA / WhatsApp policy consent)

Revision ID: g9h0i1j2k3l4
Revises: f8g9h0i1j2k3
Create Date: 2026-09-11 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'g9h0i1j2k3l4'
down_revision: str | None = 'f8g9h0i1j2k3'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'customers',
        sa.Column('marketing_opt_in', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        'customers',
        sa.Column('marketing_opt_in_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('customers', 'marketing_opt_in_at')
    op.drop_column('customers', 'marketing_opt_in')

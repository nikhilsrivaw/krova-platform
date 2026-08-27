"""click-to-whatsapp ad attribution on customers

Revision ID: f21668406b0e
Revises: 85c7d7d57cde
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'f21668406b0e'
down_revision: str | None = '85c7d7d57cde'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('customers', sa.Column('ctwa_clid', sa.String(length=500), nullable=True))
    op.add_column('customers', sa.Column('ctwa_source_id', sa.String(length=100), nullable=True))
    op.add_column('customers', sa.Column('ctwa_headline', sa.String(length=500), nullable=True))
    op.add_column('customers', sa.Column('ctwa_captured_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('customers', 'ctwa_captured_at')
    op.drop_column('customers', 'ctwa_headline')
    op.drop_column('customers', 'ctwa_source_id')
    op.drop_column('customers', 'ctwa_clid')

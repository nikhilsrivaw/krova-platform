"""deal value on a customer, for the pipeline board

Revision ID: f4c8d1a9e376
Revises: e19a4b7c2f05
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'f4c8d1a9e376'
down_revision: str | None = 'e19a4b7c2f05'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('customers', sa.Column('deal_value_paise', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('customers', 'deal_value_paise')

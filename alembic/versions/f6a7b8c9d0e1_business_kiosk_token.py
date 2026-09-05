"""business kiosk_token - opd queue v2 self-service check-in

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-05 12:00:02.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'f6a7b8c9d0e1'
down_revision: str | None = 'e5f6a7b8c9d0'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('businesses', sa.Column('kiosk_token', sa.String(length=64), nullable=True))
    op.create_unique_constraint('uq_businesses_kiosk_token', 'businesses', ['kiosk_token'])


def downgrade() -> None:
    op.drop_constraint('uq_businesses_kiosk_token', 'businesses', type_='unique')
    op.drop_column('businesses', 'kiosk_token')

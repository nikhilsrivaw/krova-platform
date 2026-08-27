"""carousel templates - campaign card mapping

Revision ID: c3f8b12e94a7
Revises: a72e5f0d6c81
Create Date: 2026-08-28 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'c3f8b12e94a7'
down_revision: str | None = 'a72e5f0d6c81'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'campaigns',
        sa.Column('carousel_cards', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
    )


def downgrade() -> None:
    op.drop_column('campaigns', 'carousel_cards')

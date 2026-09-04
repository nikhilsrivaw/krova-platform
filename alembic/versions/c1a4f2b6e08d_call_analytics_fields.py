"""call analytics fields

Revision ID: c1a4f2b6e08d
Revises: 226a1311d6ef
Create Date: 2026-09-04 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'c1a4f2b6e08d'
down_revision: str | None = '226a1311d6ef'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('calls', sa.Column('outcome', sa.String(length=20), nullable=True))
    op.add_column('calls', sa.Column('sentiment', sa.String(length=20), nullable=True))
    op.add_column('calls', sa.Column('topic', sa.String(length=255), nullable=True))
    op.add_column('calls', sa.Column('summary', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('calls', 'summary')
    op.drop_column('calls', 'topic')
    op.drop_column('calls', 'sentiment')
    op.drop_column('calls', 'outcome')

"""Escalations round 2: Escalation.category

Revision ID: q0r1s2t3u4v5
Revises: p9q0r1s2t3u4
Create Date: 2026-09-14
"""
import sqlalchemy as sa
from alembic import op

revision: str = 'q0r1s2t3u4v5'
down_revision: str | None = 'p9q0r1s2t3u4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('escalations', sa.Column('category', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('escalations', 'category')

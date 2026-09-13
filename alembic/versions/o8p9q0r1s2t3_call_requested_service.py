"""Voice booking handoff: Call.requested_service

Revision ID: o8p9q0r1s2t3
Revises: n7o8p9q0r1s2
Create Date: 2026-09-13
"""
import sqlalchemy as sa
from alembic import op

revision: str = 'o8p9q0r1s2t3'
down_revision: str | None = 'n7o8p9q0r1s2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('calls', sa.Column('requested_service', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('calls', 'requested_service')

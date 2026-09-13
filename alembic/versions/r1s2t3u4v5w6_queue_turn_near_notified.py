"""Queue: QueueEntry.turn_near_notified_at

Revision ID: r1s2t3u4v5w6
Revises: q0r1s2t3u4v5
Create Date: 2026-09-14
"""
import sqlalchemy as sa
from alembic import op

revision: str = 'r1s2t3u4v5w6'
down_revision: str | None = 'q0r1s2t3u4v5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('queue_entries', sa.Column('turn_near_notified_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('queue_entries', 'turn_near_notified_at')

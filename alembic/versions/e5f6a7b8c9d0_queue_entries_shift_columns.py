"""queue_entries: shift, intake_channel, source_message_ids, shift-scoped uniqueness

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-05 12:00:01.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e5f6a7b8c9d0'
down_revision: str | None = 'd4e5f6a7b8c9'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # No real rows exist yet (OPD Queue v1 is not yet deployed), so these
    # land as plain nullable/no-default adds rather than a backfill dance -
    # nothing to migrate. shift is added nullable first then tightened to
    # NOT NULL in the same revision for exactly that reason (safe only
    # because the table is empty in every environment this runs against).
    op.add_column('queue_entries', sa.Column('shift', sa.String(length=20), nullable=True))
    op.add_column('queue_entries', sa.Column('intake_channel', sa.String(length=20), nullable=True))
    op.add_column('queue_entries', sa.Column('source_message_ids', postgresql.ARRAY(sa.UUID()), nullable=True))
    op.execute("UPDATE queue_entries SET shift = 'morning', intake_channel = 'manual' WHERE shift IS NULL")
    op.alter_column('queue_entries', 'shift', nullable=False)
    op.alter_column('queue_entries', 'intake_channel', nullable=False)

    op.create_unique_constraint(
        'uq_queue_number_per_shift_per_day', 'queue_entries',
        ['business_id', 'queue_date', 'shift', 'queue_number'],
    )


def downgrade() -> None:
    op.drop_constraint('uq_queue_number_per_shift_per_day', 'queue_entries', type_='unique')
    op.drop_column('queue_entries', 'source_message_ids')
    op.drop_column('queue_entries', 'intake_channel')
    op.drop_column('queue_entries', 'shift')

"""Voice roadmap round 2: proactive deadline-call dedupe column

Revision ID: b4c5d6e7f8g9
Revises: a3b4c5d6e7f8
Create Date: 2026-09-09 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'b4c5d6e7f8g9'
down_revision: str | None = 'a3b4c5d6e7f8'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('commitments', sa.Column('deadline_call_sent_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('commitments', 'deadline_call_sent_at')

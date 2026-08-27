"""message sent_by_user_id, for per-agent response analytics

Revision ID: a72e5f0d6c81
Revises: f4c8d1a9e376
Create Date: 2026-08-28 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a72e5f0d6c81'
down_revision: str | None = 'f4c8d1a9e376'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'messages',
        sa.Column('sent_by_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
    )
    op.create_index('idx_messages_sent_by', 'messages', ['business_id', 'sent_by_user_id'])


def downgrade() -> None:
    op.drop_index('idx_messages_sent_by', table_name='messages')
    op.drop_column('messages', 'sent_by_user_id')

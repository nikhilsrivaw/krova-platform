"""shift sessions - opd queue v2

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-05 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'd4e5f6a7b8c9'
down_revision: str | None = 'c3d4e5f6a7b8'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('shift_sessions',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('shift', sa.String(length=20), nullable=False),
    sa.Column('session_date', sa.Date(), nullable=False),
    sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('opened_by_user_id', sa.UUID(), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['opened_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('business_id', 'shift', 'session_date', name='uq_shift_session_per_day'),
    )
    op.create_index('idx_shift_sessions_business_date', 'shift_sessions', ['business_id', 'session_date'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_shift_sessions_business_date', table_name='shift_sessions')
    op.drop_table('shift_sessions')

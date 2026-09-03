"""number requests

Revision ID: 226a1311d6ef
Revises: 2ea2f3729e9e
Create Date: 2026-09-03 18:44:54.129856
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '226a1311d6ef'
down_revision: str | None = '2ea2f3729e9e'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'number_requests',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('business_id', sa.UUID(), nullable=False),
        sa.Column('request_type', sa.String(length=24), nullable=False),
        sa.Column('justification', sa.Text(), nullable=False),
        sa.Column('bfsi_declaration', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('admin_notes', sa.Text(), nullable=True),
        sa.Column('provisioned_number', sa.Text(), nullable=True),
        sa.Column('requested_by_user_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_number_requests_business', 'number_requests', ['business_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('idx_number_requests_business', table_name='number_requests')
    op.drop_table('number_requests')

"""outbound call campaigns

Revision ID: 2ea2f3729e9e
Revises: c3f8b12e94a7
Create Date: 2026-09-03 16:27:18.184298
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '2ea2f3729e9e'
down_revision: str | None = 'c3f8b12e94a7'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'call_campaigns',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('business_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('audience', sa.String(length=24), nullable=False),
        sa.Column('audience_params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('objective', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('recipients', sa.Integer(), nullable=False),
        sa.Column('sent_count', sa.Integer(), nullable=False),
        sa.Column('failed_count', sa.Integer(), nullable=False),
        sa.Column('skipped_count', sa.Integer(), nullable=False),
        sa.Column('created_by_user_id', sa.UUID(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_call_campaigns_business', 'call_campaigns', ['business_id', 'status', 'created_at']
    )

    op.create_table(
        'call_campaign_recipients',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('call_campaign_id', sa.UUID(), nullable=False),
        sa.Column('customer_id', sa.UUID(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('call_id', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['call_campaign_id'], ['call_campaigns.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['call_id'], ['calls.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_call_campaign_recipients', 'call_campaign_recipients', ['call_campaign_id', 'status']
    )


def downgrade() -> None:
    op.drop_index('idx_call_campaign_recipients', table_name='call_campaign_recipients')
    op.drop_table('call_campaign_recipients')
    op.drop_index('idx_call_campaigns_business', table_name='call_campaigns')
    op.drop_table('call_campaigns')

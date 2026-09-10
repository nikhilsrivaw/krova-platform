"""Campaign drip steps: follow-ups after a campaign's own first send

Revision ID: h1i2j3k4l5m6
Revises: g9h0i1j2k3l4
Create Date: 2026-09-11 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'h1i2j3k4l5m6'
down_revision: str | None = 'g9h0i1j2k3l4'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'campaign_steps',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('campaign_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('step_order', sa.Integer(), nullable=False),
        sa.Column('delay_days', sa.Integer(), nullable=False),
        sa.Column('condition', sa.String(length=20), nullable=False, server_default='no_reply'),
        sa.Column('stop_on_reply', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('template_name', sa.String(length=512), nullable=False),
        sa.Column('template_language', sa.String(length=16), nullable=False, server_default='en'),
        sa.Column('variable_mapping', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('carousel_cards', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('campaign_id', 'step_order', name='uq_campaign_step_order'),
    )
    op.create_index('idx_campaign_steps_campaign', 'campaign_steps', ['campaign_id'])

    op.create_table(
        'campaign_step_recipients',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('step_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('campaign_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('variables', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('message_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['step_id'], ['campaign_steps.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='SET NULL'),
        sa.UniqueConstraint('step_id', 'customer_id', name='uq_campaign_step_recipient'),
    )
    op.create_index(
        'idx_campaign_step_recipients', 'campaign_step_recipients', ['campaign_id', 'status']
    )


def downgrade() -> None:
    op.drop_index('idx_campaign_step_recipients', table_name='campaign_step_recipients')
    op.drop_table('campaign_step_recipients')
    op.drop_index('idx_campaign_steps_campaign', table_name='campaign_steps')
    op.drop_table('campaign_steps')

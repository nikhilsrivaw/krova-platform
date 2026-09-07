"""Software-startup round 2: onboarding lifecycle events, Stripe billing dunning, commitment external_ref

Revision ID: a3b4c5d6e7f8
Revises: f8a1c2b3d4e5
Create Date: 2026-09-08 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a3b4c5d6e7f8'
down_revision: str | None = 'f8a1c2b3d4e5'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('commitments', sa.Column('external_ref', sa.String(255), nullable=True))
    op.create_index('idx_commitments_external_ref', 'commitments', ['business_id', 'external_ref'])

    op.create_table(
        'customer_lifecycle_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('event', sa.String(100), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('event_metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('nudge_sent_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('idx_lifecycle_events_customer', 'customer_lifecycle_events', ['customer_id', 'event'])
    op.create_index('idx_lifecycle_events_sweep', 'customer_lifecycle_events', ['business_id', 'event', 'nudge_sent_at'])

    op.create_table(
        'stripe_connections',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('webhook_token', sa.String(64), nullable=False),
        sa.Column('webhook_secret', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('business_id', name='uq_stripe_connection_per_business'),
        sa.UniqueConstraint('webhook_token', name='uq_stripe_connection_webhook_token'),
    )
    op.create_index('idx_stripe_connections_business', 'stripe_connections', ['business_id'])
    op.create_index('idx_stripe_connections_token', 'stripe_connections', ['webhook_token'])


def downgrade() -> None:
    op.drop_index('idx_stripe_connections_token', table_name='stripe_connections')
    op.drop_index('idx_stripe_connections_business', table_name='stripe_connections')
    op.drop_table('stripe_connections')

    op.drop_index('idx_lifecycle_events_sweep', table_name='customer_lifecycle_events')
    op.drop_index('idx_lifecycle_events_customer', table_name='customer_lifecycle_events')
    op.drop_table('customer_lifecycle_events')

    op.drop_index('idx_commitments_external_ref', table_name='commitments')
    op.drop_column('commitments', 'external_ref')

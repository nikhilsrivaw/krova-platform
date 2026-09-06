"""outbound integrations - calendar connections, outbound webhooks, sync-state columns

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-07 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'c9d0e1f2a3b4'
down_revision: str | None = 'b8c9d0e1f2a3'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'calendar_connections',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('provider', sa.String(50), nullable=False, server_default='google'),
        sa.Column('external_calendar_id', sa.String(255), nullable=True),
        sa.Column('access_token', sa.Text(), nullable=True),
        sa.Column('refresh_token', sa.Text(), nullable=True),
        sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('business_id', 'provider', name='uq_calendar_connection_provider'),
    )
    op.create_index('idx_calendar_connections_business', 'calendar_connections', ['business_id'])

    op.create_table(
        'outbound_webhooks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('target_url', sa.Text(), nullable=False),
        sa.Column('secret', sa.Text(), nullable=False),
        sa.Column('event_types', postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('last_delivery_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_delivery_status', sa.String(255), nullable=True),
        sa.Column('failure_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_outbound_webhooks_business', 'outbound_webhooks', ['business_id'])

    op.add_column('appointments', sa.Column('google_calendar_event_id', sa.String(255), nullable=True))
    op.add_column('queue_entries', sa.Column('google_calendar_event_id', sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column('queue_entries', 'google_calendar_event_id')
    op.drop_column('appointments', 'google_calendar_event_id')
    op.drop_index('idx_outbound_webhooks_business', table_name='outbound_webhooks')
    op.drop_table('outbound_webhooks')
    op.drop_index('idx_calendar_connections_business', table_name='calendar_connections')
    op.drop_table('calendar_connections')

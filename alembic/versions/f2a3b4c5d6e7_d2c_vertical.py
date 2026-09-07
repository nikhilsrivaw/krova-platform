"""D2C vertical: COD/NDR/repeat-purchase order columns, abandoned checkouts, shipping connections

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-10 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f2a3b4c5d6e7'
down_revision: str | None = 'e1f2a3b4c5d6'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('is_cod', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('orders', sa.Column('cod_confirmed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('cod_declined_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('cod_confirmation_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('ndr_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('ndr_reschedule_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('repeat_purchase_nudge_sent_at', sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        'abandoned_checkouts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id', ondelete='SET NULL'), nullable=True),
        sa.Column('source_platform', sa.String(30), nullable=False),
        sa.Column('external_checkout_id', sa.String(120), nullable=False),
        sa.Column('items', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('total_paise', sa.Integer(), nullable=True),
        sa.Column('checkout_url', sa.String(1000), nullable=True),
        sa.Column('customer_phone', sa.String(30), nullable=True),
        sa.Column('customer_email', sa.String(320), nullable=True),
        sa.Column('abandoned_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('recovery_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('recovered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('business_id', 'source_platform', 'external_checkout_id', name='uq_checkout_per_business_platform'),
    )
    op.create_index('idx_abandoned_checkouts_business', 'abandoned_checkouts', ['business_id'])
    op.create_index('idx_abandoned_checkouts_customer', 'abandoned_checkouts', ['customer_id'])
    op.create_index('idx_abandoned_checkouts_sweep', 'abandoned_checkouts', ['business_id', 'recovery_sent_at', 'recovered_at'])

    op.create_table(
        'shipping_connections',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('platform', sa.String(30), nullable=False),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('password', sa.Text(), nullable=False),
        sa.Column('access_token', sa.Text(), nullable=True),
        sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('business_id', 'platform', name='uq_shipping_connection_per_business'),
    )
    op.create_index('idx_shipping_connections_business', 'shipping_connections', ['business_id'])


def downgrade() -> None:
    op.drop_index('idx_shipping_connections_business', table_name='shipping_connections')
    op.drop_table('shipping_connections')

    op.drop_index('idx_abandoned_checkouts_sweep', table_name='abandoned_checkouts')
    op.drop_index('idx_abandoned_checkouts_customer', table_name='abandoned_checkouts')
    op.drop_index('idx_abandoned_checkouts_business', table_name='abandoned_checkouts')
    op.drop_table('abandoned_checkouts')

    op.drop_column('orders', 'repeat_purchase_nudge_sent_at')
    op.drop_column('orders', 'ndr_reschedule_sent_at')
    op.drop_column('orders', 'ndr_at')
    op.drop_column('orders', 'cod_confirmation_sent_at')
    op.drop_column('orders', 'cod_declined_at')
    op.drop_column('orders', 'cod_confirmed_at')
    op.drop_column('orders', 'is_cod')

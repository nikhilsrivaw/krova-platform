"""order sync - e-commerce capability

Revision ID: 2cfc5a31824e
Revises: c58587c6461c
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '2cfc5a31824e'
down_revision: str | None = 'c58587c6461c'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('orders',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=True),
    sa.Column('source_platform', sa.String(length=30), nullable=False),
    sa.Column('external_order_id', sa.String(length=120), nullable=False),
    sa.Column('order_number', sa.String(length=50), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('items', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('total_paise', sa.Integer(), nullable=True),
    sa.Column('tracking_number', sa.String(length=100), nullable=True),
    sa.Column('carrier', sa.String(length=100), nullable=True),
    sa.Column('placed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('business_id', 'source_platform', 'external_order_id', name='uq_order_per_business_platform'),
    )
    op.create_index('idx_orders_business', 'orders', ['business_id'], unique=False)
    op.create_index('idx_orders_customer', 'orders', ['customer_id'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_orders_customer', table_name='orders')
    op.drop_index('idx_orders_business', table_name='orders')
    op.drop_table('orders')

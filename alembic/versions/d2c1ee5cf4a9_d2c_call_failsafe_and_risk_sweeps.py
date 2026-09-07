"""D2C 4-item batch: COD call failsafe/escalation columns, shipping pincode, RTO-risk dedupe

Revision ID: d2c1ee5cf4a9
Revises: f2a3b4c5d6e7
Create Date: 2026-09-07 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'd2c1ee5cf4a9'
down_revision: str | None = 'f2a3b4c5d6e7'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('cod_call_placed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('cod_call_escalated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('shipping_pincode', sa.String(20), nullable=True))
    op.add_column('orders', sa.Column('rto_risk_checked_at', sa.DateTime(timezone=True), nullable=True))

    op.create_index('idx_orders_pincode_ndr', 'orders', ['business_id', 'shipping_pincode', 'ndr_at'])


def downgrade() -> None:
    op.drop_index('idx_orders_pincode_ndr', table_name='orders')

    op.drop_column('orders', 'rto_risk_checked_at')
    op.drop_column('orders', 'shipping_pincode')
    op.drop_column('orders', 'cod_call_escalated_at')
    op.drop_column('orders', 'cod_call_placed_at')

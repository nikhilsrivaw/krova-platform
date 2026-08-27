"""whatsapp flows

Revision ID: a3f9c2d81b4e
Revises: f21668406b0e
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a3f9c2d81b4e'
down_revision: str | None = 'f21668406b0e'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'whatsapp_flows',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('meta_flow_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('categories', postgresql.JSONB(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('flow_json', postgresql.JSONB(), nullable=False),
        sa.Column('validation_errors', postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint('business_id', 'meta_flow_id', name='uq_flow_meta_id'),
    )
    op.create_index('idx_flows_business', 'whatsapp_flows', ['business_id'])

    op.create_table(
        'whatsapp_flow_sends',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('flow_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('whatsapp_flows.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('flow_token', sa.String(length=255), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('business_id', 'flow_token', name='uq_flow_token'),
    )
    op.create_index('idx_flow_sends_token', 'whatsapp_flow_sends', ['business_id', 'flow_token'])


def downgrade() -> None:
    op.drop_index('idx_flow_sends_token', table_name='whatsapp_flow_sends')
    op.drop_table('whatsapp_flow_sends')
    op.drop_index('idx_flows_business', table_name='whatsapp_flows')
    op.drop_table('whatsapp_flows')

"""store connections - e-commerce webhook credentials

Revision ID: d3a675cd0ec8
Revises: 2cfc5a31824e
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'd3a675cd0ec8'
down_revision: str | None = '2cfc5a31824e'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('store_connections',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('platform', sa.String(length=30), nullable=False),
    sa.Column('store_identifier', sa.String(length=255), nullable=False),
    sa.Column('webhook_secret', sa.String(length=500), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('business_id', 'platform', 'store_identifier', name='uq_store_connection_per_business'),
    )
    op.create_index('idx_store_connections_business', 'store_connections', ['business_id'], unique=False)
    op.create_index('idx_store_connections_lookup', 'store_connections', ['platform', 'store_identifier'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_store_connections_lookup', table_name='store_connections')
    op.drop_index('idx_store_connections_business', table_name='store_connections')
    op.drop_table('store_connections')

"""Product catalogue: Product + ProductVariant

Revision ID: s2t3u4v5w6x7
Revises: r1s2t3u4v5w6
Create Date: 2026-09-24
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision: str = 's2t3u4v5w6x7'
down_revision: str | None = 'r1s2t3u4v5w6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'products',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('business_id', PgUUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_platform', sa.String(length=30), nullable=False),
        sa.Column('external_id', sa.String(length=120), nullable=True),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('product_type', sa.String(length=255), nullable=True),
        sa.Column('vendor', sa.String(length=255), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('image_url', sa.Text(), nullable=True),
        sa.Column('raw_payload', JSONB(), nullable=False, server_default='{}'),
        sa.UniqueConstraint('business_id', 'source_platform', 'external_id', name='uq_product_per_business_platform'),
    )
    op.create_index('idx_products_business', 'products', ['business_id', 'status'])
    op.create_index('idx_products_title', 'products', ['business_id', 'title'])

    op.create_table(
        'product_variants',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('product_id', PgUUID(as_uuid=True), sa.ForeignKey('products.id', ondelete='CASCADE'), nullable=False),
        sa.Column('business_id', PgUUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_platform', sa.String(length=30), nullable=False),
        sa.Column('external_id', sa.String(length=120), nullable=True),
        sa.Column('sku', sa.String(length=120), nullable=True),
        sa.Column('title', sa.String(length=500), nullable=True),
        sa.Column('options', JSONB(), nullable=False, server_default='{}'),
        sa.Column('price_paise', sa.Integer(), nullable=True),
        sa.Column('inventory_quantity', sa.Integer(), nullable=True),
        sa.Column('available', sa.Boolean(), nullable=True),
        sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw_payload', JSONB(), nullable=False, server_default='{}'),
        sa.UniqueConstraint('business_id', 'source_platform', 'external_id', name='uq_variant_per_business_platform'),
    )
    op.create_index('idx_variants_sku', 'product_variants', ['business_id', 'sku'])
    op.create_index('idx_variants_product', 'product_variants', ['product_id'])


def downgrade() -> None:
    op.drop_index('idx_variants_product', table_name='product_variants')
    op.drop_index('idx_variants_sku', table_name='product_variants')
    op.drop_table('product_variants')
    op.drop_index('idx_products_title', table_name='products')
    op.drop_index('idx_products_business', table_name='products')
    op.drop_table('products')

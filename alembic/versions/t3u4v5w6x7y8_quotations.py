"""Quotations: Quotation + QuotationItem

Revision ID: t3u4v5w6x7y8
Revises: s2t3u4v5w6x7
Create Date: 2026-09-24
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PgUUID

revision: str = 't3u4v5w6x7y8'
down_revision: str | None = 's2t3u4v5w6x7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'quotations',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('business_id', PgUUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', PgUUID(as_uuid=True), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('reference', sa.String(length=80), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='draft'),
        sa.Column('total_paise', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('last_followed_up_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('follow_up_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('outcome_note', sa.Text(), nullable=True),
        sa.Column('supersedes_id', PgUUID(as_uuid=True), sa.ForeignKey('quotations.id', ondelete='SET NULL'), nullable=True),
        sa.Column('source_message_ids', ARRAY(PgUUID(as_uuid=True)), nullable=False, server_default='{}'),
        sa.Column('source_quote', sa.Text(), nullable=True),
        sa.UniqueConstraint('business_id', 'reference', name='uq_quotation_reference'),
    )
    op.create_index('idx_quotations_open', 'quotations', ['business_id', 'status', 'sent_at'])
    op.create_index('idx_quotations_customer', 'quotations', ['customer_id', 'status'])
    op.create_index('idx_quotations_expiry', 'quotations', ['status', 'valid_until'])

    op.create_table(
        'quotation_items',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('quotation_id', PgUUID(as_uuid=True), sa.ForeignKey('quotations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('business_id', PgUUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('variant_id', PgUUID(as_uuid=True), sa.ForeignKey('product_variants.id', ondelete='SET NULL'), nullable=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('quantity', sa.String(length=80), nullable=True),
        sa.Column('unit_price_paise', sa.Integer(), nullable=True),
        sa.Column('line_total_paise', sa.Integer(), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('extra', JSONB(), nullable=False, server_default='{}'),
    )
    op.create_index('idx_quotation_items_quotation', 'quotation_items', ['quotation_id', 'position'])
    op.create_index('idx_quotation_items_variant', 'quotation_items', ['business_id', 'variant_id'])


def downgrade() -> None:
    op.drop_index('idx_quotation_items_variant', table_name='quotation_items')
    op.drop_index('idx_quotation_items_quotation', table_name='quotation_items')
    op.drop_table('quotation_items')
    op.drop_index('idx_quotations_expiry', table_name='quotations')
    op.drop_index('idx_quotations_customer', table_name='quotations')
    op.drop_index('idx_quotations_open', table_name='quotations')
    op.drop_table('quotations')

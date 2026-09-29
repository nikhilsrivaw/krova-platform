"""Type 1 backlog: commitments.quotation_id, customers.price_tier

Revision ID: a1b2c3d4e5f6
Revises: z9a0b1c2d3e4
Create Date: 2026-09-29

Two of the three Type 1 backlog items built this batch (docs/new/
type-1-backlog.md):

- commitments.quotation_id: links an advance/proforma-payment Commitment
  back to the quotation it belongs to, so QuotationStatus.won stops being a
  dead end (#2). Same nullable-FK-SET_NULL shape as commitments.
  appointment_id from the Type 4 batch.
- customers.price_tier: a business's own free-text label for which pricing
  tier a customer is in ("Dealer A", "Distributor B") - the buyer-tier
  concept #1 (price/terms consistency) needs before comparing two quotes'
  prices means anything. Same shape as customers.stage.

No backfill: nullable, no existing row has either set.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = 'z9a0b1c2d3e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'commitments',
        sa.Column(
            'quotation_id',
            PgUUID(as_uuid=True),
            sa.ForeignKey('quotations.id', ondelete='SET NULL', name='fk_commitments_quotation'),
            nullable=True,
        ),
    )
    op.create_index('ix_commitments_quotation_id', 'commitments', ['quotation_id'])
    op.add_column('customers', sa.Column('price_tier', sa.String(60), nullable=True))


def downgrade() -> None:
    op.drop_column('customers', 'price_tier')
    op.drop_index('ix_commitments_quotation_id', table_name='commitments')
    op.drop_constraint('fk_commitments_quotation', 'commitments', type_='foreignkey')
    op.drop_column('commitments', 'quotation_id')

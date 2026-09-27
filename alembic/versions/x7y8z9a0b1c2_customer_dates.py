"""Key dates on a customer: customer_dates

Revision ID: x7y8z9a0b1c2
Revises: w6x7y8z9a0b1
Create Date: 2026-09-27

A date the business knows about a customer that the conversation may never
mention - a renewal, a package ending, an AMC expiring. The business names
it; its own automation rules decide what happens as it approaches
(customer.date_approaching). See shared/db/models/crm.py::CustomerDate.
No backfill: nothing recorded these dates before.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = 'x7y8z9a0b1c2'
down_revision: str | None = 'w6x7y8z9a0b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'customer_dates',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('business_id', PgUUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', PgUUID(as_uuid=True), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('label', sa.String(length=60), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
    )
    op.create_index('idx_customer_dates_business_date', 'customer_dates', ['business_id', 'date'])
    op.create_index('idx_customer_dates_customer', 'customer_dates', ['customer_id'])


def downgrade() -> None:
    op.drop_index('idx_customer_dates_customer', table_name='customer_dates')
    op.drop_index('idx_customer_dates_business_date', table_name='customer_dates')
    op.drop_table('customer_dates')

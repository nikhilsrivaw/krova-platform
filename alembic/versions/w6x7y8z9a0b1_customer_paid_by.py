"""Who pays for a customer: customers.paid_by_customer_id

Revision ID: w6x7y8z9a0b1
Revises: v5w6x7y8z9a0
Create Date: 2026-09-26

A parent for a student, a company for an employee, a family member for a
tenant - see Customer.paid_by_customer_id. Nullable, SET NULL on delete, no
backfill: nothing has ever recorded who pays for whom, so every existing
customer starts with no payer linked.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = 'w6x7y8z9a0b1'
down_revision: str | None = 'v5w6x7y8z9a0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'customers',
        sa.Column(
            'paid_by_customer_id',
            PgUUID(as_uuid=True),
            sa.ForeignKey('customers.id', ondelete='SET NULL', name='fk_customers_paid_by'),
            nullable=True,
        ),
    )
    op.create_index('ix_customers_paid_by_customer_id', 'customers', ['paid_by_customer_id'])


def downgrade() -> None:
    op.drop_index('ix_customers_paid_by_customer_id', table_name='customers')
    op.drop_constraint('fk_customers_paid_by', 'customers', type_='foreignkey')
    op.drop_column('customers', 'paid_by_customer_id')

"""Type 2's biggest known gap: installment plans

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-29

"₹60,000 in 3 installments" is a series of dated obligations from one
agreement - a Commitment is one amount and one date. Rather than widen
Commitment into holding an array (which would break every existing query,
sweep and dunning mechanism that assumes one amount/one due_at per row),
each installment IS its own ordinary Commitment - due-date reminders,
partial payments, the deadline sweep, the Ledger UI all already work on it
unmodified. installment_plans is only the grouping: the agreement's total
and description, with its own commitments.installment_plan_id pointing
back to it, in order (installment_number).

No backfill: nothing existing was ever part of a plan.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PgUUID

revision: str = 'b2c3d4e5f6a7'
down_revision: str | None = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'installment_plans',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', PgUUID(as_uuid=True),
                  sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', PgUUID(as_uuid=True),
                  sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('total_paise', sa.Integer(), nullable=False),
        sa.Column('source_message_ids', ARRAY(PgUUID(as_uuid=True)),
                  nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_installment_plans_business_customer', 'installment_plans',
                     ['business_id', 'customer_id'])

    op.add_column(
        'commitments',
        sa.Column(
            'installment_plan_id',
            PgUUID(as_uuid=True),
            sa.ForeignKey('installment_plans.id', ondelete='CASCADE', name='fk_commitments_installment_plan'),
            nullable=True,
        ),
    )
    op.add_column('commitments', sa.Column('installment_number', sa.Integer(), nullable=True))
    op.create_index('ix_commitments_installment_plan_id', 'commitments', ['installment_plan_id'])


def downgrade() -> None:
    op.drop_index('ix_commitments_installment_plan_id', table_name='commitments')
    op.drop_column('commitments', 'installment_number')
    op.drop_constraint('fk_commitments_installment_plan', 'commitments', type_='foreignkey')
    op.drop_column('commitments', 'installment_plan_id')
    op.drop_index('idx_installment_plans_business_customer', table_name='installment_plans')
    op.drop_table('installment_plans')

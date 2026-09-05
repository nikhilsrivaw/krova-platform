"""tpa claim tracking - clinic capability

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-05 00:00:02.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'c3d4e5f6a7b8'
down_revision: str | None = 'b2c3d4e5f6a7'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('insurance_claims',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('insurer_or_tpa_name', sa.String(length=255), nullable=True),
    sa.Column('policy_number', sa.String(length=100), nullable=True),
    sa.Column('claim_number', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('claim_amount_paise', sa.Integer(), nullable=True),
    sa.Column('approved_amount_paise', sa.Integer(), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_claims_business_status', 'insurance_claims', ['business_id', 'status'], unique=False)
    op.create_index('idx_claims_customer', 'insurance_claims', ['customer_id'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_claims_customer', table_name='insurance_claims')
    op.drop_index('idx_claims_business_status', table_name='insurance_claims')
    op.drop_table('insurance_claims')

"""case tracking - law firm capability

Revision ID: c58587c6461c
Revises: 0aecef9144c3
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'c58587c6461c'
down_revision: str | None = '0aecef9144c3'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('cases',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('assigned_to_user_id', sa.UUID(), nullable=True),
    sa.Column('case_number', sa.String(length=100), nullable=True),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('opposing_party', sa.String(length=255), nullable=True),
    sa.Column('court', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('next_hearing_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['assigned_to_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_cases_business', 'cases', ['business_id'], unique=False)
    op.create_index('idx_cases_customer', 'cases', ['customer_id'], unique=False)
    op.create_index('idx_cases_business_hearing', 'cases', ['business_id', 'next_hearing_at'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_cases_business_hearing', table_name='cases')
    op.drop_index('idx_cases_customer', table_name='cases')
    op.drop_index('idx_cases_business', table_name='cases')
    op.drop_table('cases')

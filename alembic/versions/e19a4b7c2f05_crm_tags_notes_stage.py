"""crm tags, notes, pipeline stage

Revision ID: e19a4b7c2f05
Revises: b7e42fd190a3
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e19a4b7c2f05'
down_revision: str | None = 'b7e42fd190a3'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'customer_tags',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('label', sa.String(length=60), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='confirmed'),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('created_by_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('decided_by_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('customer_id', 'label', name='uq_customer_tag_label'),
    )
    op.create_index('idx_customer_tags_business', 'customer_tags', ['business_id', 'status'])
    op.create_index('idx_customer_tags_customer', 'customer_tags', ['customer_id'])

    op.create_table(
        'customer_notes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('author_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
    )
    op.create_index('idx_customer_notes_customer', 'customer_notes', ['customer_id'])

    op.add_column('customers', sa.Column('stage', sa.String(length=60), nullable=True))


def downgrade() -> None:
    op.drop_column('customers', 'stage')

    op.drop_index('idx_customer_notes_customer', table_name='customer_notes')
    op.drop_table('customer_notes')

    op.drop_index('idx_customer_tags_customer', table_name='customer_tags')
    op.drop_index('idx_customer_tags_business', table_name='customer_tags')
    op.drop_table('customer_tags')

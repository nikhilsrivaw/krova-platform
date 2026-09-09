"""Voice roadmap round 6: structured call scripts (lead qualification / survey)

Revision ID: f8g9h0i1j2k3
Revises: e7f8g9h0i1j2
Create Date: 2026-09-10 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f8g9h0i1j2k3'
down_revision: str | None = 'e7f8g9h0i1j2'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'call_scripts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('purpose', sa.String(30), nullable=False, server_default='survey'),
        sa.Column('questions', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_call_scripts_business', 'call_scripts', ['business_id'])

    op.create_table(
        'call_script_responses',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('call_script_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('call_scripts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('customers.id', ondelete='SET NULL'), nullable=True),
        sa.Column('call_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('calls.id', ondelete='SET NULL'), nullable=True),
        sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_call_script_responses_script', 'call_script_responses', ['call_script_id', 'created_at'])

    op.add_column('call_campaigns', sa.Column('call_script_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('call_scripts.id', ondelete='SET NULL'), nullable=True))


def downgrade() -> None:
    op.drop_column('call_campaigns', 'call_script_id')
    op.drop_index('idx_call_script_responses_script', table_name='call_script_responses')
    op.drop_table('call_script_responses')
    op.drop_index('idx_call_scripts_business', table_name='call_scripts')
    op.drop_table('call_scripts')

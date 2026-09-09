"""Voice roadmap round 5: post-call action rules engine

Revision ID: e7f8g9h0i1j2
Revises: d6e7f8g9h0i1
Create Date: 2026-09-09 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e7f8g9h0i1j2'
down_revision: str | None = 'd6e7f8g9h0i1'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'post_call_action_rules',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('trigger_type', sa.String(50), nullable=False),
        sa.Column('action_type', sa.String(50), nullable=False),
        sa.Column('action_config', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        'idx_post_call_rules_lookup', 'post_call_action_rules',
        ['business_id', 'trigger_type', 'is_active'],
    )


def downgrade() -> None:
    op.drop_index('idx_post_call_rules_lookup', table_name='post_call_action_rules')
    op.drop_table('post_call_action_rules')

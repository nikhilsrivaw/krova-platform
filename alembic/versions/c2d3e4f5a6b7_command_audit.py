"""Command audit: every owner command, from preview to outcome

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-10-03

The row is the pending action until the owner confirms, then the audit
record. Nothing is executed without a row here first.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision: str = 'c2d3e4f5a6b7'
down_revision: str | None = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'command_audit',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', PgUUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', PgUUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('tool', sa.String(40), nullable=False),
        sa.Column('args', JSONB, nullable=False),
        sa.Column('preview', JSONB, nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('result', JSONB, nullable=True),
        sa.Column('error', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('idx_command_audit_business_created', 'command_audit', ['business_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('idx_command_audit_business_created', table_name='command_audit')
    op.drop_table('command_audit')

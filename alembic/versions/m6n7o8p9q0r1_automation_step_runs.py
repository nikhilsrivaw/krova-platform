"""Automations engine phase 3: AutomationStepRun, for delayed steps

Revision ID: m6n7o8p9q0r1
Revises: l5m6n7o8p9q0
Create Date: 2026-09-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision: str = 'm6n7o8p9q0r1'
down_revision: str | None = 'l5m6n7o8p9q0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'automation_step_runs',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('step_id', PgUUID(as_uuid=True), sa.ForeignKey('automation_steps.id', ondelete='CASCADE'), nullable=False),
        sa.Column('business_id', PgUUID(as_uuid=True), nullable=False),
        sa.Column('customer_id', PgUUID(as_uuid=True), nullable=False),
        sa.Column('call_id', PgUUID(as_uuid=True), nullable=True),
        sa.Column('channel', sa.String(length=20), nullable=True),
        sa.Column('trigger_type', sa.String(length=50), nullable=False),
        sa.Column('action_type', sa.String(length=50), nullable=False),
        sa.Column('action_config', JSONB(), nullable=False, server_default='{}'),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('idx_automation_step_runs_due', 'automation_step_runs', ['due_at', 'executed_at'])


def downgrade() -> None:
    op.drop_index('idx_automation_step_runs_due', table_name='automation_step_runs')
    op.drop_table('automation_step_runs')

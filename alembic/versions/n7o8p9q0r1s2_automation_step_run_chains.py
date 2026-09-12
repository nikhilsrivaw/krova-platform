"""Automations engine phase 4: AutomationStepRun becomes a resumable chain

Revision ID: n7o8p9q0r1s2
Revises: m6n7o8p9q0r1
Create Date: 2026-09-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision: str = 'n7o8p9q0r1s2'
down_revision: str | None = 'm6n7o8p9q0r1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Phase 3 shipped this table with exactly one delayed step in mind
    # (step_id/action_type/action_config). Phase 4 generalizes a queued
    # run to "the rest of the chain from here" - no real rows exist yet
    # (the feature is brand new, not yet reachable from any rule with more
    # than one step), so this replaces rather than migrates the old shape.
    op.drop_constraint('automation_step_runs_step_id_fkey', 'automation_step_runs', type_='foreignkey')
    op.drop_column('automation_step_runs', 'step_id')
    op.drop_column('automation_step_runs', 'action_type')
    op.drop_column('automation_step_runs', 'action_config')

    op.add_column(
        'automation_step_runs',
        sa.Column('rule_id', PgUUID(as_uuid=True), sa.ForeignKey('post_call_action_rules.id', ondelete='CASCADE'), nullable=True),
    )
    op.add_column('automation_step_runs', sa.Column('context', JSONB(), nullable=False, server_default='{}'))
    op.add_column('automation_step_runs', sa.Column('remaining_steps', JSONB(), nullable=False, server_default='[]'))
    # No existing rows to backfill (see above) - rule_id only starts NOT
    # NULL from here since a backfill value would have to be fabricated.
    op.alter_column('automation_step_runs', 'rule_id', nullable=False)


def downgrade() -> None:
    op.drop_column('automation_step_runs', 'remaining_steps')
    op.drop_column('automation_step_runs', 'context')
    op.drop_constraint('automation_step_runs_rule_id_fkey', 'automation_step_runs', type_='foreignkey')
    op.drop_column('automation_step_runs', 'rule_id')

    op.add_column('automation_step_runs', sa.Column('action_type', sa.String(length=50), nullable=False, server_default='add_tag'))
    op.add_column('automation_step_runs', sa.Column('action_config', JSONB(), nullable=False, server_default='{}'))
    op.add_column(
        'automation_step_runs',
        sa.Column('step_id', PgUUID(as_uuid=True), sa.ForeignKey('automation_steps.id', ondelete='CASCADE'), nullable=True),
    )
    op.alter_column('automation_step_runs', 'step_id', nullable=False)

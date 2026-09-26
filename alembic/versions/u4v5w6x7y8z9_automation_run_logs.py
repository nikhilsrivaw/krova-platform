"""Automation execution history: automation_run_logs

Revision ID: u4v5w6x7y8z9
Revises: t3u4v5w6x7y8
Create Date: 2026-09-25

One row per automation step outcome, so a business can finally see whether
a rule it built ever ran - see shared/db/models/integrations.py's
AutomationRunLog docstring for why this is its own table rather than a
widening of automation_step_runs (that one only ever exists for a *delayed*
step, which is the minority case).

No backfill: there is no history to recover. Rows start accumulating from
the first trigger that fires after this deploys, and age out on the prune
job registered in services/api/scheduler.py.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision: str = 'u4v5w6x7y8z9'
down_revision: str | None = 't3u4v5w6x7y8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'automation_run_logs',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            'rule_id',
            PgUUID(as_uuid=True),
            sa.ForeignKey('post_call_action_rules.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('business_id', PgUUID(as_uuid=True), nullable=False),
        sa.Column('customer_id', PgUUID(as_uuid=True), nullable=True),
        sa.Column('trigger_type', sa.String(length=50), nullable=False),
        sa.Column('step_position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('action_type', sa.String(length=50), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('context', JSONB(), nullable=False, server_default='{}'),
    )
    # The two ways this table is ever read: one rule's history, and the
    # whole business's recent activity - both newest-first.
    op.create_index('idx_automation_run_logs_rule', 'automation_run_logs', ['rule_id', 'created_at'])
    op.create_index('idx_automation_run_logs_business', 'automation_run_logs', ['business_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('idx_automation_run_logs_business', table_name='automation_run_logs')
    op.drop_index('idx_automation_run_logs_rule', table_name='automation_run_logs')
    op.drop_table('automation_run_logs')

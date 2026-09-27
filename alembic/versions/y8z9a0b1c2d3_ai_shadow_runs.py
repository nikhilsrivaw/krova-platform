"""AI router shadow testing: ai_shadow_runs

Revision ID: y8z9a0b1c2d3
Revises: x7y8z9a0b1c2
Create Date: 2026-09-27

One row per real AI call that a cheaper candidate model also answered,
silently, while shadow mode is switched on for that task (AI_SHADOW_ROUTES,
see shared/ai/router.py). Empty unless a shadow test is running. See
shared/db/models/billing.py's AiShadowRun docstring.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision: str = 'y8z9a0b1c2d3'
down_revision: str | None = 'x7y8z9a0b1c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_shadow_runs',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('task', sa.String(64), nullable=False),
        sa.Column('primary_model', sa.String(64), nullable=False),
        sa.Column('shadow_model', sa.String(64), nullable=False),
        sa.Column('primary_output', JSONB, nullable=False, server_default='{}'),
        sa.Column('shadow_output', JSONB, nullable=False, server_default='{}'),
        sa.Column('input_excerpt', sa.String(), nullable=True),
        sa.Column('primary_cost_paise', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('shadow_cost_paise', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_ai_shadow_runs_task_created', 'ai_shadow_runs', ['task', 'created_at'])


def downgrade() -> None:
    op.drop_index('idx_ai_shadow_runs_task_created', table_name='ai_shadow_runs')
    op.drop_table('ai_shadow_runs')

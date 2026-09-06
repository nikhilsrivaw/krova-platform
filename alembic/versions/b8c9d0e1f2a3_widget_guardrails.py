"""widget guardrails - rate limiting, turn cap, consent tracking

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'b8c9d0e1f2a3'
down_revision: str | None = 'a7b8c9d0e1f2'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('web_widget_configs', sa.Column('rate_limit_window_started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('web_widget_configs', sa.Column('rate_limit_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('web_sessions', sa.Column('turn_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('web_sessions', sa.Column('consent_given_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('web_sessions', 'consent_given_at')
    op.drop_column('web_sessions', 'turn_count')
    op.drop_column('web_widget_configs', 'rate_limit_count')
    op.drop_column('web_widget_configs', 'rate_limit_window_started_at')

"""Automation rules: an optional channel filter

Revision ID: k4l5m6n7o8p9
Revises: j3k4l5m6n7o8
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa

revision: str = 'k4l5m6n7o8p9'
down_revision: str | None = 'j3k4l5m6n7o8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'post_call_action_rules',
        sa.Column('channel', sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('post_call_action_rules', 'channel')

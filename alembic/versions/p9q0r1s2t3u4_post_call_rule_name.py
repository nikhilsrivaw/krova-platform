"""Self-service automations: PostCallActionRule.name

Revision ID: p9q0r1s2t3u4
Revises: o8p9q0r1s2t3
Create Date: 2026-09-13
"""
import sqlalchemy as sa
from alembic import op

revision: str = 'p9q0r1s2t3u4'
down_revision: str | None = 'o8p9q0r1s2t3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('post_call_action_rules', sa.Column('name', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('post_call_action_rules', 'name')

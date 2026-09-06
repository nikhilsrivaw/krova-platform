"""website chat widget - web_widget_configs, web_sessions

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a7b8c9d0e1f2'
down_revision: str | None = 'f6a7b8c9d0e1'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('web_widget_configs',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('site_key', sa.String(length=64), nullable=False),
    sa.Column('allowed_domain', sa.String(length=255), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('site_key'),
    sa.UniqueConstraint('business_id', 'allowed_domain', name='uq_web_widget_domain_per_business'),
    )
    op.create_index('idx_web_widget_configs_business', 'web_widget_configs', ['business_id'], unique=False)

    op.create_table('web_sessions',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('session_token', sa.String(length=64), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=True),
    sa.Column('transcript', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_token'),
    )
    op.create_index('idx_web_sessions_business', 'web_sessions', ['business_id'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_web_sessions_business', table_name='web_sessions')
    op.drop_table('web_sessions')
    op.drop_index('idx_web_widget_configs_business', table_name='web_widget_configs')
    op.drop_table('web_widget_configs')

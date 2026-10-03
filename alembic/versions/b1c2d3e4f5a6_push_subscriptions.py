"""Push subscriptions: one row per browser or home-screen install that opted in to notifications

Revision ID: b1c2d3e4f5a6
Revises: a8fc8a086e3b
Create Date: 2026-10-03

Web Push (VAPID) needs no third-party account. The endpoint URL is unique
per browser install, so re-subscribing the same install upserts instead of
duplicating.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = 'b1c2d3e4f5a6'
down_revision: str | None = 'a8fc8a086e3b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'push_subscriptions',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', PgUUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', PgUUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('endpoint', sa.String(1024), nullable=False, unique=True),
        sa.Column('p256dh', sa.String(255), nullable=False),
        sa.Column('auth', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('idx_push_subscriptions_business', 'push_subscriptions', ['business_id'])


def downgrade() -> None:
    op.drop_index('idx_push_subscriptions_business', table_name='push_subscriptions')
    op.drop_table('push_subscriptions')

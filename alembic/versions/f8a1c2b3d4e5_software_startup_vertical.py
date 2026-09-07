"""Software-startup vertical: GitHub bug-lifecycle loop, email sending, feature-request dedup

Revision ID: f8a1c2b3d4e5
Revises: d2c1ee5cf4a9
Create Date: 2026-09-07 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f8a1c2b3d4e5'
down_revision: str | None = 'd2c1ee5cf4a9'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('commitments', sa.Column('github_issue_url', sa.Text(), nullable=True))
    op.add_column('commitments', sa.Column('bug_fix_notified_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('idx_commitments_github_issue', 'commitments', ['github_issue_url'])

    op.add_column('insights', sa.Column('dedup_checked_at', sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        'github_connections',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('repo_owner', sa.String(255), nullable=False),
        sa.Column('repo_name', sa.String(255), nullable=False),
        sa.Column('access_token', sa.Text(), nullable=False),
        sa.Column('webhook_secret', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('business_id', name='uq_github_connection_per_business'),
    )
    op.create_index('idx_github_connections_business', 'github_connections', ['business_id'])

    op.create_table(
        'email_send_connections',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('from_email', sa.String(320), nullable=False),
        sa.Column('postmark_signature_id', sa.String(50), nullable=False),
        sa.Column('verified', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('business_id', name='uq_email_send_connection_per_business'),
    )
    op.create_index('idx_email_send_connections_business', 'email_send_connections', ['business_id'])


def downgrade() -> None:
    op.drop_index('idx_email_send_connections_business', table_name='email_send_connections')
    op.drop_table('email_send_connections')

    op.drop_index('idx_github_connections_business', table_name='github_connections')
    op.drop_table('github_connections')

    op.drop_column('insights', 'dedup_checked_at')

    op.drop_index('idx_commitments_github_issue', table_name='commitments')
    op.drop_column('commitments', 'bug_fix_notified_at')
    op.drop_column('commitments', 'github_issue_url')

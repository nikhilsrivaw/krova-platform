"""Team: availability, round-robin routing marker, reply-time alerts.

Revision ID: c8e3a05d7f12
Revises: b7d2f91c4e58
"""

import sqlalchemy as sa
from alembic import op

revision: str = "c8e3a05d7f12"
down_revision: str = "b7d2f91c4e58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_members",
        sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("business_members", sa.Column("last_routed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("customers", sa.Column("sla_alerted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("customers", sa.Column("sla_escalated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("customers", "sla_escalated_at")
    op.drop_column("customers", "sla_alerted_at")
    op.drop_column("business_members", "last_routed_at")
    op.drop_column("business_members", "available")

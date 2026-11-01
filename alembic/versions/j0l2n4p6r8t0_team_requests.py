"""Talk to team: a customer's ask to speak to a person, waiting for whoever is free.

Revision ID: j0l2n4p6r8t0
Revises: i9k1m3o5q7s9
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "j0l2n4p6r8t0"
down_revision: str = "i9k1m3o5q7s9"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "team_requests",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("business_id", UUID, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", UUID, sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("what", sa.String(2000), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="customer"),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("team_id", UUID, sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True),
        sa.Column("taken_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(2000), nullable=True),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("idx_team_requests_business_status", "team_requests", ["business_id", "status"])
    op.create_index("idx_team_requests_customer", "team_requests", ["customer_id"])


def downgrade() -> None:
    op.drop_index("idx_team_requests_customer", table_name="team_requests")
    op.drop_index("idx_team_requests_business_status", table_name="team_requests")
    op.drop_table("team_requests")

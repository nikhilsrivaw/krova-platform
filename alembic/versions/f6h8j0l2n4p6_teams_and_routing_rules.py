"""Teams of staff, their members, routing rules that send a conversation to a team, and Customer.team_id.

Revision ID: f6h8j0l2n4p6
Revises: e5g7i9k1m3o5
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6h8j0l2n4p6"
down_revision: str = "e5g7i9k1m3o5"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("business_id", UUID, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("lead_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reply_target_minutes", sa.Integer(), nullable=True),
        sa.Column("sharing", sa.String(20), nullable=False, server_default="round_robin"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("business_id", "name", name="uq_team_name"),
    )
    op.create_index("idx_teams_business", "teams", ["business_id"])
    op.create_table(
        "team_members",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("team_id", UUID, sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("last_routed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("team_id", "user_id", name="uq_team_member"),
    )
    op.create_table(
        "routing_rules",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("business_id", UUID, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("conditions", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("team_id", UUID, sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("also_if_owned", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_routing_rules_business", "routing_rules", ["business_id", "position"])
    op.add_column("customers", sa.Column("team_id", UUID, sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True))


def downgrade() -> None:
    op.drop_column("customers", "team_id")
    op.drop_index("idx_routing_rules_business", table_name="routing_rules")
    op.drop_table("routing_rules")
    op.drop_table("team_members")
    op.drop_index("idx_teams_business", table_name="teams")
    op.drop_table("teams")

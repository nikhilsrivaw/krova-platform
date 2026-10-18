"""How often an automation rule may run for one customer.

Revision ID: f8b0d2e4a6c8
Revises: e7a9c1d3f5b7
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f8b0d2e4a6c8"
down_revision: str = "e7a9c1d3f5b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("post_call_action_rules", sa.Column("reentry_days", sa.Integer(), nullable=True))
    op.create_table(
        "automation_rule_customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("post_call_action_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("times", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("rule_id", "customer_id", name="uq_automation_rule_customer"),
    )


def downgrade() -> None:
    op.drop_table("automation_rule_customers")
    op.drop_column("post_call_action_rules", "reentry_days")

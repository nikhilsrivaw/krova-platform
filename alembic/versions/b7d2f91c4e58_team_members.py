"""Team v1: members with a Team ID + password, escalation owner, thread presence.

Revision ID: b7d2f91c4e58
Revises: a1c4e7f20b93
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d2f91c4e58"
down_revision: str = "a1c4e7f20b93"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_users_username", "users", ["username"])
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users", sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "created_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    # A team member may have only a Team ID: no email, no phone.
    op.drop_constraint("ck_users_email_or_phone", "users", type_="check")
    op.create_check_constraint(
        "ck_users_has_identifier", "users",
        "email IS NOT NULL OR phone IS NOT NULL OR username IS NOT NULL",
    )

    op.add_column(
        "escalations",
        sa.Column(
            "assigned_to_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    op.add_column("escalations", sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("customers", sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "thread_presence",
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column(
            "customer_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("typing", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_thread_presence_seen", "thread_presence", ["customer_id", "seen_at"])


def downgrade() -> None:
    op.drop_index("idx_thread_presence_seen", table_name="thread_presence")
    op.drop_table("thread_presence")
    op.drop_column("customers", "assigned_at")
    op.drop_column("escalations", "assigned_at")
    op.drop_column("escalations", "assigned_to_user_id")
    op.drop_constraint("ck_users_has_identifier", "users", type_="check")
    op.create_check_constraint(
        "ck_users_email_or_phone", "users", "email IS NOT NULL OR phone IS NOT NULL"
    )
    op.drop_column("users", "created_by_user_id")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_logins")
    op.drop_column("users", "must_change_password")
    op.drop_constraint("uq_users_username", "users", type_="unique")
    op.drop_column("users", "username")

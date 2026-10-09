"""Phone-only registration: users.email becomes optional.

Revision ID: 88161202625e
Revises: 269bc3318729
"""

import sqlalchemy as sa
from alembic import op

revision: str = "88161202625e"
down_revision: str = "269bc3318729"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "email", existing_type=sa.String(320), nullable=True)
    op.create_check_constraint(
        "ck_users_email_or_phone", "users", "email IS NOT NULL OR phone IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_email_or_phone", "users", type_="check")
    op.alter_column("users", "email", existing_type=sa.String(320), nullable=False)

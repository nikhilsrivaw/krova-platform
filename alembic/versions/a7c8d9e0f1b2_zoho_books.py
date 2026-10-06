"""Zoho Books connections, and commitments remember which system an external id came from.

Revision ID: a7c8d9e0f1b2
Revises: b3c4d5e6f7a8
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c8d9e0f1b2"
down_revision: str = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "zoho_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("organization_id", sa.String(64), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_summary", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column("commitments", sa.Column("source_system", sa.String(30), nullable=True))
    op.create_unique_constraint(
        "uq_commitment_source_ref", "commitments", ["business_id", "source_system", "external_ref"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_commitment_source_ref", "commitments", type_="unique")
    op.drop_column("commitments", "source_system")
    op.drop_table("zoho_connections")

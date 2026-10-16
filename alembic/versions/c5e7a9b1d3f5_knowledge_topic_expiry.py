"""Knowledge items can belong to a guided-setup topic and can expire.

Revision ID: c5e7a9b1d3f5
Revises: b4d6f8a0c2e4
"""

import sqlalchemy as sa
from alembic import op

revision: str = "c5e7a9b1d3f5"
down_revision: str = "b4d6f8a0c2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_items", sa.Column("topic", sa.String(60), nullable=True))
    op.add_column("knowledge_items", sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True))
    # One answer per guided topic per business (free-form items have no topic, and any number of them are fine).
    op.create_index(
        "uq_knowledge_topic", "knowledge_items", ["business_id", "topic"], unique=True,
        postgresql_where=sa.text("topic IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_knowledge_topic", table_name="knowledge_items")
    op.drop_column("knowledge_items", "valid_until")
    op.drop_column("knowledge_items", "topic")

"""Campaign.flow_id - bulk-send a WhatsApp Flow via a FLOW-button template

Revision ID: i2j3k4l5m6n7
Revises: h1i2j3k4l5m6
Create Date: 2026-09-12 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'i2j3k4l5m6n7'
down_revision: str | None = 'h1i2j3k4l5m6'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('campaigns', sa.Column('flow_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        'fk_campaigns_flow_id', 'campaigns', 'whatsapp_flows', ['flow_id'], ['id'], ondelete='SET NULL'
    )


def downgrade() -> None:
    op.drop_constraint('fk_campaigns_flow_id', 'campaigns', type_='foreignkey')
    op.drop_column('campaigns', 'flow_id')

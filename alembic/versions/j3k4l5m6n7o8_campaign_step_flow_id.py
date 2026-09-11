"""CampaignStep.flow_id - a follow-up step can open a Flow too

Revision ID: j3k4l5m6n7o8
Revises: i2j3k4l5m6n7
Create Date: 2026-09-12 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'j3k4l5m6n7o8'
down_revision: str | None = 'i2j3k4l5m6n7'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('campaign_steps', sa.Column('flow_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        'fk_campaign_steps_flow_id', 'campaign_steps', 'whatsapp_flows', ['flow_id'], ['id'], ondelete='SET NULL'
    )


def downgrade() -> None:
    op.drop_constraint('fk_campaign_steps_flow_id', 'campaign_steps', type_='foreignkey')
    op.drop_column('campaign_steps', 'flow_id')

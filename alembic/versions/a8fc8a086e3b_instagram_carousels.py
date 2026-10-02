"""Saved, reusable Instagram carousels - so the agent can send one by name

Revision ID: a8fc8a086e3b
Revises: 54350b20dbae
Create Date: 2026-10-02

shared/db/models/instagram_carousel.py's own docstring has the full
reasoning: pairs with the one-off carousel composer already shipped
(POST /instagram/carousel), giving the agent a named, reusable carousel
to offer during a live reply - the multi-option counterpart to
share_catalog's single on/off flag.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision: str = 'a8fc8a086e3b'
down_revision: str | None = '54350b20dbae'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'instagram_carousels',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('business_id', PgUUID(as_uuid=True),
                  sa.ForeignKey('businesses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('description', sa.String(300), nullable=False, server_default=''),
        sa.Column('elements', JSONB, nullable=False, server_default='[]'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('business_id', 'name', name='uq_instagram_carousel_name'),
    )


def downgrade() -> None:
    op.drop_table('instagram_carousels')

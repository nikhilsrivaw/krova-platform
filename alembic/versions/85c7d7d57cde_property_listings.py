"""property listings - real estate capability

Revision ID: 85c7d7d57cde
Revises: d3a675cd0ec8
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '85c7d7d57cde'
down_revision: str | None = 'd3a675cd0ec8'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('properties',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('listing_type', sa.String(length=10), nullable=False),
    sa.Column('property_type', sa.String(length=100), nullable=True),
    sa.Column('locality', sa.String(length=255), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('bedrooms', sa.Integer(), nullable=True),
    sa.Column('area_sqft', sa.Integer(), nullable=True),
    sa.Column('price_paise', sa.Integer(), nullable=True),
    sa.Column('price_period', sa.String(length=20), nullable=True),
    sa.Column('rera_registration_number', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_properties_business', 'properties', ['business_id'], unique=False)
    op.create_index('idx_properties_business_status', 'properties', ['business_id', 'status'], unique=False)

    op.add_column('appointments', sa.Column('property_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_appointments_property', 'appointments', 'properties', ['property_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_index('idx_appointments_property', 'appointments', ['property_id'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_appointments_property', table_name='appointments')
    op.drop_constraint('fk_appointments_property', 'appointments', type_='foreignkey')
    op.drop_column('appointments', 'property_id')

    op.drop_index('idx_properties_business_status', table_name='properties')
    op.drop_index('idx_properties_business', table_name='properties')
    op.drop_table('properties')

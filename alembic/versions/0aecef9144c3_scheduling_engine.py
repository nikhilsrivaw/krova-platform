"""scheduling engine - departments, doctors, availability, appointments

Revision ID: 0aecef9144c3
Revises: f4db91f0f87d
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '0aecef9144c3'
down_revision: str | None = 'f4db91f0f87d'
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('departments',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('business_id', 'name', name='uq_department_name_per_business')
    )
    op.create_index('idx_departments_business', 'departments', ['business_id'], unique=False)

    op.create_table('doctors',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('department_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('qualifications', sa.String(length=255), nullable=True),
    sa.Column('consultation_fee_paise', sa.Integer(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_doctors_business', 'doctors', ['business_id'], unique=False)

    op.create_table('availability_rules',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('doctor_id', sa.UUID(), nullable=False),
    sa.Column('weekday', sa.Integer(), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=False),
    sa.Column('end_time', sa.Time(), nullable=False),
    sa.Column('slot_duration_minutes', sa.Integer(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('weekday >= 0 AND weekday <= 6', name='ck_availability_weekday'),
    sa.CheckConstraint('end_time > start_time', name='ck_availability_time_order'),
    sa.CheckConstraint('slot_duration_minutes > 0', name='ck_availability_slot_duration'),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['doctor_id'], ['doctors.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_availability_doctor', 'availability_rules', ['doctor_id'], unique=False)

    op.create_table('availability_exceptions',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('doctor_id', sa.UUID(), nullable=False),
    sa.Column('date', sa.Date(), nullable=False),
    sa.Column('is_unavailable', sa.Boolean(), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=True),
    sa.Column('end_time', sa.Time(), nullable=True),
    sa.Column('reason', sa.String(length=255), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['doctor_id'], ['doctors.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_availability_exceptions_doctor_date', 'availability_exceptions', ['doctor_id', 'date'], unique=False)

    op.create_table('appointments',
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('doctor_id', sa.UUID(), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('intake_channel', sa.String(length=20), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('reminder_24h_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reminder_2h_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_message_ids', sa.ARRAY(sa.UUID()), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['doctor_id'], ['doctors.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_appointments_business', 'appointments', ['business_id'], unique=False)
    op.create_index('idx_appointments_customer', 'appointments', ['customer_id'], unique=False)
    op.create_index('idx_appointments_doctor_time', 'appointments', ['doctor_id', 'starts_at'], unique=False)
    op.create_index(
        'uq_appointments_doctor_slot', 'appointments', ['doctor_id', 'starts_at'],
        unique=True, postgresql_where=sa.text("status != 'cancelled'"),
    )


def downgrade() -> None:
    op.drop_index('uq_appointments_doctor_slot', table_name='appointments')
    op.drop_index('idx_appointments_doctor_time', table_name='appointments')
    op.drop_index('idx_appointments_customer', table_name='appointments')
    op.drop_index('idx_appointments_business', table_name='appointments')
    op.drop_table('appointments')

    op.drop_index('idx_availability_exceptions_doctor_date', table_name='availability_exceptions')
    op.drop_table('availability_exceptions')

    op.drop_index('idx_availability_doctor', table_name='availability_rules')
    op.drop_table('availability_rules')

    op.drop_index('idx_doctors_business', table_name='doctors')
    op.drop_table('doctors')

    op.drop_index('idx_departments_business', table_name='departments')
    op.drop_table('departments')

"""Appointment deposits: commitments.appointment_id, appointments.deposit_expires_at

Revision ID: z9a0b1c2d3e4
Revises: y8z9a0b1c2d3
Create Date: 2026-09-29

Type 4 (booked local service) gap-close, built generically for the whole
shape - salons, repair shops, studios, table-booking restaurants - not any
one of them. See docs/new/type-4-generic.md.

A business can require a deposit at booking time (Business.settings
["scheduling"]["deposit"]). The deposit is a normal Commitment - reusing
the existing, already-deployed WhatsApp Payments request/verify/record
loop (services/api/routers/ledger.py's request-payment endpoint,
services/api/routers/webhooks.py's payment-status verification) rather
than a new payment integration. commitments.appointment_id is the link
back from "this money arrived" to "so confirm this booking".
deposit_expires_at lets a sweep release a slot nobody ever paid the
deposit for.

No backfill: nullable, no existing row is a deposit.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = 'z9a0b1c2d3e4'
down_revision: str | None = 'y8z9a0b1c2d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'commitments',
        sa.Column(
            'appointment_id',
            PgUUID(as_uuid=True),
            sa.ForeignKey('appointments.id', ondelete='SET NULL', name='fk_commitments_appointment'),
            nullable=True,
        ),
    )
    op.create_index('ix_commitments_appointment_id', 'commitments', ['appointment_id'])
    op.add_column(
        'appointments',
        sa.Column('deposit_expires_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('appointments', 'deposit_expires_at')
    op.drop_index('ix_commitments_appointment_id', table_name='commitments')
    op.drop_constraint('fk_commitments_appointment', 'commitments', type_='foreignkey')
    op.drop_column('commitments', 'appointment_id')

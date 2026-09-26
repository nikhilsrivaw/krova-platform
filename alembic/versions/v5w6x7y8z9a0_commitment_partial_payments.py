"""Partial payments on commitments: amount_received_paise + payment_log

Revision ID: v5w6x7y8z9a0
Revises: u4v5w6x7y8z9
Create Date: 2026-09-25

A commitment keeps what was promised (amount_paise, untouched) and now
separately records what actually arrived, so "₹8,000 of ₹15,000 received"
is something the Ledger can say. See shared/care/commitment_payments.py.

Server defaults make every existing row "nothing received yet, no receipts",
which is exactly what they were. Commitments already marked met keep their
status; they are not back-filled as fully received, because nothing
recorded how or when that money came in and inventing receipts would be
inventing facts.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = 'v5w6x7y8z9a0'
down_revision: str | None = 'u4v5w6x7y8z9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'commitments',
        sa.Column('amount_received_paise', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'commitments',
        sa.Column('payment_log', JSONB(), nullable=False, server_default='[]'),
    )


def downgrade() -> None:
    op.drop_column('commitments', 'payment_log')
    op.drop_column('commitments', 'amount_received_paise')

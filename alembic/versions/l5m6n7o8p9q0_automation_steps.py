"""Automations engine phase 1: AutomationStep, backfilled 1:1 from existing rules

Revision ID: l5m6n7o8p9q0
Revises: k4l5m6n7o8p9
Create Date: 2026-09-12
"""
import json
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID

revision: str = 'l5m6n7o8p9q0'
down_revision: str | None = 'k4l5m6n7o8p9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'automation_steps',
        sa.Column('id', PgUUID(as_uuid=True), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('rule_id', PgUUID(as_uuid=True), sa.ForeignKey('post_call_action_rules.id', ondelete='CASCADE'), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('delay_seconds', sa.Integer(), nullable=True),
        sa.Column('condition', JSONB(), nullable=True),
        sa.Column('action_type', sa.String(length=50), nullable=False),
        sa.Column('action_config', JSONB(), nullable=False, server_default='{}'),
    )
    op.create_index('idx_automation_steps_rule', 'automation_steps', ['rule_id', 'position'])

    # Backfill: every existing rule becomes exactly one step, position 0, no
    # delay, no condition - copying its own action_type/action_config
    # verbatim. Lossless and behaviour-preserving on its own (nothing reads
    # from automation_steps yet); the next phase is what actually starts
    # executing from here instead of directly off post_call_action_rules.
    conn = op.get_bind()
    rules = conn.execute(sa.text(
        'SELECT id, action_type, action_config FROM post_call_action_rules'
    )).fetchall()
    if rules:
        def _as_json_text(value) -> str:
            # psycopg2 (Alembic's sync driver) already deserialises JSONB
            # into a dict; guard the (harmless either way) case where a
            # future driver swap hands back a JSON string instead.
            return value if isinstance(value, str) else json.dumps(value)

        conn.execute(
            sa.text(
                'INSERT INTO automation_steps '
                '(id, created_at, updated_at, rule_id, position, delay_seconds, condition, action_type, action_config) '
                'VALUES (:id, now(), now(), :rule_id, 0, NULL, NULL, :action_type, CAST(:action_config AS JSONB))'
            ),
            [
                {
                    'id': str(uuid.uuid4()),
                    'rule_id': str(row.id),
                    'action_type': row.action_type,
                    'action_config': _as_json_text(row.action_config),
                }
                for row in rules
            ],
        )


def downgrade() -> None:
    op.drop_index('idx_automation_steps_rule', table_name='automation_steps')
    op.drop_table('automation_steps')

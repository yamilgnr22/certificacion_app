"""agent base tables

Revision ID: 004_agent_base
Revises: 003_extend_cliente
Create Date: 2026-05-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "004_agent_base"
down_revision = "003_extend_cliente"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("periodo_id", sa.String(length=36), sa.ForeignKey("periodos_certificacion.id"), nullable=False),
        sa.Column("command_id", sa.String(length=80), nullable=False),
        sa.Column("cpa_user", sa.String(length=120), nullable=False, server_default="system"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=80), nullable=True),
        sa.Column("response_type", sa.String(length=40), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_messages_periodo_id", "agent_messages", ["periodo_id"])
    op.create_index("ix_agent_messages_command_id", "agent_messages", ["command_id"])

    op.create_table(
        "agent_proposals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("periodo_id", sa.String(length=36), sa.ForeignKey("periodos_certificacion.id"), nullable=False),
        sa.Column("command_id", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("payload_before_hash", sa.String(length=128), nullable=False),
        sa.Column("proposal_json", sa.Text(), nullable=False),
        sa.Column("projected_payload_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discarded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_proposals_periodo_id", "agent_proposals", ["periodo_id"])
    op.create_index("ix_agent_proposals_command_id", "agent_proposals", ["command_id"])
    op.create_index("ix_agent_proposals_status", "agent_proposals", ["status"])

    op.create_table(
        "legacy_call_counters",
        sa.Column("endpoint", sa.String(length=120), primary_key=True),
        sa.Column("call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("legacy_call_counters")
    op.drop_index("ix_agent_proposals_status", table_name="agent_proposals")
    op.drop_index("ix_agent_proposals_command_id", table_name="agent_proposals")
    op.drop_index("ix_agent_proposals_periodo_id", table_name="agent_proposals")
    op.drop_table("agent_proposals")
    op.drop_index("ix_agent_messages_command_id", table_name="agent_messages")
    op.drop_index("ix_agent_messages_periodo_id", table_name="agent_messages")
    op.drop_table("agent_messages")

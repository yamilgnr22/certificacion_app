"""Add structured agent session context.

Revision ID: 009_agent_session_context
Revises: 008_postable_account_catalog
Create Date: 2026-05-31
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "009_agent_session_context"
down_revision = "008_postable_account_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_session_context",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("periodo_id", sa.String(length=36), sa.ForeignKey("periodos_certificacion.id"), nullable=False),
        sa.Column("cpa_user", sa.String(length=120), nullable=False, server_default="system"),
        sa.Column("last_account", sa.String(length=120), nullable=True),
        sa.Column("last_month", sa.String(length=7), nullable=True),
        sa.Column("last_proposal_id", sa.String(length=36), nullable=True),
        sa.Column("pending_goal_message", sa.Text(), nullable=True),
        sa.Column("pending_goal_kind", sa.String(length=80), nullable=True),
        sa.Column("last_query_kind", sa.String(length=80), nullable=True),
        sa.Column("last_query_payload", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("periodo_id", "cpa_user", name="uq_agent_session_context_periodo_user"),
    )
    op.create_index("ix_agent_session_context_periodo_id", "agent_session_context", ["periodo_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_session_context_periodo_id", table_name="agent_session_context")
    op.drop_table("agent_session_context")

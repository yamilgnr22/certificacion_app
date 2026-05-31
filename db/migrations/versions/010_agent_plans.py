"""Add persistent agent plans.

Revision ID: 010_agent_plans
Revises: 009_agent_session_context
Create Date: 2026-05-31
"""

from __future__ import annotations

from alembic import op


revision = "010_agent_plans"
down_revision = "009_agent_session_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_plans (
            id TEXT PRIMARY KEY,
            periodo_id TEXT NOT NULL REFERENCES periodos_certificacion(id),
            cpa_user TEXT NOT NULL DEFAULT 'system',
            kind TEXT NOT NULL,
            user_message TEXT NOT NULL,
            plan_summary TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            aggregate_impact_json TEXT,
            status TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            applied_at TIMESTAMP,
            failed_step_order INTEGER,
            failure_reason TEXT
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_agent_plans_one_pending_per_period
            ON agent_plans(periodo_id, cpa_user)
            WHERE status = 'pending'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_agent_plans_periodo_id_status
            ON agent_plans(periodo_id, status)
        """
    )
    op.create_index("ix_agent_plans_periodo_id", "agent_plans", ["periodo_id"])
    op.create_index("ix_agent_plans_status", "agent_plans", ["status"])


def downgrade() -> None:
    op.drop_index("ix_agent_plans_status", table_name="agent_plans")
    op.drop_index("ix_agent_plans_periodo_id", table_name="agent_plans")
    op.execute("DROP INDEX IF EXISTS ix_agent_plans_periodo_id_status")
    op.execute("DROP INDEX IF EXISTS uq_agent_plans_one_pending_per_period")
    op.drop_table("agent_plans")

"""Motor V2: columna engine en periodos_certificacion.

Distingue el motor que interpreta payload_json: 'v1' = simulador legado
(financial_model.build_financial_model), 'v2' = motor determinista
(motor.certificar_tipo_a/b, payload = InputsTipoA/B JSON).

Revision ID: 011_motor_v2_engine
Revises: 010_agent_plans
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op


revision = "011_motor_v2_engine"
down_revision = "010_agent_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE periodos_certificacion ADD COLUMN engine TEXT NOT NULL DEFAULT 'v1'"
    )
    op.create_index(
        "ix_periodos_certificacion_engine", "periodos_certificacion", ["engine"]
    )


def downgrade() -> None:
    op.drop_index("ix_periodos_certificacion_engine", table_name="periodos_certificacion")
    op.execute("ALTER TABLE periodos_certificacion DROP COLUMN engine")

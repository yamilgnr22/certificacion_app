"""seed giros

Revision ID: 002_seed_giros
Revises: 001_initial
Create Date: 2026-05-17
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

from db.seed import DEFAULT_BALANCE_ACCOUNTS, GIROS_SEED


revision = "002_seed_giros"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = sa.table(
        "giros_negocio",
        sa.column("id", sa.String),
        sa.column("nombre", sa.String),
        sa.column("descripcion", sa.Text),
        sa.column("cost_pct_min", sa.Float),
        sa.column("cost_pct_max", sa.Float),
        sa.column("variabilidad_ingresos_pct", sa.Float),
        sa.column("variabilidad_costos_pct", sa.Float),
        sa.column("plantilla_gastos_json", sa.Text),
        sa.column("cuentas_balance_json", sa.Text),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
        sa.column("activo", sa.Integer),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        table,
        [
            {
                "id": item["id"],
                "nombre": item["nombre"],
                "descripcion": item.get("descripcion"),
                "cost_pct_min": item["cost_pct_min"],
                "cost_pct_max": item["cost_pct_max"],
                "variabilidad_ingresos_pct": item["variabilidad_ingresos_pct"],
                "variabilidad_costos_pct": item["variabilidad_costos_pct"],
                "plantilla_gastos_json": json.dumps(item["plantilla_gastos"], ensure_ascii=False, sort_keys=True),
                "cuentas_balance_json": json.dumps(DEFAULT_BALANCE_ACCOUNTS, ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
                "activo": 1,
            }
            for item in GIROS_SEED
        ],
    )


def downgrade() -> None:
    for item in GIROS_SEED:
        op.execute(sa.text(f"DELETE FROM giros_negocio WHERE id = '{item['id']}'"))

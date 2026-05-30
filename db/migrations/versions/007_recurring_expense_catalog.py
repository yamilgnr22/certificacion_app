"""recurring expense catalog metadata

Revision ID: 007_recurring_expense_catalog
Revises: 006_enrich_account_catalog
Create Date: 2026-05-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "007_recurring_expense_catalog"
down_revision = "006_enrich_account_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_catalog",
        sa.Column("is_recurring_expense", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("account_catalog", sa.Column("legacy_payload_key", sa.String(length=120), nullable=True))
    for code, legacy_key in {
        "exp_salaries": "Sueldos y Salarios",
        "exp_services": "Servicios Publicos",
        "exp_alcaldia_dgi": "Alcaldia y DGI",
        "exp_fuel": "Combustible",
        "exp_advertising": "Publicidad",
        "exp_maintenance": "Mantenimientos",
        "exp_rent": "Renta",
        "exp_insurance": "Seguros",
        "exp_other": "Otros Gastos",
    }.items():
        op.execute(
            sa.text(
                "UPDATE account_catalog "
                "SET is_recurring_expense = 1, legacy_payload_key = :legacy_key "
                "WHERE code = :code"
            ).bindparams(code=code, legacy_key=legacy_key)
        )


def downgrade() -> None:
    op.drop_column("account_catalog", "legacy_payload_key")
    op.drop_column("account_catalog", "is_recurring_expense")

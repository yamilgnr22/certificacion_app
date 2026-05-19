"""account catalog

Revision ID: 005_account_catalog
Revises: 004_agent_base
Create Date: 2026-05-18
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "005_account_catalog"
down_revision = "004_agent_base"
branch_labels = None
depends_on = None


SYSTEM_ACCOUNTS = [
    ("cash", "Efectivo y Equivalentes de Efectivo", "activo", "corriente"),
    ("accounts_receivable", "Cuentas por Cobrar Clientes", "activo", "corriente"),
    ("inventory", "Inventarios", "activo", "corriente"),
    ("ppe_real_estate", "Bienes Inmuebles", "activo", "no_corriente"),
    ("ppe_equipment", "Mobiliario y Equipos", "activo", "no_corriente"),
    ("ppe_vehicles", "Vehiculos", "activo", "no_corriente"),
    ("accum_depreciation", "Depreciacion Acumulada", "activo", "no_corriente"),
    ("credit_cards", "Tarjetas de Credito", "pasivo", "corriente"),
    ("suppliers", "Proveedores", "pasivo", "corriente"),
    ("taxes_payable", "Impuestos por Pagar", "pasivo", "corriente"),
    ("accrued_expenses", "Gastos Acumulados por pagar", "pasivo", "corriente"),
    ("loans_mortgage", "Creditos Hipotecarios", "pasivo", "no_corriente"),
    ("loans_consumo", "Creditos Consumo", "pasivo", "no_corriente"),
    ("loans_personal", "Creditos Personales", "pasivo", "no_corriente"),
    ("loans_pledge", "Creditos Prendarios", "pasivo", "no_corriente"),
    ("loans_commercial", "Creditos Comerciales", "pasivo", "no_corriente"),
    ("capital", "Capital", "patrimonio", "patrimonio"),
    ("retained_earnings", "Resultados Acumulados", "patrimonio", "patrimonio"),
    ("current_earnings", "Resultados del Ejercicio", "patrimonio", "patrimonio"),
    ("revenue", "Ingresos", "ingreso", "ingresos"),
    ("cogs", "Costo de Venta", "costo", "costo_ventas"),
    ("operating_expenses", "Gastos Operativos", "gasto", "gastos_operativos"),
    ("financial_expenses", "Gastos Financieros", "gasto", "gastos_financieros"),
    ("depreciation_expense", "Gasto por Depreciacion", "gasto", "gastos_operativos"),
]


def upgrade() -> None:
    op.create_table(
        "account_catalog",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("code", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=220), nullable=False),
        sa.Column("account_type", sa.String(length=40), nullable=False),
        sa.Column("section", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_account_catalog_code", "account_catalog", ["code"], unique=True)

    account_table = sa.table(
        "account_catalog",
        sa.column("id", sa.String),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("account_type", sa.String),
        sa.column("section", sa.String),
        sa.column("source", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("active", sa.Integer),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        account_table,
        [
            {
                "id": code,
                "code": code,
                "name": name,
                "account_type": account_type,
                "section": section,
                "source": "system",
                "created_at": now,
                "updated_at": now,
                "active": 1,
            }
            for code, name, account_type, section in SYSTEM_ACCOUNTS
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_account_catalog_code", table_name="account_catalog")
    op.drop_table("account_catalog")

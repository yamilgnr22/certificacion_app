"""Add postable flag to account catalog.

Revision ID: 008_postable_account_catalog
Revises: 007_recurring_expense_catalog
Create Date: 2026-05-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "008_postable_account_catalog"
down_revision = "007_recurring_expense_catalog"
branch_labels = None
depends_on = None


POSTABLE_CODES = (
    "cash",
    "accounts_receivable",
    "inventory",
    "ppe_real_estate",
    "ppe_equipment",
    "ppe_vehicles",
    "accum_depreciation",
    "suppliers",
    "credit_cards",
    "taxes_payable",
    "accrued_expenses",
    "loans_mortgage",
    "loans_consumo",
    "loans_personal",
    "loans_pledge",
    "loans_commercial",
    "capital",
    "retained_earnings",
    "current_earnings",
    "legal_reserve",
    "revenue",
    "cogs",
    "exp_salaries",
    "exp_services",
    "depreciation_expense",
    "financial_expenses",
    "exp_alcaldia_dgi",
    "exp_fuel",
    "exp_advertising",
    "exp_maintenance",
    "exp_rent",
    "exp_insurance",
    "exp_other",
)

PARENT_UPDATES = {
    "exp_salaries": "niif_611",
    "exp_services": "niif_612",
    "depreciation_expense": "niif_613",
    "financial_expenses": "niif_615",
    "exp_alcaldia_dgi": "niif_619",
    "exp_fuel": "niif_619",
    "exp_advertising": "niif_619",
    "exp_maintenance": "niif_619",
    "exp_rent": "niif_619",
    "exp_insurance": "niif_619",
    "exp_other": "niif_619",
}


def upgrade() -> None:
    op.add_column(
        "account_catalog",
        sa.Column("is_postable", sa.Integer(), nullable=False, server_default="0"),
    )
    with op.batch_alter_table("account_catalog") as batch_op:
        batch_op.create_check_constraint("ck_account_catalog_is_postable", "is_postable IN (0, 1)")
        batch_op.create_check_constraint(
            "ck_account_catalog_recurring_implies_postable",
            "is_recurring_expense = 0 OR is_postable = 1",
        )

    bind = op.get_bind()
    bind.execute(sa.text("UPDATE account_catalog SET is_postable = 0"))
    for code in POSTABLE_CODES:
        bind.execute(
            sa.text("UPDATE account_catalog SET is_postable = 1 WHERE code = :code"),
            {"code": code},
        )
    for code, parent in PARENT_UPDATES.items():
        bind.execute(
            sa.text("UPDATE account_catalog SET parent_code = :parent WHERE code = :code"),
            {"code": code, "parent": parent},
        )


def downgrade() -> None:
    with op.batch_alter_table("account_catalog") as batch_op:
        batch_op.drop_constraint("ck_account_catalog_recurring_implies_postable", type_="check")
        batch_op.drop_constraint("ck_account_catalog_is_postable", type_="check")
    op.drop_column("account_catalog", "is_postable")

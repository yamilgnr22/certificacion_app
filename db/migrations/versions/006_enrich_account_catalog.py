"""enrich account catalog

Revision ID: 006_enrich_account_catalog
Revises: 005_account_catalog
Create Date: 2026-05-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "006_enrich_account_catalog"
down_revision = "005_account_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("account_catalog", sa.Column("niif_code", sa.String(length=40), nullable=True))
    op.add_column("account_catalog", sa.Column("normal_balance", sa.String(length=20), nullable=True))
    op.add_column("account_catalog", sa.Column("parent_code", sa.String(length=120), nullable=True))
    op.add_column("account_catalog", sa.Column("aliases_json", sa.Text(), nullable=True))
    op.add_column("account_catalog", sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("account_catalog", sa.Column("required_model_account", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_account_catalog_niif_code", "account_catalog", ["niif_code"])
    op.create_index("ix_account_catalog_parent_code", "account_catalog", ["parent_code"])


def downgrade() -> None:
    op.drop_index("ix_account_catalog_parent_code", table_name="account_catalog")
    op.drop_index("ix_account_catalog_niif_code", table_name="account_catalog")
    op.drop_column("account_catalog", "required_model_account")
    op.drop_column("account_catalog", "display_order")
    op.drop_column("account_catalog", "aliases_json")
    op.drop_column("account_catalog", "parent_code")
    op.drop_column("account_catalog", "normal_balance")
    op.drop_column("account_catalog", "niif_code")

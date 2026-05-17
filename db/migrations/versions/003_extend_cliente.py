"""extend cliente with certification fields

Revision ID: 003_extend_cliente
Revises: 002_seed_giros
Create Date: 2026-05-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "003_extend_cliente"
down_revision = "002_seed_giros"
branch_labels = None
depends_on = None


NEW_COLUMNS = [
    ("sexo", sa.String(length=20)),
    ("estado_civil", sa.String(length=60)),
    ("profesion", sa.String(length=180)),
    ("banco", sa.String(length=120)),
    ("regimen", sa.String(length=80)),
    ("antiguedad", sa.String(length=80)),
    ("empleados", sa.String(length=80)),
    ("domicilio", sa.Text()),
]


def upgrade() -> None:
    with op.batch_alter_table("clientes") as batch:
        for name, col_type in NEW_COLUMNS:
            batch.add_column(sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("clientes") as batch:
        for name, _ in NEW_COLUMNS:
            batch.drop_column(name)

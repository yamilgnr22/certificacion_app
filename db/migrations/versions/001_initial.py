"""initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-05-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "giros_negocio",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("nombre", sa.String(length=180), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("cost_pct_min", sa.Float(), nullable=False),
        sa.Column("cost_pct_max", sa.Float(), nullable=False),
        sa.Column("variabilidad_ingresos_pct", sa.Float(), nullable=False),
        sa.Column("variabilidad_costos_pct", sa.Float(), nullable=False),
        sa.Column("plantilla_gastos_json", sa.Text(), nullable=False),
        sa.Column("cuentas_balance_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activo", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpa_user", sa.String(length=120), nullable=False),
        sa.Column("entity_type", sa.String(length=60), nullable=False),
        sa.Column("entity_id", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload_before_hash", sa.String(length=128), nullable=True),
        sa.Column("payload_after_hash", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("prev_entry_hash", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_entity_id", "audit_log", ["entity_id"])
    op.create_index("ix_audit_log_entity_type", "audit_log", ["entity_type"])
    op.create_index("ix_audit_log_timestamp", "audit_log", ["timestamp"])
    op.create_table(
        "clientes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("nombre_completo", sa.String(length=220), nullable=False),
        sa.Column("cedula", sa.String(length=40), nullable=False),
        sa.Column("fecha_nacimiento", sa.Date(), nullable=True),
        sa.Column("direccion_domicilio", sa.Text(), nullable=True),
        sa.Column("telefono", sa.String(length=80), nullable=True),
        sa.Column("email", sa.String(length=160), nullable=True),
        sa.Column("nombre_negocio", sa.String(length=220), nullable=False),
        sa.Column("ruc", sa.String(length=80), nullable=True),
        sa.Column("matricula_roc", sa.String(length=180), nullable=True),
        sa.Column("direccion_negocio", sa.Text(), nullable=False),
        sa.Column("giro_negocio_id", sa.String(length=80), nullable=False),
        sa.Column("fecha_inicio_negocio", sa.Date(), nullable=True),
        sa.Column("plantilla_gastos_json", sa.Text(), nullable=True),
        sa.Column("last_cedula_extracted_json", sa.Text(), nullable=True),
        sa.Column("last_matricula_extracted_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("activo", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["giro_negocio_id"], ["giros_negocio.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_clientes_cedula", "clientes", ["cedula"])
    op.create_index("ix_clientes_giro_negocio_id", "clientes", ["giro_negocio_id"])
    op.create_table(
        "periodos_certificacion",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cliente_id", sa.String(length=36), nullable=False),
        sa.Column("periodo_meses", sa.Integer(), nullable=False),
        sa.Column("mes_final", sa.String(length=7), nullable=False),
        sa.Column("mes_inicial", sa.String(length=7), nullable=False),
        sa.Column("estado", sa.String(length=30), nullable=False),
        sa.Column("tasa_cambio", sa.Float(), nullable=False),
        sa.Column("ingresos_base_usd", sa.Float(), nullable=True),
        sa.Column("variabilidad_ingresos_pct", sa.Float(), nullable=True),
        sa.Column("cost_pct", sa.Float(), nullable=True),
        sa.Column("variabilidad_costos_pct", sa.Float(), nullable=True),
        sa.Column("cash_sales_pct", sa.Float(), nullable=True),
        sa.Column("seed", sa.String(length=160), nullable=True),
        sa.Column("periodo_anterior_id", sa.String(length=36), nullable=True),
        sa.Column("saldos_iniciales_origen", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("period_blocks_json", sa.Text(), nullable=True),
        sa.Column("saldos_finales_json", sa.Text(), nullable=True),
        sa.Column("validation_json", sa.Text(), nullable=True),
        sa.Column("documento_path", sa.Text(), nullable=True),
        sa.Column("documento_generado_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recompute_required", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["cliente_id"], ["clientes.id"]),
        sa.ForeignKeyConstraint(["periodo_anterior_id"], ["periodos_certificacion.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_periodos_certificacion_cliente_id", "periodos_certificacion", ["cliente_id"])
    op.create_index("ix_periodos_certificacion_estado", "periodos_certificacion", ["estado"])
    op.create_index("ix_periodos_certificacion_mes_final", "periodos_certificacion", ["mes_final"])
    op.create_table(
        "documentos_soporte",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cliente_id", sa.String(length=36), nullable=False),
        sa.Column("tipo", sa.String(length=60), nullable=False),
        sa.Column("original_filename", sa.String(length=260), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("extracted_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cliente_id"], ["clientes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documentos_soporte_cliente_id", "documentos_soporte", ["cliente_id"])


def downgrade() -> None:
    op.drop_index("ix_documentos_soporte_cliente_id", table_name="documentos_soporte")
    op.drop_table("documentos_soporte")
    op.drop_index("ix_periodos_certificacion_mes_final", table_name="periodos_certificacion")
    op.drop_index("ix_periodos_certificacion_estado", table_name="periodos_certificacion")
    op.drop_index("ix_periodos_certificacion_cliente_id", table_name="periodos_certificacion")
    op.drop_table("periodos_certificacion")
    op.drop_index("ix_clientes_giro_negocio_id", table_name="clientes")
    op.drop_index("ix_clientes_cedula", table_name="clientes")
    op.drop_table("clientes")
    op.drop_index("ix_audit_log_timestamp", table_name="audit_log")
    op.drop_index("ix_audit_log_entity_type", table_name="audit_log")
    op.drop_index("ix_audit_log_entity_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("giros_negocio")

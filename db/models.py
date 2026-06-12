from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class GiroNegocio(Base):
    __tablename__ = "giros_negocio"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(180), nullable=False)
    descripcion: Mapped[str | None] = mapped_column(Text)
    cost_pct_min: Mapped[float] = mapped_column(Float, nullable=False)
    cost_pct_max: Mapped[float] = mapped_column(Float, nullable=False)
    variabilidad_ingresos_pct: Mapped[float] = mapped_column(Float, nullable=False)
    variabilidad_costos_pct: Mapped[float] = mapped_column(Float, nullable=False)
    plantilla_gastos_json: Mapped[str] = mapped_column(Text, nullable=False)
    cuentas_balance_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    activo: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    clientes: Mapped[list[Cliente]] = relationship(back_populates="giro")


class Cliente(Base):
    __tablename__ = "clientes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    nombre_completo: Mapped[str] = mapped_column(String(220), nullable=False)
    cedula: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    fecha_nacimiento: Mapped[datetime | None] = mapped_column(Date)
    direccion_domicilio: Mapped[str | None] = mapped_column(Text)
    telefono: Mapped[str | None] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(160))
    nombre_negocio: Mapped[str] = mapped_column(String(220), nullable=False)
    ruc: Mapped[str | None] = mapped_column(String(80))
    matricula_roc: Mapped[str | None] = mapped_column(String(180))
    direccion_negocio: Mapped[str] = mapped_column(Text, nullable=False)
    giro_negocio_id: Mapped[str] = mapped_column(ForeignKey("giros_negocio.id"), nullable=False, index=True)
    fecha_inicio_negocio: Mapped[datetime | None] = mapped_column(Date)
    plantilla_gastos_json: Mapped[str | None] = mapped_column(Text)
    last_cedula_extracted_json: Mapped[str | None] = mapped_column(Text)
    last_matricula_extracted_json: Mapped[str | None] = mapped_column(Text)
    # Campos de certificacion (rellenan el DOCX). Todos nullable.
    sexo: Mapped[str | None] = mapped_column(String(20))
    estado_civil: Mapped[str | None] = mapped_column(String(60))
    profesion: Mapped[str | None] = mapped_column(String(180))
    banco: Mapped[str | None] = mapped_column(String(120))
    regimen: Mapped[str | None] = mapped_column(String(80))
    antiguedad: Mapped[str | None] = mapped_column(String(80))
    empleados: Mapped[str | None] = mapped_column(String(80))
    domicilio: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(120))
    activo: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    giro: Mapped[GiroNegocio] = relationship(back_populates="clientes")
    periodos: Mapped[list[PeriodoCertificacion]] = relationship(back_populates="cliente")
    documentos: Mapped[list[DocumentoSoporte]] = relationship(back_populates="cliente")


class PeriodoCertificacion(Base):
    __tablename__ = "periodos_certificacion"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cliente_id: Mapped[str] = mapped_column(ForeignKey("clientes.id"), nullable=False, index=True)
    periodo_meses: Mapped[int] = mapped_column(Integer, nullable=False)
    mes_final: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    mes_inicial: Mapped[str] = mapped_column(String(7), nullable=False)
    estado: Mapped[str] = mapped_column(String(30), nullable=False, index=True, default="borrador")
    tasa_cambio: Mapped[float] = mapped_column(Float, nullable=False)
    ingresos_base_usd: Mapped[float | None] = mapped_column(Float)
    variabilidad_ingresos_pct: Mapped[float | None] = mapped_column(Float)
    cost_pct: Mapped[float | None] = mapped_column(Float)
    variabilidad_costos_pct: Mapped[float | None] = mapped_column(Float)
    cash_sales_pct: Mapped[float | None] = mapped_column(Float)
    seed: Mapped[str | None] = mapped_column(String(160))
    periodo_anterior_id: Mapped[str | None] = mapped_column(ForeignKey("periodos_certificacion.id"))
    saldos_iniciales_origen: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    period_blocks_json: Mapped[str | None] = mapped_column(Text)
    saldos_finales_json: Mapped[str | None] = mapped_column(Text)
    validation_json: Mapped[str | None] = mapped_column(Text)
    documento_path: Mapped[str | None] = mapped_column(Text)
    documento_generado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recompute_required: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(120))

    cliente: Mapped[Cliente] = relationship(back_populates="periodos")
    periodo_anterior: Mapped[PeriodoCertificacion | None] = relationship(remote_side=[id])


class DocumentoSoporte(Base):
    __tablename__ = "documentos_soporte"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cliente_id: Mapped[str] = mapped_column(ForeignKey("clientes.id"), nullable=False, index=True)
    tipo: Mapped[str] = mapped_column(String(60), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(260))
    path: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    cliente: Mapped[Cliente] = relationship(back_populates="documentos")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    cpa_user: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload_before_hash: Mapped[str | None] = mapped_column(String(128))
    payload_after_hash: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[str | None] = mapped_column(Text)
    prev_entry_hash: Mapped[str | None] = mapped_column(String(128))


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    periodo_id: Mapped[str] = mapped_column(ForeignKey("periodos_certificacion.id"), nullable=False, index=True)
    command_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    cpa_user: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(String(80))
    response_type: Mapped[str | None] = mapped_column(String(40))
    response_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AgentProposal(Base):
    __tablename__ = "agent_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    periodo_id: Mapped[str] = mapped_column(ForeignKey("periodos_certificacion.id"), nullable=False, index=True)
    command_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    payload_before_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    proposal_json: Mapped[str] = mapped_column(Text, nullable=False)
    projected_payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentPlan(Base):
    __tablename__ = "agent_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    periodo_id: Mapped[str] = mapped_column(ForeignKey("periodos_certificacion.id"), nullable=False, index=True)
    cpa_user: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    plan_summary: Mapped[str] = mapped_column(Text, nullable=False)
    steps_json: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_impact_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    payload_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_step_order: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index(
            "uq_agent_plans_one_pending_per_period",
            "periodo_id",
            "cpa_user",
            unique=True,
            sqlite_where=(status == "pending"),
        ),
        Index("ix_agent_plans_periodo_id_status", "periodo_id", "status"),
    )


class AgentSessionContext(Base):
    __tablename__ = "agent_session_context"
    __table_args__ = (
        UniqueConstraint("periodo_id", "cpa_user", name="uq_agent_session_context_periodo_user"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    periodo_id: Mapped[str] = mapped_column(ForeignKey("periodos_certificacion.id"), nullable=False, index=True)
    cpa_user: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    last_account: Mapped[str | None] = mapped_column(String(120))
    last_month: Mapped[str | None] = mapped_column(String(7))
    last_proposal_id: Mapped[str | None] = mapped_column(String(36))
    pending_goal_message: Mapped[str | None] = mapped_column(Text)
    pending_goal_kind: Mapped[str | None] = mapped_column(String(80))
    last_query_kind: Mapped[str | None] = mapped_column(String(80))
    last_query_payload: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class AccountCatalog(Base):
    __tablename__ = "account_catalog"
    __table_args__ = (
        CheckConstraint("is_postable IN (0, 1)", name="ck_account_catalog_is_postable"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    niif_code: Mapped[str | None] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(220), nullable=False)
    account_type: Mapped[str] = mapped_column(String(40), nullable=False)
    section: Mapped[str] = mapped_column(String(80), nullable=False)
    normal_balance: Mapped[str | None] = mapped_column(String(20))
    parent_code: Mapped[str | None] = mapped_column(String(120), index=True)
    aliases_json: Mapped[str | None] = mapped_column(Text)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    required_model_account: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_recurring_expense: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    legacy_payload_key: Mapped[str | None] = mapped_column(String(120))
    is_postable: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    active: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

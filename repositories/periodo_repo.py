from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import PeriodoCertificacion


class PeriodoRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, **data) -> PeriodoCertificacion:
        periodo = PeriodoCertificacion(**data)
        self.session.add(periodo)
        self.session.flush()
        return periodo

    def get(self, periodo_id: str) -> PeriodoCertificacion | None:
        return self.session.get(PeriodoCertificacion, periodo_id)

    def list_for_cliente(self, cliente_id: str) -> list[PeriodoCertificacion]:
        stmt = (
            select(PeriodoCertificacion)
            .where(PeriodoCertificacion.cliente_id == cliente_id)
            .order_by(PeriodoCertificacion.mes_final.desc(), PeriodoCertificacion.updated_at.desc())
        )
        return list(self.session.scalars(stmt))

    def latest_finalized_for_cliente(self, cliente_id: str) -> PeriodoCertificacion | None:
        stmt = (
            select(PeriodoCertificacion)
            .where(
                PeriodoCertificacion.cliente_id == cliente_id,
                PeriodoCertificacion.estado.in_(["finalizado", "certificado"]),
            )
            .order_by(PeriodoCertificacion.mes_final.desc())
            .limit(1)
        )
        return self.session.scalar(stmt)

    def has_certified_for_cliente(self, cliente_id: str) -> bool:
        stmt = (
            select(PeriodoCertificacion.id)
            .where(
                PeriodoCertificacion.cliente_id == cliente_id,
                PeriodoCertificacion.estado == "certificado",
            )
            .limit(1)
        )
        return self.session.scalar(stmt) is not None

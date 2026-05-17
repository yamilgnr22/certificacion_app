from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import GiroNegocio


class GiroRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, giro_id: str) -> GiroNegocio | None:
        return self.session.get(GiroNegocio, giro_id)

    def list_active(self) -> list[GiroNegocio]:
        stmt = select(GiroNegocio).where(GiroNegocio.activo == 1).order_by(GiroNegocio.nombre)
        return list(self.session.scalars(stmt))

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import Cliente


class ClienteRepositoryError(ValueError):
    pass


class ClienteRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, **data) -> Cliente:
        cliente = Cliente(**data)
        self.session.add(cliente)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise ClienteRepositoryError("Ya existe un cliente con esa cedula") from exc
        return cliente

    def get(self, cliente_id: str) -> Cliente | None:
        return self.session.get(Cliente, cliente_id)

    def find_by_cedula(self, cedula: str) -> Cliente | None:
        stmt = select(Cliente).where(Cliente.cedula == cedula, Cliente.activo == 1)
        return self.session.scalar(stmt)

    def has_active_cedula(self, cedula: str, *, exclude_id: str | None = None) -> bool:
        stmt = select(Cliente.id).where(Cliente.cedula == cedula, Cliente.activo == 1)
        if exclude_id:
            stmt = stmt.where(Cliente.id != exclude_id)
        return self.session.scalar(stmt) is not None

    def search(self, query: str = "", *, giro_id: str | None = None) -> list[Cliente]:
        stmt = select(Cliente).where(Cliente.activo == 1)
        query = (query or "").strip()
        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(
                or_(
                    Cliente.nombre_completo.ilike(pattern),
                    Cliente.cedula.ilike(pattern),
                    Cliente.nombre_negocio.ilike(pattern),
                    Cliente.ruc.ilike(pattern),
                )
            )
        if giro_id:
            stmt = stmt.where(Cliente.giro_negocio_id == giro_id)
        stmt = stmt.order_by(Cliente.nombre_completo)
        return list(self.session.scalars(stmt))

    def soft_delete(self, cliente_id: str) -> bool:
        cliente = self.get(cliente_id)
        if not cliente:
            return False
        cliente.activo = 0
        self.session.flush()
        return True

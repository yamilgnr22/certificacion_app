from __future__ import annotations

from sqlalchemy.orm import Session

from repositories import GiroRepository
from services.serializers import giro_to_dict


class GiroService:
    def __init__(self, session: Session):
        self.repo = GiroRepository(session)

    def list_active(self) -> list[dict]:
        return [giro_to_dict(giro) for giro in self.repo.list_active()]

    def get(self, giro_id: str) -> dict | None:
        giro = self.repo.get(giro_id)
        if not giro or not giro.activo:
            return None
        return giro_to_dict(giro)

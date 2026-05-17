from __future__ import annotations

import json

from sqlalchemy.orm import Session

from financial_model import DEFAULT_EXPENSES_USD
from repositories import ClienteRepository, GiroRepository
from services.serializers import parse_json_object


class PlantillaService:
    def __init__(self, session: Session):
        self.clientes = ClienteRepository(session)
        self.giros = GiroRepository(session)

    def effective_for_cliente(self, cliente_id: str) -> dict:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            return {"origen": "default", "plantilla": dict(DEFAULT_EXPENSES_USD)}
        giro = self.giros.get(cliente.giro_negocio_id)
        base = parse_json_object(giro.plantilla_gastos_json) if giro and giro.activo else dict(DEFAULT_EXPENSES_USD)
        override = parse_json_object(cliente.plantilla_gastos_json) if cliente.plantilla_gastos_json else {}
        merged = {**base, **override}
        if override:
            origen = "cliente"
        elif giro and giro.activo:
            origen = "giro"
        else:
            origen = "default"
        return {"origen": origen, "plantilla": merged}

    def set_cliente_template(self, cliente_id: str, plantilla: dict) -> None:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            raise KeyError("Cliente no encontrado")
        cliente.plantilla_gastos_json = json.dumps(plantilla, ensure_ascii=False, sort_keys=True)

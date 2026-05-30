from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from repositories import ClienteRepository, GiroRepository, PeriodoRepository
from repositories.cliente_repo import ClienteRepositoryError
from services.audit_service import AuditService
from services.plantilla_service import PlantillaService
from services.serializers import cliente_to_dict, periodo_to_basic_dict


class ServiceValidationError(ValueError):
    pass


class ServiceConflictError(ValueError):
    pass


class ClienteService:
    REQUIRED = ["nombre_completo", "cedula", "nombre_negocio", "direccion_negocio", "giro_negocio_id"]
    EDITABLE = {
        "nombre_completo",
        "cedula",
        "fecha_nacimiento",
        "direccion_domicilio",
        "telefono",
        "email",
        "nombre_negocio",
        "ruc",
        "matricula_roc",
        "direccion_negocio",
        "giro_negocio_id",
        "fecha_inicio_negocio",
        # Campos de certificacion
        "sexo",
        "estado_civil",
        "profesion",
        "banco",
        "regimen",
        "antiguedad",
        "empleados",
        "domicilio",
        "last_cedula_extracted_json",
        "last_matricula_extracted_json",
    }
    SEXO_VALIDOS = {"femenino", "masculino", "otro"}

    def __init__(self, session: Session):
        self.session = session
        self.clientes = ClienteRepository(session)
        self.giros = GiroRepository(session)
        self.periodos = PeriodoRepository(session)
        self.audit = AuditService(session)
        self.plantillas = PlantillaService(session)

    def list(self, *, query: str = "", giro_id: str | None = None) -> list[dict]:
        return [cliente_to_dict(cliente, include_giro=True) for cliente in self.clientes.search(query, giro_id=giro_id)]

    def get_detail(self, cliente_id: str) -> dict | None:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            return None
        return {
            "cliente": cliente_to_dict(cliente, include_giro=True),
            "periodos": [periodo_to_basic_dict(periodo) for periodo in self.periodos.list_for_cliente(cliente.id)],
            "plantilla_gastos": self.plantillas.effective_for_cliente(cliente.id),
        }

    def create(self, data: dict[str, Any], *, cpa_user: str = "system") -> dict:
        cleaned = self._clean_payload(data)
        self._validate_required(cleaned)
        self._validate_giro(cleaned["giro_negocio_id"])
        if self.clientes.has_active_cedula(cleaned["cedula"]):
            raise ServiceConflictError("Ya existe un cliente activo con esa cedula")
        try:
            cliente = self.clientes.create(**cleaned, created_by=cpa_user or "system")
            after = cliente_to_dict(cliente, include_giro=False)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="cliente",
                entity_id=cliente.id,
                action="create",
                summary=f"Creo cliente {cliente.nombre_completo}",
                after=after,
                metadata={"fields": sorted(cleaned.keys())},
            )
            self.session.commit()
            return cliente_to_dict(cliente, include_giro=True)
        except ClienteRepositoryError as exc:
            self.session.rollback()
            raise ServiceConflictError(str(exc)) from exc
        except Exception:
            self.session.rollback()
            raise

    def update(self, cliente_id: str, data: dict[str, Any], *, cpa_user: str = "system") -> dict | None:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            return None
        changes = self._clean_payload({key: value for key, value in data.items() if key in self.EDITABLE}, partial=True)
        if not changes:
            raise ServiceValidationError("No hay campos validos para actualizar")
        if "giro_negocio_id" in changes:
            self._validate_giro(changes["giro_negocio_id"])
        if "cedula" in changes and changes["cedula"] != cliente.cedula:
            if self.periodos.has_certified_for_cliente(cliente.id):
                raise ServiceConflictError("No se puede cambiar la cedula de un cliente con periodos certificados")
            if self.clientes.has_active_cedula(changes["cedula"], exclude_id=cliente.id):
                raise ServiceConflictError("Ya existe un cliente activo con esa cedula")

        before = cliente_to_dict(cliente, include_giro=False)
        try:
            for key, value in changes.items():
                setattr(cliente, key, value)
            self.session.flush()
            after = cliente_to_dict(cliente, include_giro=False)
            changed_fields = [key for key in sorted(changes) if before.get(key) != after.get(key)]
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="cliente",
                entity_id=cliente.id,
                action="update",
                summary=f"Actualizo cliente {cliente.nombre_completo}",
                before=before,
                after=after,
                metadata={"changed_fields": changed_fields},
            )
            self.session.commit()
            return cliente_to_dict(cliente, include_giro=True)
        except Exception:
            self.session.rollback()
            raise

    def soft_delete(self, cliente_id: str, *, cpa_user: str = "system") -> bool:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            return False
        before = cliente_to_dict(cliente, include_giro=False)
        try:
            self.clientes.soft_delete(cliente_id)
            after = cliente_to_dict(cliente, include_giro=False)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="cliente",
                entity_id=cliente.id,
                action="delete",
                summary=f"Desactivo cliente {cliente.nombre_completo}",
                before=before,
                after=after,
                metadata={"soft_delete": True},
            )
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            raise

    def set_plantilla(self, cliente_id: str, plantilla: dict[str, Any], *, cpa_user: str = "system") -> dict | None:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            return None
        cleaned = self._clean_template(plantilla)
        before = cliente_to_dict(cliente, include_giro=False)
        try:
            self.plantillas.set_cliente_template(cliente_id, cleaned)
            self.session.flush()
            after = cliente_to_dict(cliente, include_giro=False)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="cliente",
                entity_id=cliente.id,
                action="update_template",
                summary=f"Actualizo plantilla de gastos de {cliente.nombre_completo}",
                before=before,
                after=after,
                metadata={"changed_fields": ["plantilla_gastos_json"], "template_keys": sorted(cleaned.keys())},
            )
            self.session.commit()
            return self.plantillas.effective_for_cliente(cliente_id)
        except Exception:
            self.session.rollback()
            raise

    def _validate_required(self, data: dict[str, Any]) -> None:
        missing = [key for key in self.REQUIRED if not str(data.get(key) or "").strip()]
        if missing:
            raise ServiceValidationError(f"Campos requeridos faltantes: {', '.join(missing)}")

    def _validate_giro(self, giro_id: str) -> None:
        giro = self.giros.get(giro_id)
        if not giro or not giro.activo:
            raise ServiceValidationError("Giro de negocio invalido")

    def _clean_payload(self, data: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in data.items():
            if key not in self.EDITABLE:
                continue
            if isinstance(value, str):
                value = " ".join(value.strip().split())
            if value == "":
                value = None
            if key in {"fecha_nacimiento", "fecha_inicio_negocio"} and isinstance(value, str) and value:
                value = date.fromisoformat(value[:10])
            if key == "sexo" and value:
                normalized = str(value).strip().lower()
                if normalized not in self.SEXO_VALIDOS:
                    raise ServiceValidationError(
                        f"Sexo invalido. Valores aceptados: {', '.join(sorted(self.SEXO_VALIDOS))}"
                    )
                # Normalizamos capitalizando para mostrar consistente
                value = normalized.capitalize()
            if key in {"last_cedula_extracted_json", "last_matricula_extracted_json"} and value is not None:
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                else:
                    json.loads(str(value))
                    value = str(value)
            out[key] = value
        if not partial:
            for key in self.REQUIRED:
                out.setdefault(key, "")
        return out

    def _clean_template(self, plantilla: dict[str, Any]) -> dict[str, float]:
        if not isinstance(plantilla, dict):
            raise ServiceValidationError("La plantilla debe ser un objeto JSON")
        cleaned: dict[str, float] = {}
        for key, value in plantilla.items():
            name = str(key or "").strip()
            if not name:
                continue
            try:
                amount = float(value)
            except (TypeError, ValueError) as exc:
                raise ServiceValidationError(f"Monto invalido para {name}") from exc
            if amount < 0:
                raise ServiceValidationError(f"Monto negativo no permitido para {name}")
            cleaned[name] = amount
        if not cleaned:
            raise ServiceValidationError("La plantilla no puede estar vacia")
        return cleaned

"""Servicio de Periodos de Certificacion.

Orquesta creacion, lectura, edicion, preview, finalizacion y duplicacion
de periodos contables, en transacciones unicas con auditoria automatica.

Reglas de negocio:
- create() siempre nace en estado 'borrador'.
- update() solo permitido si estado == 'borrador'.
- finalize() pasa 'borrador' -> 'finalizado' y cachea saldos_finales.
- duplicate() crea un 'borrador' nuevo, sin documento ni saldos_finales.
- hard_delete() solo permitido si estado == 'borrador'.
- editar un periodo con descendientes (rollforward hijos) marca a esos hijos
  como recompute_required=1 para advertir en UI.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pandas as pd
from sqlalchemy.orm import Session

from db.models import GiroNegocio, PeriodoCertificacion
from financial_model import zero_balances
from repositories import ClienteRepository, GiroRepository, PeriodoRepository
from services.audit_service import AuditService
from services.periodo_document_adapter import periodo_to_dataframes
from services.rollforward_service import RollforwardService
from services.serializers import (
    cliente_to_dict,
    iso,
    parse_json_object,
    periodo_to_basic_dict,
)


DOCUMENTOS_DIRNAME = "documentos"


class PeriodoServiceError(ValueError):
    pass


class PeriodoValidationError(PeriodoServiceError):
    pass


class PeriodoConflictError(PeriodoServiceError):
    pass


class PeriodoNotFoundError(PeriodoServiceError):
    pass


class PeriodoService:
    # Campos editables (borrador) que el cliente UI puede modificar.
    EDITABLE = {
        "mes_inicial",
        "mes_final",
        "tasa_cambio",
        "ingresos_base_usd",
        "variabilidad_ingresos_pct",
        "cost_pct",
        "variabilidad_costos_pct",
        "cash_sales_pct",
        "seed",
        "saldos_iniciales_origen",
    }

    def __init__(self, session: Session):
        self.session = session
        self.periodos = PeriodoRepository(session)
        self.clientes = ClienteRepository(session)
        self.giros = GiroRepository(session)
        self.audit = AuditService(session)
        self.rollforward = RollforwardService(session)
        # Lazy import para evitar circularidad
        from services.plantilla_service import PlantillaService
        self.plantillas = PlantillaService(session)

    # ----------------------------------------------------------------- read
    def list_for_cliente(self, cliente_id: str) -> list[dict]:
        return [periodo_to_basic_dict(p) for p in self.periodos.list_for_cliente(cliente_id)]

    def list_editables(self) -> list[dict]:
        """Lista periodos para el selector del editor avanzado.

        Devuelve borradores primero (editables), despues finalizados/certificados
        (solo lectura). Solo de clientes activos.
        """
        from sqlalchemy import select
        from db.models import Cliente as ClienteModel
        from db.models import PeriodoCertificacion as Per

        # Orden custom: borrador=0, finalizado=1, certificado=2, otros=3
        # Dentro de cada grupo, por updated_at desc
        stmt = (
            select(Per, ClienteModel)
            .join(ClienteModel, Per.cliente_id == ClienteModel.id)
            .where(ClienteModel.activo == 1)
            .order_by(Per.updated_at.desc())
        )
        rows = list(self.session.execute(stmt))
        estado_rank = {"borrador": 0, "finalizado": 1, "certificado": 2}
        rows.sort(key=lambda r: (estado_rank.get(r[0].estado, 3), -(r[0].updated_at.timestamp() if r[0].updated_at else 0)))
        return [
            {
                "id": p.id,
                "cliente_id": p.cliente_id,
                "cliente_nombre": c.nombre_completo,
                "cliente_negocio": c.nombre_negocio,
                "mes_inicial": p.mes_inicial,
                "mes_final": p.mes_final,
                "estado": p.estado,
                "recompute_required": bool(p.recompute_required),
                "updated_at": iso(p.updated_at),
            }
            for p, c in rows
        ]

    def get_detail(self, periodo_id: str) -> dict | None:
        periodo = self.periodos.get(periodo_id)
        if not periodo:
            return None
        cliente = self.clientes.get(periodo.cliente_id) if periodo.cliente_id else None
        return {
            "periodo": self._full_dict(periodo),
            "cliente": cliente_to_dict(cliente, include_giro=True) if cliente else None,
        }

    # --------------------------------------------------------------- create
    def create(
        self,
        cliente_id: str,
        data: Mapping[str, Any],
        *,
        cpa_user: str = "system",
    ) -> dict:
        """Crea un periodo en estado borrador para el cliente.

        data puede contener:
          mes_inicial, mes_final, tasa_cambio, ingresos_base_usd,
          variabilidad_ingresos_pct, cost_pct, variabilidad_costos_pct,
          cash_sales_pct, seed,
          rollforward: bool  (si True, usar saldos del periodo anterior),
          balances_override: dict  (saldos iniciales manuales),
          expenses_override: dict  (plantilla de gastos custom).
        """
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            raise PeriodoNotFoundError("Cliente no encontrado o inactivo")

        meses = self._validate_meses(data)
        rollforward_info = None
        # Baseline en cero explicito: nunca heredar DEFAULT_BALANCES_NIO (saldos
        # de un negocio de ejemplo). El roll-forward y el override manual se
        # aplican encima de esta base (F7-T1).
        saldos_iniciales: dict[str, float] = zero_balances()
        periodo_anterior_id: str | None = None
        saldos_origen = "manual"

        if bool(data.get("rollforward")):
            rollforward_info = self.rollforward.propose_for_new_periodo(
                cliente_id, meses["mes_inicial"]
            )
            if rollforward_info.get("has_anterior"):
                saldos_iniciales.update(dict(rollforward_info.get("saldos") or {}))
                periodo_anterior_id = rollforward_info.get("periodo_anterior_id")
                saldos_origen = "rollforward"

        # Override manual gana sobre rollforward (UI puede ajustar lo propuesto).
        balances_override = data.get("balances_override") or {}
        if isinstance(balances_override, dict) and balances_override:
            saldos_iniciales.update({k: float(v) for k, v in balances_override.items() if v is not None})
            # Si hubo override sobre rollforward, marcamos origen mixto.
            if saldos_origen == "rollforward":
                saldos_origen = "rollforward_ajustado"
            else:
                saldos_origen = "manual"

        # Construir payload financiero
        # Si no se trajo expenses_override explicito, usar la plantilla efectiva del cliente
        if not data.get("expenses_override"):
            plantilla_efectiva = self.plantillas.effective_for_cliente(cliente.id)
            data = dict(data)  # copia para no mutar el input del caller
            data["expenses_override"] = dict(plantilla_efectiva.get("plantilla") or {})
            plantilla_origen = plantilla_efectiva.get("origen", "default")
            plantilla_warnings = list(plantilla_efectiva.get("warnings") or [])
        else:
            expenses_override, plantilla_warnings = self.plantillas.engine_expenses_from_template(data.get("expenses_override") or {})
            data = dict(data)
            data["expenses_override"] = expenses_override
            plantilla_origen = "override_explicito"

        payload = self._build_payload(
            meses=meses,
            data=data,
            cliente=cliente,
            balances=saldos_iniciales,
        )

        # Ejecutar modelo financiero para validaciones iniciales
        validation_json = self._run_and_capture_validation(payload)

        try:
            periodo = self.periodos.create(
                cliente_id=cliente_id,
                periodo_meses=meses["periodo_meses"],
                mes_inicial=meses["mes_inicial"],
                mes_final=meses["mes_final"],
                estado="borrador",
                tasa_cambio=float(payload["period"]["exchange_rate"]),
                ingresos_base_usd=_opt_float(payload["income"].get("base_income_usd")),
                variabilidad_ingresos_pct=_opt_float(payload["income"].get("income_variability_pct")),
                cost_pct=_opt_float(payload["income"].get("cost_pct")),
                variabilidad_costos_pct=_opt_float(payload["income"].get("cost_variability_pct")),
                cash_sales_pct=_opt_float(payload["income"].get("cash_sales_pct")),
                seed=str(payload["period"].get("seed") or ""),
                periodo_anterior_id=periodo_anterior_id,
                saldos_iniciales_origen=saldos_origen,
                payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                validation_json=json.dumps(validation_json, ensure_ascii=False, sort_keys=True, default=str),
                created_by=cpa_user or "system",
            )

            after_snapshot = self._full_dict(periodo)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="create",
                summary=f"Creo periodo {periodo.mes_inicial}..{periodo.mes_final} para cliente {cliente.nombre_completo}",
                after=after_snapshot,
                metadata={
                    "cliente_id": cliente.id,
                    "rollforward": bool(rollforward_info and rollforward_info.get("has_anterior")),
                    "warning": (rollforward_info or {}).get("warning"),
                    "plantilla_origen": plantilla_origen,
                    "plantilla_warnings": plantilla_warnings,
                },
            )
            self.session.commit()
            return {
                "periodo": after_snapshot,
                "rollforward": rollforward_info,
            }
        except Exception:
            self.session.rollback()
            raise

    # --------------------------------------------------------------- update
    def update(
        self,
        periodo_id: str,
        data: Mapping[str, Any],
        *,
        cpa_user: str = "system",
    ) -> dict:
        periodo = self.periodos.get(periodo_id)
        if not periodo:
            raise PeriodoNotFoundError("Periodo no encontrado")
        if periodo.estado != "borrador":
            raise PeriodoConflictError(
                f"No se puede editar un periodo en estado '{periodo.estado}'. "
                "Para corregirlo duplicalo como borrador."
            )

        changes = {key: data.get(key) for key in data.keys() if key in self.EDITABLE}
        if not changes:
            raise PeriodoValidationError("No hay campos editables en la solicitud")

        # Validar nuevos meses si cambian
        new_meses = {
            "mes_inicial": str(changes.get("mes_inicial") or periodo.mes_inicial),
            "mes_final": str(changes.get("mes_final") or periodo.mes_final),
        }
        meses = self._validate_meses(new_meses)

        before = self._full_dict(periodo)

        try:
            # Rehacer payload con cambios
            payload = parse_json_object(periodo.payload_json)
            period_block = dict(payload.get("period") or {})
            period_block["start_month"] = meses["mes_inicial"]
            period_block["end_month"] = meses["mes_final"]
            period_block["months"] = meses["periodo_meses"]
            if "tasa_cambio" in changes and changes["tasa_cambio"] is not None:
                period_block["exchange_rate"] = float(changes["tasa_cambio"])
            if "seed" in changes and changes["seed"]:
                period_block["seed"] = str(changes["seed"])
            payload["period"] = period_block

            income_block = dict(payload.get("income") or {})
            for src, dst in (
                ("ingresos_base_usd", "base_income_usd"),
                ("variabilidad_ingresos_pct", "income_variability_pct"),
                ("cost_pct", "cost_pct"),
                ("variabilidad_costos_pct", "cost_variability_pct"),
                ("cash_sales_pct", "cash_sales_pct"),
            ):
                if src in changes and changes[src] is not None:
                    income_block[dst] = float(changes[src])
            payload["income"] = income_block

            validation_json = self._run_and_capture_validation(payload)

            periodo.mes_inicial = meses["mes_inicial"]
            periodo.mes_final = meses["mes_final"]
            periodo.periodo_meses = meses["periodo_meses"]
            for src in ("tasa_cambio", "ingresos_base_usd", "variabilidad_ingresos_pct",
                        "cost_pct", "variabilidad_costos_pct", "cash_sales_pct", "seed"):
                if src in changes and changes[src] is not None:
                    setattr(periodo, src, changes[src] if src == "seed" else float(changes[src]))
            if "saldos_iniciales_origen" in changes and changes["saldos_iniciales_origen"]:
                periodo.saldos_iniciales_origen = str(changes["saldos_iniciales_origen"])
            periodo.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            periodo.validation_json = json.dumps(validation_json, ensure_ascii=False, sort_keys=True, default=str)

            self.session.flush()
            # Invalidar descendientes (raro en borrador, pero por consistencia)
            invalidated = self.rollforward.invalidate_descendants(periodo.id)

            after = self._full_dict(periodo)
            changed_fields = sorted([k for k in changes.keys() if before.get(k) != after.get(k)])
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="update",
                summary=f"Actualizo periodo {periodo.mes_inicial}..{periodo.mes_final}",
                before=before,
                after=after,
                metadata={"changed_fields": changed_fields, "invalidated_descendants": invalidated},
            )
            self.session.commit()
            return {"periodo": after, "invalidated_descendants": invalidated}
        except Exception:
            self.session.rollback()
            raise

    # ------------------------------------------------------------- update_payload
    def update_payload(
        self,
        periodo_id: str,
        new_payload: Mapping[str, Any],
        *,
        cpa_user: str = "system",
    ) -> dict:
        """Reemplaza el payload completo del periodo (solo borradores).

        - Solo permitido si estado=borrador.
        - Recalcula validation_json.
        - Marca recompute_required en descendants.
        - Audit con changed_blocks (top-level keys que cambiaron).
        """
        periodo = self.periodos.get(periodo_id)
        if not periodo:
            raise PeriodoNotFoundError("Periodo no encontrado")
        if periodo.estado != "borrador":
            raise PeriodoConflictError(
                f"Solo se puede editar el payload de un borrador. Estado actual: '{periodo.estado}'."
            )
        if not isinstance(new_payload, Mapping):
            raise PeriodoValidationError("El payload debe ser un objeto JSON")
        if not new_payload.get("period"):
            raise PeriodoValidationError("Falta el bloque 'period' en el payload")

        before = self._full_dict(periodo)
        old_payload = parse_json_object(periodo.payload_json)
        try:
            # Sincronizar campos individuales del periodo con el payload
            period_block = dict(new_payload.get("period") or {})
            income_block = dict(new_payload.get("income") or {})
            # Si el payload trae mes_inicial/final, validamos
            mes_inicial = str(period_block.get("start_month") or period_block.get("mes_inicio") or periodo.mes_inicial)[:7]
            mes_final = str(period_block.get("end_month") or period_block.get("mes_final") or periodo.mes_final)[:7]
            meses = self._validate_meses({"mes_inicial": mes_inicial, "mes_final": mes_final})
            periodo.mes_inicial = meses["mes_inicial"]
            periodo.mes_final = meses["mes_final"]
            periodo.periodo_meses = meses["periodo_meses"]
            if period_block.get("exchange_rate") is not None:
                periodo.tasa_cambio = float(period_block["exchange_rate"])
            if period_block.get("seed"):
                periodo.seed = str(period_block["seed"])
            for src, attr in (
                ("base_income_usd", "ingresos_base_usd"),
                ("income_variability_pct", "variabilidad_ingresos_pct"),
                ("cost_pct", "cost_pct"),
                ("cost_variability_pct", "variabilidad_costos_pct"),
                ("cash_sales_pct", "cash_sales_pct"),
            ):
                if income_block.get(src) is not None:
                    setattr(periodo, attr, float(income_block[src]))

            payload_dict = dict(new_payload)
            validation_json = self._run_and_capture_validation(payload_dict)
            periodo.payload_json = json.dumps(payload_dict, ensure_ascii=False, sort_keys=True, default=str)
            periodo.validation_json = json.dumps(validation_json, ensure_ascii=False, sort_keys=True, default=str)

            self.session.flush()
            invalidated = self.rollforward.invalidate_descendants(periodo.id)

            # Detectar bloques cambiados a nivel top-level
            changed_blocks = sorted({
                k for k in set(old_payload.keys()) | set(payload_dict.keys())
                if old_payload.get(k) != payload_dict.get(k)
            })

            after = self._full_dict(periodo)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="update_payload",
                summary=f"Actualizo payload del periodo {periodo.mes_inicial}..{periodo.mes_final}",
                before=before,
                after=after,
                metadata={
                    "changed_blocks": changed_blocks,
                    "invalidated_descendants": invalidated,
                },
            )
            self.session.commit()
            return {"periodo": after, "invalidated_descendants": invalidated, "changed_blocks": changed_blocks}
        except (PeriodoValidationError, PeriodoConflictError, PeriodoNotFoundError):
            self.session.rollback()
            raise
        except Exception:
            self.session.rollback()
            raise

    # ----------------------------------------------------------------- preview
    def preview(self, periodo_id: str) -> dict:
        """Recalcula el modelo financiero del payload actual sin persistir."""
        periodo = self.periodos.get(periodo_id)
        if not periodo:
            raise PeriodoNotFoundError("Periodo no encontrado")
        payload = parse_json_object(periodo.payload_json)
        from financial_model import build_financial_model, result_to_json
        result = build_financial_model(payload)
        return result_to_json(result)

    # ---------------------------------------------------------------- finalize
    def finalize(self, periodo_id: str, *, cpa_user: str = "system") -> dict:
        periodo = self.periodos.get(periodo_id)
        if not periodo:
            raise PeriodoNotFoundError("Periodo no encontrado")
        if periodo.estado not in ("borrador",):
            raise PeriodoConflictError(
                f"Solo se puede finalizar un periodo en estado 'borrador'. Estado actual: '{periodo.estado}'."
            )

        before = self._full_dict(periodo)
        try:
            periodo.estado = "finalizado"
            periodo.finalized_at = _utc_now()
            # Calcular y cachear saldos finales para roll-forward
            self.rollforward.cache_saldos_finales(periodo)
            self.session.flush()
            after = self._full_dict(periodo)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="finalize",
                summary=f"Finalizo periodo {periodo.mes_inicial}..{periodo.mes_final}",
                before=before,
                after=after,
                metadata={"saldos_finales_keys": sorted(json.loads(periodo.saldos_finales_json or "{}").keys())},
            )
            self.session.commit()
            return {"periodo": after}
        except Exception:
            self.session.rollback()
            raise

    # --------------------------------------------------------------- duplicate
    def duplicate(self, periodo_id: str, *, cpa_user: str = "system") -> dict:
        original = self.periodos.get(periodo_id)
        if not original:
            raise PeriodoNotFoundError("Periodo no encontrado")

        try:
            clone = self.periodos.create(
                cliente_id=original.cliente_id,
                periodo_meses=original.periodo_meses,
                mes_inicial=original.mes_inicial,
                mes_final=original.mes_final,
                estado="borrador",
                tasa_cambio=original.tasa_cambio,
                ingresos_base_usd=original.ingresos_base_usd,
                variabilidad_ingresos_pct=original.variabilidad_ingresos_pct,
                cost_pct=original.cost_pct,
                variabilidad_costos_pct=original.variabilidad_costos_pct,
                cash_sales_pct=original.cash_sales_pct,
                seed=original.seed,
                periodo_anterior_id=original.periodo_anterior_id,
                saldos_iniciales_origen=original.saldos_iniciales_origen,
                payload_json=original.payload_json,
                period_blocks_json=original.period_blocks_json,
                saldos_finales_json=None,
                validation_json=original.validation_json,
                documento_path=None,
                documento_generado_at=None,
                recompute_required=0,
                created_by=cpa_user or "system",
            )
            after = self._full_dict(clone)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=clone.id,
                action="duplicate",
                summary=f"Duplico periodo {original.id} como borrador {clone.id}",
                after=after,
                metadata={"source_periodo_id": original.id},
            )
            self.session.commit()
            return {"periodo": after, "source_periodo_id": original.id}
        except Exception:
            self.session.rollback()
            raise

    # ----------------------------------------------------------------- delete
    def hard_delete(self, periodo_id: str, *, cpa_user: str = "system") -> bool:
        periodo = self.periodos.get(periodo_id)
        if not periodo:
            return False
        if periodo.estado != "borrador":
            raise PeriodoConflictError(
                f"Solo se puede eliminar un periodo en estado 'borrador'. Estado actual: '{periodo.estado}'."
            )
        before = self._full_dict(periodo)
        try:
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="delete",
                summary=f"Elimino borrador {periodo.mes_inicial}..{periodo.mes_final}",
                before=before,
                metadata={"hard_delete": True},
            )
            self.session.delete(periodo)
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            raise

    # ------------------------------------------------------- generate document
    def generate_document(self, periodo_id: str, *, cpa_user: str = "system") -> dict:
        """Genera el DOCX y persiste documento_path/documento_generado_at.

        Reglas:
          - Solo periodos en estado 'finalizado' o 'certificado'.
          - Sobrescribe si ya existia (regenerar permitido).
          - Audit log con action='generate_document'.
        """
        from datetime import datetime, timezone
        from pathlib import Path

        from document_generator import generar_documento_completo

        periodo = self.periodos.get(periodo_id)
        if not periodo:
            raise PeriodoNotFoundError("Periodo no encontrado")
        if periodo.estado not in ("finalizado", "certificado"):
            raise PeriodoConflictError(
                "Solo se puede generar el documento de un periodo finalizado. "
                f"Estado actual: '{periodo.estado}'."
            )
        cliente = self.clientes.get(periodo.cliente_id)
        if not cliente:
            raise PeriodoNotFoundError("Cliente del periodo no encontrado")
        giro = self.giros.get(cliente.giro_negocio_id) if cliente.giro_negocio_id else None

        before = self._full_dict(periodo)
        try:
            df_esf, df_er, df_datos, df_cert = periodo_to_dataframes(periodo, cliente, giro)

            out_dir = _documentos_dir() / cliente.id
            out_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{periodo.id}_{periodo.mes_inicial}_{periodo.mes_final}.docx"
            out_path = out_dir / filename

            generar_documento_completo(
                df_esf,
                df_er,
                df_datos,
                df_cert,
                str(out_path),
                incluir_validacion=False,
                tolerancia_validacion=1.0,
                detener_si_error=False,
                validacion_documentos=None,
                validacion_llm=None,
                esf_tipo="mensual",
            )

            periodo.documento_path = str(out_path)
            periodo.documento_generado_at = datetime.now(timezone.utc)
            self.session.flush()

            after = self._full_dict(periodo)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="generate_document",
                summary=f"Genero documento DOCX para {periodo.mes_inicial}..{periodo.mes_final}",
                before=before,
                after=after,
                metadata={"documento_path": str(out_path), "size_bytes": out_path.stat().st_size},
            )
            self.session.commit()
            return {"periodo": after, "documento_path": str(out_path)}
        except (PeriodoConflictError, PeriodoNotFoundError):
            self.session.rollback()
            raise
        except Exception:
            self.session.rollback()
            raise

    def get_document_path(self, periodo_id: str) -> str | None:
        """Devuelve la ruta absoluta del DOCX si existe, sino None."""
        from pathlib import Path

        periodo = self.periodos.get(periodo_id)
        if not periodo or not periodo.documento_path:
            return None
        p = Path(periodo.documento_path)
        return str(p) if p.exists() else None

    # ----------------------------------------------------- rollforward preview
    def rollforward_preview(self, cliente_id: str, mes_inicial: str) -> dict:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            raise PeriodoNotFoundError("Cliente no encontrado o inactivo")
        # Validar formato del mes
        self._validate_mes_key(mes_inicial, "mes_inicial")
        return self.rollforward.propose_for_new_periodo(cliente_id, mes_inicial)

    # ----------------------------------------------------------------- internals
    def _full_dict(self, periodo: PeriodoCertificacion) -> dict[str, Any]:
        base = periodo_to_basic_dict(periodo)
        base.update({
            "tasa_cambio": periodo.tasa_cambio,
            "ingresos_base_usd": periodo.ingresos_base_usd,
            "variabilidad_ingresos_pct": periodo.variabilidad_ingresos_pct,
            "cost_pct": periodo.cost_pct,
            "variabilidad_costos_pct": periodo.variabilidad_costos_pct,
            "cash_sales_pct": periodo.cash_sales_pct,
            "seed": periodo.seed,
            "periodo_anterior_id": periodo.periodo_anterior_id,
            "saldos_iniciales_origen": periodo.saldos_iniciales_origen,
            "documento_path": periodo.documento_path,
            "documento_generado_at": iso(periodo.documento_generado_at),
            "recompute_required": bool(periodo.recompute_required),
            "saldos_finales": parse_json_object(periodo.saldos_finales_json) if periodo.saldos_finales_json else None,
            "validation": parse_json_object(periodo.validation_json) if periodo.validation_json else None,
            "payload": parse_json_object(periodo.payload_json) if periodo.payload_json else {},
        })
        return base

    def _validate_meses(self, data: Mapping[str, Any]) -> dict[str, Any]:
        mes_inicial = str(data.get("mes_inicial") or "").strip()[:7]
        mes_final = str(data.get("mes_final") or "").strip()[:7]
        self._validate_mes_key(mes_inicial, "mes_inicial")
        self._validate_mes_key(mes_final, "mes_final")
        try:
            inicio = pd.to_datetime(f"{mes_inicial}-01")
            fin = pd.to_datetime(f"{mes_final}-01")
        except Exception as exc:
            raise PeriodoValidationError("Formato de mes invalido (use YYYY-MM)") from exc
        if inicio > fin:
            raise PeriodoValidationError("mes_inicial debe ser anterior o igual a mes_final")
        meses = (fin.year - inicio.year) * 12 + (fin.month - inicio.month) + 1
        if meses < 1 or meses > 60:
            raise PeriodoValidationError("Rango invalido: debe abarcar entre 1 y 60 meses")
        return {"mes_inicial": mes_inicial, "mes_final": mes_final, "periodo_meses": meses}

    @staticmethod
    def _validate_mes_key(value: str, label: str) -> None:
        if not value or len(value) != 7 or value[4] != "-":
            raise PeriodoValidationError(f"{label} debe tener formato YYYY-MM")
        try:
            pd.to_datetime(f"{value}-01")
        except Exception as exc:
            raise PeriodoValidationError(f"{label} no es un mes valido") from exc

    @staticmethod
    def _build_payload(
        *,
        meses: dict[str, Any],
        data: Mapping[str, Any],
        cliente,
        balances: dict[str, float],
    ) -> dict[str, Any]:
        payload = {
            "period": {
                "start_month": meses["mes_inicial"],
                "end_month": meses["mes_final"],
                "months": meses["periodo_meses"],
                "exchange_rate": _opt_float(data.get("tasa_cambio")) or 36.6243,
                "seed": str(data.get("seed") or f"{cliente.id[:8]}-{meses['mes_final']}"),
            },
            "income": {},
            "expenses": {},
            "balances": dict(balances),
            "movements": {},
        }
        for src, dst in (
            ("ingresos_base_usd", "base_income_usd"),
            ("variabilidad_ingresos_pct", "income_variability_pct"),
            ("cost_pct", "cost_pct"),
            ("variabilidad_costos_pct", "cost_variability_pct"),
            ("cash_sales_pct", "cash_sales_pct"),
        ):
            value = data.get(src)
            if value is not None and value != "":
                try:
                    payload["income"][dst] = float(value)
                except (TypeError, ValueError):
                    continue
        # expenses override (opcional)
        expenses_override = data.get("expenses_override") or {}
        if isinstance(expenses_override, dict):
            payload["expenses"] = {k: float(v) for k, v in expenses_override.items() if v is not None}
        return payload

    @staticmethod
    def _run_and_capture_validation(payload: Mapping[str, Any]) -> dict[str, Any]:
        from financial_model import build_financial_model
        try:
            result = build_financial_model(payload)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": bool(
                result.validations.get("er", {}).get("ok")
                and result.validations.get("esf", {}).get("ok")
                and result.validations.get("balance", {}).get("ok")
            ),
            "er": result.validations.get("er"),
            "esf": result.validations.get("esf"),
            "balance": result.validations.get("balance"),
        }


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _documentos_dir():
    """Carpeta raiz para documentos generados (configurable via env)."""
    import os
    from pathlib import Path
    base = os.getenv("CERTAPP_DOCUMENTOS_DIR")
    if base:
        return Path(base)
    return Path(__file__).resolve().parents[1] / "data" / DOCUMENTOS_DIRNAME

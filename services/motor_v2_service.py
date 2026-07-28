"""Persistencia de certificaciones del Motor V2.

Los borradores/finales del motor V2 se guardan como PeriodoCertificacion
(engine='v2') en SQLite: misma entidad, estados, auditoria y documentos que
el resto de la app. payload_json guarda el JSON de InputsTipoA/B tal como lo
envia la UI (el mismo shape que consume /api/motor/v2/certificar).

Ciclo de vida:
  crear_borrador -> actualizar (n veces) -> finalizar (motor OK ->
  DOCX con notas a disco + saldos finales cacheados + estado 'finalizado').

Roll-forward V2: saldos_rollforward(cliente) propone los saldos iniciales del
siguiente periodo desde el ultimo finalizado v2 (Resultados Acumulados nuevos
= RA del corte + Resultados del Ejercicio del corte; asi el Capital derivado
se mantiene constante entre periodos).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import PeriodoCertificacion
from repositories import ClienteRepository, PeriodoRepository
from services.audit_service import AuditService
from services.periodo_service import (
    PeriodoConflictError,
    PeriodoNotFoundError,
    PeriodoValidationError,
    _documentos_dir,
)
from services.serializers import parse_json_object, periodo_to_basic_dict


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _meses_count(mes_inicial: str, mes_final: str) -> int:
    y1, m1 = int(mes_inicial[:4]), int(mes_inicial[5:7])
    y2, m2 = int(mes_final[:4]), int(mes_final[5:7])
    return (y2 - y1) * 12 + (m2 - m1) + 1


# Campos del snapshot de corte que se cachean al finalizar (todo NIO).
_CUENTAS_CORTE = [
    "efectivo", "cuentas_por_cobrar", "inventarios", "bienes_inmuebles",
    "mobiliario_equipos", "vehiculos", "depreciacion_acumulada",
    "tarjetas_credito", "proveedores", "impuestos_por_pagar", "gastos_acumulados",
    "creditos_hipotecarios", "creditos_consumo", "creditos_personales",
    "creditos_prendarios", "creditos_comerciales",
    "capital", "resultados_acumulados", "resultados_ejercicio",
    "retiros_acumulados",
    "total_activos", "total_pasivos", "total_patrimonio",
]


class MotorV2Service:
    def __init__(self, session: Session):
        self.session = session
        self.periodos = PeriodoRepository(session)
        self.clientes = ClienteRepository(session)
        self.audit = AuditService(session)

    # ------------------------------------------------------------- helpers
    def _validar_inputs(self, inputs_body: Mapping[str, Any]) -> dict[str, Any]:
        """Parsea los inputs con los dataclasses del motor (valida estructura
        y reglas duras de construccion) sin correr el calculo completo."""
        from motor.json_io import inputs_from_json, inputs_tipo_b_from_json

        if not isinstance(inputs_body, Mapping) or not inputs_body.get("periodo"):
            raise PeriodoValidationError("Faltan los inputs del motor (bloque 'periodo')")
        periodo_block = dict(inputs_body.get("periodo") or {})
        tipo = str(periodo_block.get("tipo") or "A").upper()
        try:
            if tipo == "B":
                inputs_tipo_b_from_json(inputs_body)
            else:
                inputs_from_json(inputs_body)
        except (KeyError, ValueError, TypeError) as exc:
            raise PeriodoValidationError(f"Inputs del motor invalidos: {exc}") from exc
        return {
            "tipo": tipo,
            "mes_inicial": str(periodo_block["mes_inicial"]),
            "mes_final": str(periodo_block["mes_final"]),
            "tasa_cambio": float(periodo_block["tasa_cambio"]),
            "seed": str(inputs_body.get("seed") or ""),
        }

    def _get_v2(self, periodo_id: str) -> PeriodoCertificacion:
        periodo = self.periodos.get(periodo_id)
        if not periodo:
            raise PeriodoNotFoundError("Periodo no encontrado")
        if getattr(periodo, "engine", "v1") != "v2":
            raise PeriodoConflictError(
                "Este periodo pertenece al editor clasico (engine v1); no se gestiona desde el Motor V2."
            )
        return periodo

    # -------------------------------------------------------------- create
    def crear_borrador(
        self,
        cliente_id: str,
        inputs_body: Mapping[str, Any],
        *,
        cpa_user: str = "system",
    ) -> dict:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            raise PeriodoNotFoundError("Cliente no encontrado o inactivo")
        meta = self._validar_inputs(inputs_body)
        try:
            periodo = self.periodos.create(
                cliente_id=cliente_id,
                engine="v2",
                periodo_meses=_meses_count(meta["mes_inicial"], meta["mes_final"]),
                mes_inicial=meta["mes_inicial"],
                mes_final=meta["mes_final"],
                estado="borrador",
                tasa_cambio=meta["tasa_cambio"],
                seed=meta["seed"] or None,
                saldos_iniciales_origen="manual",
                payload_json=json.dumps(dict(inputs_body), ensure_ascii=False, sort_keys=True, default=str),
                created_by=cpa_user or "system",
            )
            basic = periodo_to_basic_dict(periodo)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="create",
                summary=(
                    f"Creo certificacion Motor V2 tipo {meta['tipo']} "
                    f"{periodo.mes_inicial}..{periodo.mes_final} para {cliente.nombre_completo}"
                ),
                after=basic,
                metadata={"engine": "v2", "tipo": meta["tipo"], "cliente_id": cliente_id},
            )
            self.session.commit()
            return {"periodo": basic}
        except Exception:
            self.session.rollback()
            raise

    # -------------------------------------------------------------- update
    def actualizar(
        self,
        periodo_id: str,
        inputs_body: Mapping[str, Any],
        *,
        cpa_user: str = "system",
    ) -> dict:
        periodo = self._get_v2(periodo_id)
        if periodo.estado != "borrador":
            raise PeriodoConflictError(
                f"Solo se puede editar un borrador. Estado actual: '{periodo.estado}'."
            )
        meta = self._validar_inputs(inputs_body)
        before = periodo_to_basic_dict(periodo)
        try:
            periodo.mes_inicial = meta["mes_inicial"]
            periodo.mes_final = meta["mes_final"]
            periodo.periodo_meses = _meses_count(meta["mes_inicial"], meta["mes_final"])
            periodo.tasa_cambio = meta["tasa_cambio"]
            periodo.seed = meta["seed"] or None
            periodo.payload_json = json.dumps(dict(inputs_body), ensure_ascii=False, sort_keys=True, default=str)
            self.session.flush()
            after = periodo_to_basic_dict(periodo)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="update",
                summary=f"Actualizo certificacion Motor V2 {periodo.mes_inicial}..{periodo.mes_final}",
                before=before,
                after=after,
                metadata={"engine": "v2", "tipo": meta["tipo"]},
            )
            self.session.commit()
            return {"periodo": after}
        except Exception:
            self.session.rollback()
            raise

    # ---------------------------------------------------------------- read
    def obtener(self, periodo_id: str) -> dict:
        periodo = self._get_v2(periodo_id)
        return {
            "periodo": periodo_to_basic_dict(periodo),
            "inputs": parse_json_object(periodo.payload_json),
        }

    def listar(self, cliente_id: str) -> list[dict]:
        stmt = (
            select(PeriodoCertificacion)
            .where(
                PeriodoCertificacion.cliente_id == cliente_id,
                PeriodoCertificacion.engine == "v2",
            )
            .order_by(PeriodoCertificacion.mes_final.desc(), PeriodoCertificacion.updated_at.desc())
        )
        return [periodo_to_basic_dict(p) for p in self.session.scalars(stmt)]

    # ------------------------------------------------------------ finalize
    def finalizar(self, periodo_id: str, *, cpa_user: str = "system") -> dict:
        """Corre el motor con los inputs guardados. Si la validacion pasa,
        guarda el DOCX (con notas), cachea saldos finales y finaliza.
        Si NO pasa, devuelve ok=False con la validacion y no cambia nada."""
        from document_generator import generar_documento_completo
        from motor.json_io import modelo_from_json, validacion_to_json
        from motor.notas import construir_notas

        periodo = self._get_v2(periodo_id)
        if periodo.estado != "borrador":
            raise PeriodoConflictError(
                f"Solo se puede finalizar un borrador. Estado actual: '{periodo.estado}'."
            )
        cliente = self.clientes.get(periodo.cliente_id)
        if not cliente:
            raise PeriodoNotFoundError("Cliente del periodo no encontrado")

        inputs_body = parse_json_object(periodo.payload_json)
        modelo = modelo_from_json(inputs_body)
        validacion = validacion_to_json(modelo)
        if not modelo.ok:
            return {"ok": False, "validacion": validacion}

        # Nota 1 desglosada por cuentas bancarias: si la caja residual da
        # negativa es BLOQUEANTE (no se finaliza con una nota que no cuadra).
        from motor.notas import NotasError

        incluir_notas = bool(inputs_body.get("incluir_notas", True))
        try:
            notas_data = (
                construir_notas(modelo, cuentas_bancarias=inputs_body.get("cuentas_bancarias") or None)
                if incluir_notas else None
            )
        except NotasError as exc:
            return {
                "ok": False,
                "validacion": {
                    "ok": False,
                    "errores": [{"invariante": 0, "mensaje": str(exc)}],
                    "alertas": [],
                },
            }

        # Respaldo best-effort de la DB antes de mutar (finalizar es el paso
        # que convierte el borrador en el activo real e inmutable).
        from db.backup import backup_sqlite

        backup_sqlite(self.session.get_bind(), motivo="pre_finalizar")

        before = periodo_to_basic_dict(periodo)
        try:
            out_dir = _documentos_dir() / cliente.id
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{periodo.id}_{periodo.mes_inicial}_{periodo.mes_final}_v2.docx"
            # Vista del ESF elegida en los inputs guardados: corte (default) o mensual.
            esf_vista = (
                "mensual"
                if str(inputs_body.get("esf_vista") or "corte").lower() == "mensual"
                else "corte"
            )
            # Hojas opcionales del Word (checks de la UI). Los documentos del
            # cliente SIEMPRE van; lo opcional son notas y fotos del negocio.
            # (incluir_notas y notas_data ya se resolvieron arriba, con el
            # bloqueo de caja negativa ANTES de mutar nada.)
            incluir_fotos = bool(inputs_body.get("incluir_fotos_negocio", False))
            # Imagenes elegidas para esta certificacion (checks de la UI):
            # ids -> {tipo, path}. Las de tipo foto_negocio van a su propia
            # hoja; el resto al pareado de Documentos del cliente.
            from services.documento_service import DocumentoService

            soportes = DocumentoService(self.session).soportes_para(
                list(inputs_body.get("documentos_ids") or [])
            )
            docs_imagenes = [s for s in soportes if s["tipo"] != "foto_negocio"]
            fotos_negocio = [s for s in soportes if s["tipo"] == "foto_negocio"]
            generar_documento_completo(
                modelo.df_esf_mensual if esf_vista == "mensual" else modelo.df_esf_corte,
                modelo.df_er,
                modelo.df_datos,
                modelo.df_certificacion,
                str(out_path),
                incluir_validacion=False,
                esf_tipo=esf_vista,
                notas_data=notas_data,
                docs_imagenes=docs_imagenes or None,
                fotos_negocio=fotos_negocio or None,
                incluir_fotos_negocio=incluir_fotos,
            )

            corte = modelo.esf.corte()
            saldos_corte = {k: round(float(getattr(corte, k)), 2) for k in _CUENTAS_CORTE}
            periodo.estado = "finalizado"
            periodo.finalized_at = _utc_now()
            periodo.documento_path = str(out_path)
            periodo.documento_generado_at = _utc_now()
            periodo.saldos_finales_json = json.dumps(saldos_corte, ensure_ascii=False, sort_keys=True)
            periodo.validation_json = json.dumps(validacion, ensure_ascii=False, sort_keys=True)
            self.session.flush()

            after = periodo_to_basic_dict(periodo)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="finalize",
                summary=f"Finalizo certificacion Motor V2 {periodo.mes_inicial}..{periodo.mes_final}",
                before=before,
                after=after,
                metadata={
                    "engine": "v2",
                    "documento_path": str(out_path),
                    "alertas": len(validacion.get("alertas") or []),
                },
            )
            self.session.commit()
            return {
                "ok": True,
                "periodo": after,
                "validacion": validacion,
                "documento_path": str(out_path),
            }
        except Exception:
            self.session.rollback()
            raise

    def documento_path(self, periodo_id: str) -> str | None:
        from pathlib import Path

        periodo = self._get_v2(periodo_id)
        if not periodo.documento_path:
            return None
        p = Path(periodo.documento_path)
        return str(p) if p.exists() else None

    # --------------------------------------------------------- rollforward
    def saldos_rollforward(self, cliente_id: str) -> dict:
        """Propuesta de saldos iniciales para el proximo periodo V2 del cliente,
        desde su ultimo periodo v2 finalizado. RA nuevo = RA corte + RE corte."""
        stmt = (
            select(PeriodoCertificacion)
            .where(
                PeriodoCertificacion.cliente_id == cliente_id,
                PeriodoCertificacion.engine == "v2",
                PeriodoCertificacion.estado.in_(["finalizado", "certificado"]),
            )
            .order_by(PeriodoCertificacion.mes_final.desc())
            .limit(1)
        )
        anterior = self.session.scalar(stmt)
        if not anterior or not anterior.saldos_finales_json:
            return {"has_anterior": False, "saldos": None, "periodo_anterior": None}
        corte = parse_json_object(anterior.saldos_finales_json)
        saldos = {
            k: corte.get(k, 0.0)
            for k in _CUENTAS_CORTE
            if k not in {"capital", "resultados_acumulados", "resultados_ejercicio",
                         "retiros_acumulados",
                         "total_activos", "total_pasivos", "total_patrimonio"}
        }
        # RA del siguiente periodo = RA + utilidad del ejercicio. Los retiros
        # NO se restan aqui: el capital del corte ya viene NETO de retiros
        # (presentacion del CPA), asi el capital derivado del nuevo periodo
        # (A0 - P0 - RA0) coincide con el capital neto del cierre anterior.
        saldos["resultados_acumulados"] = round(
            float(corte.get("resultados_acumulados", 0.0))
            + float(corte.get("resultados_ejercicio", 0.0)),
            2,
        )
        return {
            "has_anterior": True,
            "saldos": saldos,
            "periodo_anterior": periodo_to_basic_dict(anterior),
        }

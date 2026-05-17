"""Servicio de roll-forward entre periodos.

Calcula saldos finales del ESF del ultimo mes de un periodo y los propone
como saldos iniciales del siguiente. Tambien detecta gaps temporales y
gestiona la invalidacion en cascada cuando se edita un periodo con
descendientes (otros periodos creados via rollforward).
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from accounting_model import BALANCE_ACCOUNTS
from db.models import PeriodoCertificacion
from repositories import PeriodoRepository


# Mapeo inverso: nombre de cuenta humano -> clave interna del payload balances
ACCOUNT_LABEL_TO_BALANCE_KEY: dict[str, str] = {
    nombre_humano: clave_interna
    for clave_interna, nombre_humano in BALANCE_ACCOUNTS.items()
}
# Algunos labels en ESF usan prefijo "(-)" para depreciacion
ACCOUNT_LABEL_TO_BALANCE_KEY["(-) Depreciacion Acumulada"] = "accum_depreciation"


class RollforwardService:
    def __init__(self, session: Session):
        self.session = session
        self.periodos = PeriodoRepository(session)

    # ---------------------------------------------------------------- saldos
    def compute_saldos_finales(self, periodo: PeriodoCertificacion) -> dict[str, float]:
        """Extrae saldos del ESF del mes_final del periodo.

        Devuelve un dict en formato 'balances' interno (cash, accounts_receivable,
        ...) listo para alimentar el siguiente periodo via payload['balances'].
        """
        payload = self._payload(periodo)
        preview = (payload.get("__last_result__") or {}).get("esf_mensual")
        # Si no hay cache, recalculamos desde build_financial_model
        if not preview:
            preview = self._recompute_esf_preview(payload)
        return self._extract_balances_from_esf_preview(preview, mes_final=periodo.mes_final)

    def propose_for_new_periodo(
        self,
        cliente_id: str,
        mes_inicial_nuevo: str,
    ) -> dict[str, Any]:
        """Construye una propuesta de saldos iniciales para un periodo nuevo.

        Retorna:
            {
                "has_anterior": bool,
                "periodo_anterior_id": str|None,
                "mes_anterior_final": str|None,
                "is_contiguous": bool,
                "saldos": {clave_interna: float},  # vacio si no hay anterior
                "warning": str|None,
            }
        """
        anterior = self.periodos.latest_finalized_for_cliente(cliente_id)
        if not anterior:
            return {
                "has_anterior": False,
                "periodo_anterior_id": None,
                "mes_anterior_final": None,
                "is_contiguous": False,
                "saldos": {},
                "warning": None,
            }

        contiguous = self._is_contiguous(anterior.mes_final, mes_inicial_nuevo)
        saldos = self.compute_saldos_finales(anterior)

        warning = None
        if not contiguous:
            warning = (
                f"El periodo anterior cerro en {anterior.mes_final} y el nuevo "
                f"inicia en {mes_inicial_nuevo}. Existe un salto temporal. "
                "Revise los saldos iniciales antes de finalizar."
            )

        return {
            "has_anterior": True,
            "periodo_anterior_id": anterior.id,
            "mes_anterior_final": anterior.mes_final,
            "is_contiguous": contiguous,
            "saldos": saldos,
            "warning": warning,
        }

    def cache_saldos_finales(self, periodo: PeriodoCertificacion) -> dict[str, float]:
        """Calcula y persiste en saldos_finales_json del periodo."""
        saldos = self.compute_saldos_finales(periodo)
        periodo.saldos_finales_json = json.dumps(saldos, ensure_ascii=False, sort_keys=True)
        self.session.flush()
        return saldos

    # ---------------------------------------------------------- invalidation
    def invalidate_descendants(self, periodo_id: str) -> list[str]:
        """Marca descendientes (rollforward hijos) como recompute_required.

        Devuelve la lista de IDs afectados.
        """
        hijos = self.periodos.list_descendants(periodo_id)
        ids = [h.id for h in hijos]
        if ids:
            self.periodos.mark_recompute_required(ids)
        return ids

    # ----------------------------------------------------------- helpers
    @staticmethod
    def _payload(periodo: PeriodoCertificacion) -> dict[str, Any]:
        try:
            return json.loads(periodo.payload_json or "{}")
        except Exception:
            return {}

    @staticmethod
    def _is_contiguous(mes_anterior_final: str, mes_nuevo_inicial: str) -> bool:
        """True si mes_nuevo_inicial es el mes siguiente a mes_anterior_final."""
        try:
            prev = pd.to_datetime(f"{mes_anterior_final}-01")
            new = pd.to_datetime(f"{mes_nuevo_inicial}-01")
            esperado = (prev + pd.offsets.MonthBegin(1))
            return new.year == esperado.year and new.month == esperado.month
        except Exception:
            return False

    @staticmethod
    def _recompute_esf_preview(payload: dict[str, Any]) -> dict[str, Any]:
        """Reconstruye el preview del ESF mensual desde el payload, sin cache."""
        from financial_model import build_financial_model, result_to_json

        result = build_financial_model(payload)
        rendered = result_to_json(result)
        return (rendered.get("preview") or {}).get("esf_mensual") or {}

    @staticmethod
    def _extract_balances_from_esf_preview(
        esf_preview: dict[str, Any],
        *,
        mes_final: str,
    ) -> dict[str, float]:
        """Lee filas del preview ESF y devuelve {clave_interna: saldo} para el mes_final.

        esf_preview tiene shape {'columns': [...], 'rows': [...]} segun _df_preview.
        """
        if not isinstance(esf_preview, dict):
            return {}
        columns = esf_preview.get("columns") or []
        rows = esf_preview.get("rows") or []
        if not columns or not rows:
            return {}

        # Localizar la columna del mes_final (puede venir como '2026-04-30' o similar)
        mes_target = (mes_final or "")[:7]
        target_col = None
        for col in columns:
            col_str = str(col)
            if col_str[:7] == mes_target:
                target_col = col
                break
        # Si no hay match exacto, tomar la ultima columna no-descripcion
        if target_col is None:
            value_columns = [c for c in columns if str(c).lower() != "descripcion"]
            if value_columns:
                target_col = value_columns[-1]
        if target_col is None:
            return {}

        saldos: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = str(row.get("Descripcion") or row.get("descripcion") or "").strip()
            if not label:
                continue
            key = ACCOUNT_LABEL_TO_BALANCE_KEY.get(label)
            if not key:
                continue
            raw = row.get(target_col)
            if raw is None or raw == "":
                continue
            try:
                saldos[key] = float(raw)
            except (TypeError, ValueError):
                continue
        return saldos

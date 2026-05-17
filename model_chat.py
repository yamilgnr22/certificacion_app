from __future__ import annotations

import json
import os
import random
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional
from uuid import uuid4

import pandas as pd

from financial_model import build_financial_model, result_to_json


CHAT_SOURCE = "chat_financiero"

ALLOWED_LEVERS = {
    "purchase_adjustment",
    "supplier_financing",
    "loan_commercial_new",
    "capital_contribution",
    "owner_withdrawal",
    "retained_earnings_distribution",
    "capital_reclassification",
    "asset_real_estate",
    "asset_equipment",
    "asset_vehicle",
    "loan_pledge_new",
    "loan_personal_new",
    "loan_mortgage_new",
    "supplier_new",
}

LEDGER_ACCOUNT_ALIASES = {
    "efectivo": "cash",
    "caja": "cash",
    "banco": "cash",
    "bancos": "cash",
    "efectivo y equivalentes de efectivo": "cash",
    "cuentas por cobrar": "accounts_receivable",
    "cuentas por cobrar clientes": "accounts_receivable",
    "inventario": "inventory",
    "inventarios": "inventory",
    "bienes inmuebles": "ppe_real_estate",
    "vivienda": "ppe_real_estate",
    "mobiliario y equipos": "ppe_equipment",
    "equipos": "ppe_equipment",
    "vehiculos": "ppe_vehicles",
    "depreciacion acumulada": "accum_depreciation",
    "tarjetas": "credit_cards",
    "tarjetas de credito": "credit_cards",
    "proveedores": "suppliers",
    "impuestos por pagar": "taxes_payable",
    "gastos acumulados": "accrued_expenses",
    "gastos acumulados por pagar": "accrued_expenses",
    "creditos hipotecarios": "loans_mortgage",
    "creditos consumo": "loans_consumo",
    "creditos personales": "loans_personal",
    "creditos prendarios": "loans_pledge",
    "creditos comerciales": "loans_commercial",
    "capital": "capital",
    "resultados acumulados": "retained_earnings",
    "resultado acumulado": "retained_earnings",
    "resultados del ejercicio": "current_earnings",
    "resultado del ejercicio": "current_earnings",
    "utilidad del ejercicio": "current_earnings",
}

LEDGER_ACCOUNT_LABELS = {
    "cash": "Efectivo y Equivalentes de Efectivo",
    "accounts_receivable": "Cuentas por Cobrar Clientes",
    "inventory": "Inventarios",
    "ppe_real_estate": "Bienes Inmuebles",
    "ppe_equipment": "Mobiliario y Equipos",
    "ppe_vehicles": "Vehiculos",
    "accum_depreciation": "Depreciacion Acumulada",
    "credit_cards": "Tarjetas de Credito",
    "suppliers": "Proveedores",
    "taxes_payable": "Impuestos por Pagar",
    "accrued_expenses": "Gastos Acumulados por pagar",
    "loans_mortgage": "Creditos Hipotecarios",
    "loans_consumo": "Creditos Consumo",
    "loans_personal": "Creditos Personales",
    "loans_pledge": "Creditos Prendarios",
    "loans_commercial": "Creditos Comerciales",
    "capital": "Capital",
    "retained_earnings": "Resultados Acumulados",
    "current_earnings": "Resultados del Ejercicio",
}


class ModelChatError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _model_months(result: Any) -> list[str]:
    full_summary = (getattr(result, "metadata", {}) or {}).get("full_summary") or {}
    return list(full_summary.get("months") or result.summary.get("all_months") or result.summary.get("months") or [])


def _scoped_months(result: Any, scope: Optional[Mapping[str, Any]] = None, message: str = "") -> list[str]:
    all_months = _model_months(result)
    if not all_months:
        return []
    text = _normalize_text(message)
    if any(token in text for token in ["global", "todo el modelo", "modelo completo", "rango completo", "todos los anos", "todos los años"]):
        return all_months

    scope = dict(scope or {})
    mode = str(scope.get("mode") or scope.get("scope") or "").strip().lower()
    scope_months = [str(month)[:7] for month in (scope.get("months") or []) if str(month).strip()]
    scope_months = [month for month in scope_months if month in all_months]
    if mode in {"global", "full", "all", "modelo"}:
        return all_months
    if mode in {"block", "selected_block", "bloque"} and scope_months:
        return scope_months
    if mode in {"year", "ano", "anio"}:
        year = str(scope.get("year") or "").strip()
        year_months = [month for month in all_months if month.startswith(f"{year}-")]
        if year_months:
            return year_months

    year_match = re.search(r"\b(20\d{2})\b", text)
    if year_match:
        year = year_match.group(1)
        year_months = [month for month in all_months if month.startswith(f"{year}-")]
        if year_months:
            return year_months

    return scope_months or all_months


def _esf_df(result: Any) -> pd.DataFrame:
    return getattr(result, "df_esf_mensual_full", None) if getattr(result, "df_esf_mensual_full", None) is not None else result.df_esf_mensual


def _account_mov_df(result: Any) -> pd.DataFrame:
    return getattr(result, "df_movimiento_cuentas_full", None) if getattr(result, "df_movimiento_cuentas_full", None) is not None else result.df_movimiento_cuentas


def _cash_flow_df(result: Any) -> pd.DataFrame:
    return getattr(result, "df_flujo_caja_full", None) if getattr(result, "df_flujo_caja_full", None) is not None else result.df_flujo_caja


def preview_chat_adjustment(
    payload: Mapping[str, Any],
    message: str,
    *,
    scope: Optional[Mapping[str, Any]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Interpret a chat instruction and return a calculated adjustment proposal."""
    message = str(message or "").strip()
    if not message:
        return _clarification("Escriba una instruccion, por ejemplo: ajusta compras para caja final 1,000,000.")

    text = _normalize_text(message)
    if _is_undo_request(text):
        action = {
            "intent": "undo_last_adjustment",
            "target_month": None,
            "target_cash": None,
            "lever": None,
            "clarification": None,
        }
    else:
        action = interpret_cash_instruction(payload, message, scope=scope, api_key=api_key, model=model)
    action["instruction_id"] = action.get("instruction_id") or _new_instruction_id()
    action["message"] = message
    action["created_at"] = _utc_now()
    if action.get("intent") == "clarification_needed":
        return _clarification(action.get("clarification") or "Necesito una instruccion mas especifica.")

    solved = solve_cash_target(payload, action, scope=scope, message=message)
    if not solved.get("ok"):
        return solved

    adjusted_result = solved.pop("_adjusted_result")
    adjusted_json = result_to_json(adjusted_result)
    solved.update(
        {
            "interpreted_action": solved.get("interpreted_action") or action,
            "summary": adjusted_json.get("summary"),
            "resulting_summary": adjusted_json.get("summary"),
            "validations": adjusted_json.get("validations"),
            "preview": adjusted_json.get("preview"),
            "metadata": adjusted_json.get("metadata"),
        }
    )
    return solved


def interpret_cash_instruction(
    payload: Mapping[str, Any],
    message: str,
    *,
    scope: Optional[Mapping[str, Any]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Use the LLM only to translate natural language into a structured action."""
    current_result = build_financial_model(payload)
    months = _scoped_months(current_result, scope, message)
    default_month = months[-1] if months else None
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        return heuristic_interpret_cash_instruction(message, months)

    from openai import OpenAI  # type: ignore

    client = OpenAI(api_key=key)
    llm_model = model or os.getenv("OPENAI_MODEL_CHAT", "gpt-4o-mini")
    schema = {
        "name": "financial_chat_action",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        "target_cash_balance",
                        "target_cash_series",
                        "equity_cash_adjustment",
                        "assumption_change",
                        "journal_entry",
                        "compound_events",
                        "account_transfer",
                        "year_close_transfer",
                        "undo_last_adjustment",
                        "replace_adjustment",
                        "clarification_needed",
                    ],
                },
                "target_month": {"type": ["string", "null"]},
                "target_cash": {"type": ["number", "null"]},
                "amount": {"type": ["number", "null"]},
                "assumption": {
                    "type": ["string", "null"],
                    "enum": ["cost_pct", None],
                },
                "value": {"type": ["number", "null"]},
                "source_month": {"type": ["string", "null"]},
                "debit_account": {"type": ["string", "null"]},
                "credit_account": {"type": ["string", "null"]},
                "source_account": {"type": ["string", "null"]},
                "destination_account": {"type": ["string", "null"]},
                "cash_variability_pct": {"type": ["number", "null"]},
                "replace_existing": {"type": ["boolean", "null"]},
                "lever": {
                    "type": ["string", "null"],
                    "enum": [
                        "purchase_adjustment",
                        "supplier_financing",
                        "loan_commercial_new",
                        "capital_contribution",
                        "owner_withdrawal",
                        "retained_earnings_distribution",
                        "capital_reclassification",
                        None,
                    ],
                },
                "clarification": {"type": ["string", "null"]},
            },
            "required": [
                "intent",
                "target_month",
                "target_cash",
                "amount",
                "assumption",
                "value",
                "source_month",
                "debit_account",
                "credit_account",
                "source_account",
                "destination_account",
                "cash_variability_pct",
                "replace_existing",
                "lever",
                "clarification",
            ],
        },
    }
    system = (
        "Eres un interprete de instrucciones para un modelo financiero. "
        "Tu unica tarea es convertir el mensaje en JSON estructurado. "
        "No calcules ajustes, no inventes saldos y no modifiques numeros. "
        "Si falta el monto objetivo de caja o la palanca de ajuste, pide aclaracion."
    )
    user = {
        "message": message,
        "task": "Extrae una accion para dejar la caja en un monto objetivo.",
        "defaults": {
            "target_month": default_month,
            "currency": "nio",
        },
        "allowed_months": months,
        "allowed_levers": sorted(ALLOWED_LEVERS),
        "rules": [
            "Si se menciona compras, usa purchase_adjustment.",
            "Si se menciona proveedores o credito de proveedores, usa supplier_financing.",
            "Si se menciona prestamo o credito bancario, usa loan_commercial_new.",
            "Si se menciona aporte o capital, usa capital_contribution.",
            "Si el usuario dice saca/retira de banco/caja y restalo en capital, usa intent equity_cash_adjustment y lever owner_withdrawal.",
            "Si el usuario dice saca/retira de banco/caja y restalo en resultados acumulados, usa intent equity_cash_adjustment y lever retained_earnings_distribution.",
            "Si el usuario pide reclasificar entre capital y resultados acumulados sin tocar caja, usa intent equity_cash_adjustment y lever capital_reclassification.",
            "Si el usuario pide deshacer, usa undo_last_adjustment.",
            "Si el usuario pide cambiar el costo de venta, costo base o porcentaje de costo, usa intent assumption_change, assumption cost_pct y value con el porcentaje indicado.",
            "Para assumption_change, interpreta +/-5%, variabilidad 5% o rango 5% como cash_variability_pct=5 si aparece.",
            "Si el usuario pide trasladar resultados del ejercicio a resultados acumulados por cierre de ano, usa year_close_transfer.",
            "Si el usuario pide trasladar saldo de una cuenta patrimonial a otra, usa account_transfer.",
            "Si el usuario dice debita/debe y acredita/haber cuentas especificas, usa journal_entry.",
            "Si el usuario pide reemplazar, recalcular, eliminar o quitar un ajuste anterior, usa replace_existing=true junto al ajuste solicitado.",
            "Interpreta 1 mm, 1m, 1 millon o 1,000,000 como 1000000.",
            "Interpreta 800k como 800000.",
            "Si el usuario pide caja promedio, todos los meses, saldos de caja u oscilar alrededor de un monto, usa target_cash_series.",
            "Si el usuario menciona un rango +/-20%, variabilidad 20% o similar, devuelve cash_variability_pct=20.",
            "Para target_cash_series, si no hay rango explicito, usa cash_variability_pct=20.",
            "Si no se menciona mes, usa el mes por defecto.",
            "Si no se menciona moneda, asume cordobas.",
        ],
    }
    response_format: Dict[str, Any] = {"type": "json_schema", "json_schema": schema}
    resp = client.chat.completions.create(
        model=llm_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        response_format=response_format,
        temperature=0,
    )
    try:
        content = resp.choices[0].message.content or "{}"
        raw_action = json.loads(content)
    except Exception as exc:
        raise ModelChatError(f"Respuesta LLM no parseable: {exc}", status_code=502) from exc

    text = _normalize_text(message)
    ledger_action = _extract_ledger_action(text, months)
    if ledger_action:
        raw_action.update(ledger_action)
    assumption_action = _extract_assumption_action(text, months)
    if assumption_action:
        raw_action.update(assumption_action)
    equity_action = _extract_equity_action(text, months)
    if equity_action:
        raw_action.update(equity_action)
    if _is_undo_request(text):
        raw_action["intent"] = "undo_last_adjustment"
    if _is_series_request(text):
        raw_action["intent"] = "target_cash_series"
        raw_action["target_month"] = None
    if _is_replace_request(text):
        raw_action["replace_existing"] = True
    if raw_action.get("target_cash") in {None, ""}:
        amount = _extract_cash_amount(text)
        if amount is not None:
            raw_action["target_cash"] = amount
    if raw_action.get("amount") in {None, ""} and raw_action.get("intent") == "equity_cash_adjustment":
        amount = _extract_cash_amount(text)
        if amount is not None:
            raw_action["amount"] = amount
    if raw_action.get("cash_variability_pct") in {None, ""}:
        variability = _extract_variability_pct(text)
        if variability is not None:
            raw_action["cash_variability_pct"] = variability
    if not raw_action.get("lever"):
        lever = _extract_lever(text)
        if lever:
            raw_action["lever"] = lever

    return normalize_action(raw_action, months)


def solve_cash_target(
    payload: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    scope: Optional[Mapping[str, Any]] = None,
    message: str = "",
) -> Dict[str, Any]:
    """Calculate the deterministic event needed to hit the target cash balance."""
    current_result = build_financial_model(payload)
    months = _scoped_months(current_result, scope, message or str(action.get("message") or ""))
    normalized = normalize_action(action, months)
    if normalized.get("intent") == "clarification_needed":
        return _clarification(normalized.get("clarification") or "Instruccion ambigua.")
    if normalized.get("intent") == "undo_last_adjustment":
        return _solve_undo_last_adjustment(payload, normalized, current_result, months)
    if normalized.get("intent") == "assumption_change":
        all_months = _model_months(current_result)
        scope_mode = "global" if months == all_months else "block"
        return _solve_assumption_change(payload, normalized, current_result, months, scope_mode=scope_mode)
    if normalized.get("intent") == "compound_events":
        return _solve_compound_events_adjustment(payload, normalized, current_result, months)
    if normalized.get("intent") in {"journal_entry", "account_transfer", "year_close_transfer"}:
        return _solve_ledger_adjustment(payload, normalized, current_result, months)

    working_payload, replaced_events = _payload_for_action(payload, normalized)
    working_result = build_financial_model(working_payload) if replaced_events else current_result

    if normalized.get("intent") == "target_cash_series":
        return _solve_cash_series_target(
            working_payload,
            normalized,
            working_result,
            months,
            comparison_result=current_result,
            replaced_events=replaced_events,
        )
    if normalized.get("intent") == "equity_cash_adjustment":
        return _solve_equity_cash_adjustment(
            working_payload,
            normalized,
            working_result,
            months,
            comparison_result=current_result,
            replaced_events=replaced_events,
        )

    target_month = str(normalized["target_month"])
    target_cash = _round_money(normalized["target_cash"])
    lever = str(normalized["lever"])
    current_cash = _statement_value(
        _esf_df(working_result),
        "Descripcion",
        "Efectivo y Equivalentes de Efectivo",
        target_month,
    )
    difference = _round_money(target_cash - current_cash)

    if abs(difference) <= 1:
        adjusted_payload = working_payload if replaced_events else deepcopy(dict(payload or {}))
        adjusted_result = working_result if replaced_events else current_result
        return {
            "ok": True,
            "action": normalized,
            "adjusted_payload": adjusted_payload,
            "proposal": {
                "target_month": target_month,
                "target_cash": target_cash,
                "current_cash": current_cash,
                "adjusted_cash": current_cash,
                "difference": difference,
                "lever": lever,
                "event": None,
                "explanation": "La caja ya esta dentro de la tolerancia del objetivo.",
                "impact": _impact(current_result, adjusted_result, target_month),
            },
            "interpreted_action": normalized,
            "existing_events_preserved": _events_from_payload(adjusted_payload),
            "new_events": [],
            "removed_events": [],
            "replaced_events": replaced_events,
            "incremental_impact": _impact(current_result, adjusted_result, target_month),
            "resulting_summary": adjusted_result.summary,
            "_adjusted_result": adjusted_result,
        }

    event_account = lever
    event_amount = 0.0
    explanation = ""

    if lever == "purchase_adjustment":
        current_purchases = _account_movement_value(working_result, "Inventarios", "Aumentos", target_month)
        event_amount = _round_money(-difference)
        if current_purchases + event_amount < -1:
            max_cash = _round_money(current_cash + current_purchases)
            return _not_viable(
                "El ajuste de compras dejaria compras negativas. "
                f"Con compras puede subir caja como maximo hasta {max_cash:,.0f} en {target_month}."
            )
        if event_amount < 0:
            explanation = "Reduce compras del mes; mejora caja y baja inventario."
        else:
            explanation = "Aumenta compras del mes; baja caja y sube inventario."
    elif lever == "supplier_financing":
        if difference <= 0:
            return _not_viable("El financiamiento de proveedores solo puede aumentar caja, no reducirla.")
        paid_purchases = abs(_cash_flow_value(working_result, "Compras de inventario pagadas", target_month))
        if difference - paid_purchases > 1:
            max_cash = _round_money(current_cash + paid_purchases)
            return _not_viable(
                "No hay suficientes compras pagadas para financiarlas con proveedores. "
                f"Con esta palanca puede subir caja como maximo hasta {max_cash:,.0f} en {target_month}."
            )
        event_amount = difference
        explanation = "Convierte parte de las compras del mes en credito de proveedores; mejora caja, sube proveedores y conserva inventario."
    elif lever == "loan_commercial_new":
        if difference <= 0:
            return _not_viable("Un prestamo nuevo solo puede aumentar caja, no reducirla.")
        event_amount = difference
        explanation = "Registra un nuevo credito comercial; aumenta caja y pasivos no corrientes."
    elif lever == "capital_contribution":
        if difference <= 0:
            return _not_viable("Un aporte de capital solo puede aumentar caja, no reducirla.")
        event_amount = difference
        explanation = "Registra un aporte de capital; aumenta caja y patrimonio."
    else:
        return _clarification("Indique si el ajuste sera por compras, proveedores, prestamo o aporte de capital.")

    event_amount = _round_money(event_amount)
    event = _event_with_metadata({
        "month": target_month,
        "account": event_account,
        "amount": event_amount,
        "currency": "nio",
    }, normalized)
    adjusted_payload = _append_event(working_payload, event)
    adjusted_result = build_financial_model(adjusted_payload)
    adjusted_cash = _statement_value(
        _esf_df(adjusted_result),
        "Descripcion",
        "Efectivo y Equivalentes de Efectivo",
        target_month,
    )
    if abs(adjusted_cash - target_cash) > 1:
        return _not_viable(
            "El ajuste propuesto no alcanza el objetivo dentro de la tolerancia. "
            f"Caja objetivo: {target_cash:,.0f}; caja recalculada: {adjusted_cash:,.0f}."
        )

    return {
        "ok": True,
        "action": normalized,
        "adjusted_payload": adjusted_payload,
        "proposal": {
            "target_month": target_month,
            "target_cash": target_cash,
            "current_cash": current_cash,
            "adjusted_cash": adjusted_cash,
            "difference": difference,
            "lever": lever,
            "event": event,
            "explanation": explanation,
            "impact": _impact(current_result, adjusted_result, target_month),
        },
        "interpreted_action": normalized,
        "existing_events_preserved": _events_from_payload(working_payload),
        "new_events": [event],
        "removed_events": [],
        "replaced_events": replaced_events,
        "incremental_impact": _impact(current_result, adjusted_result, target_month),
        "resulting_summary": adjusted_result.summary,
        "_adjusted_result": adjusted_result,
    }


def normalize_action(action: Mapping[str, Any], months: list[str]) -> Dict[str, Any]:
    if not isinstance(action, Mapping):
        return _clarification_action("No pude interpretar la instruccion.")

    intent = str(action.get("intent") or "").strip()
    if intent == "undo_last_adjustment":
        return {
            "intent": intent,
            "target_month": None,
            "target_cash": None,
            "amount": None,
            "assumption": None,
            "value": None,
            "source_month": None,
            "debit_account": None,
            "credit_account": None,
            "source_account": None,
            "destination_account": None,
            "cash_variability_pct": None,
            "replace_existing": False,
            "lever": None,
            "instruction_id": str(action.get("instruction_id") or _new_instruction_id()),
            "message": str(action.get("message") or ""),
            "created_at": str(action.get("created_at") or _utc_now()),
            "clarification": None,
        }
    if intent == "replace_adjustment":
        intent = "target_cash_balance"
        action = {**dict(action), "replace_existing": True}
    if intent not in {
        "target_cash_balance",
        "target_cash_series",
        "equity_cash_adjustment",
        "assumption_change",
        "journal_entry",
        "compound_events",
        "account_transfer",
        "year_close_transfer",
    }:
        return _clarification_action(
            action.get("clarification")
            or "No identifique una instruccion contable aplicable. Si ya hay una propuesta, use Aplicar ajuste; si quiere un movimiento nuevo, indique cuenta, monto y mes."
        )

    if intent == "compound_events":
        events = []
        for raw_event in action.get("events") or []:
            if not isinstance(raw_event, Mapping):
                continue
            event_month = str(raw_event.get("month") or raw_event.get("mes") or target_month or "").strip()[:7]
            account = str(raw_event.get("account") or raw_event.get("cuenta") or "").strip()
            event_amount = _round_money(raw_event.get("amount") or raw_event.get("monto") or 0)
            if not event_month or (months and event_month not in months):
                return _clarification_action(f"El mes {event_month or '(vacio)'} no esta dentro del periodo modelado.")
            if account not in ALLOWED_LEVERS:
                return _clarification_action("La instruccion incluye una cuenta fuera del catalogo permitido.")
            if event_amount <= 0:
                return _clarification_action("Todos los eventos compuestos deben tener monto positivo.")
            events.append({
                "month": event_month,
                "account": account,
                "amount": event_amount,
                "currency": str(raw_event.get("currency") or raw_event.get("moneda") or "nio"),
            })
        if not events:
            return _clarification_action("Indique los movimientos contables a registrar.")
        return {
            "intent": "compound_events",
            "target_month": events[0]["month"],
            "target_cash": None,
            "amount": None,
            "assumption": None,
            "value": None,
            "source_month": None,
            "debit_account": None,
            "credit_account": None,
            "source_account": None,
            "destination_account": None,
            "cash_variability_pct": None,
            "replace_existing": False,
            "events": events,
            "lever": None,
            "instruction_id": str(action.get("instruction_id") or _new_instruction_id()),
            "message": str(action.get("message") or ""),
            "created_at": str(action.get("created_at") or _utc_now()),
            "clarification": None,
        }

    if intent in {"journal_entry", "account_transfer", "year_close_transfer"}:
        normalized_ledger = _normalize_ledger_action(action, months)
        if normalized_ledger.get("intent") == "clarification_needed":
            return normalized_ledger
        return normalized_ledger

    if intent == "assumption_change":
        assumption = str(action.get("assumption") or "").strip()
        if assumption != "cost_pct":
            return _clarification_action("Por ahora solo puedo cambiar el supuesto de costo de venta.")
        value = _to_float_or_none(action.get("value"))
        if value is None:
            value = _to_float_or_none(action.get("target_cash"))
        if value is None or value < 0 or value > 100:
            return _clarification_action("Indique el porcentaje de costo de venta entre 0% y 100%.")
        variability_pct = _to_float_or_none(action.get("cash_variability_pct"))
        if variability_pct is None:
            variability_pct = 5.0
        variability_pct = max(0.0, min(50.0, variability_pct))
        return {
            "intent": intent,
            "target_month": None,
            "target_cash": None,
            "amount": None,
            "assumption": assumption,
            "value": float(value),
            "source_month": None,
            "debit_account": None,
            "credit_account": None,
            "source_account": None,
            "destination_account": None,
            "cash_variability_pct": variability_pct,
            "replace_existing": bool(action.get("replace_existing")),
            "lever": None,
            "instruction_id": str(action.get("instruction_id") or _new_instruction_id()),
            "message": str(action.get("message") or ""),
            "created_at": str(action.get("created_at") or _utc_now()),
            "clarification": None,
        }

    target_month = str(action.get("target_month") or "").strip() or (months[-1] if months else "")
    target_month = target_month[:7]
    if intent == "target_cash_series":
        target_month = None
    elif months and target_month not in months:
        return _clarification_action(f"El mes {target_month or '(vacio)'} no esta dentro del periodo modelado.")

    lever = _normalize_lever(action.get("lever"))
    if not lever:
        return _clarification_action("Indique la palanca contable permitida.")

    amount = _to_float_or_none(action.get("amount"))
    target_cash = _to_float_or_none(action.get("target_cash"))
    if intent == "equity_cash_adjustment":
        if lever not in {"owner_withdrawal", "retained_earnings_distribution", "capital_reclassification"}:
            return _clarification_action("Para ajustes patrimoniales use retiro de capital, resultados acumulados o reclasificacion.")
        if amount is None:
            amount = target_cash
        if amount is None or amount == 0:
            return _clarification_action("Indique el monto del ajuste patrimonial.")
    else:
        if target_cash is None:
            return _clarification_action("Indique el monto objetivo de caja.")
        if target_cash < 0:
            return _clarification_action("La caja objetivo no puede ser negativa.")
    variability_pct = _to_float_or_none(action.get("cash_variability_pct"))
    if intent == "target_cash_series":
        if variability_pct is None:
            variability_pct = 20.0
        variability_pct = max(0.0, min(50.0, variability_pct))

    return {
        "intent": intent,
        "target_month": target_month,
        "target_cash": _round_money(target_cash) if target_cash is not None else None,
        "amount": _round_money(amount) if amount is not None else None,
        "assumption": None,
        "value": None,
        "source_month": None,
        "debit_account": None,
        "credit_account": None,
        "source_account": None,
        "destination_account": None,
        "cash_variability_pct": variability_pct,
        "replace_existing": bool(action.get("replace_existing")),
        "lever": lever,
        "instruction_id": str(action.get("instruction_id") or _new_instruction_id()),
        "message": str(action.get("message") or ""),
        "created_at": str(action.get("created_at") or _utc_now()),
        "clarification": None,
    }


def _normalize_ledger_action(action: Mapping[str, Any], months: list[str]) -> Dict[str, Any]:
    intent = str(action.get("intent") or "").strip()
    target_month = str(action.get("target_month") or "").strip()[:7] or (months[-1] if months else "")
    if target_month and months and target_month not in months:
        return _clarification_action(f"El mes {target_month} no esta dentro del periodo modelado.")

    source_month = str(action.get("source_month") or "").strip()[:7] or None
    amount = _to_float_or_none(action.get("amount"))
    if amount is not None:
        amount = _round_money(amount)
    if amount is not None and amount <= 0:
        return _clarification_action("El monto de la partida debe ser positivo.")

    debit_account = _normalize_ledger_account(action.get("debit_account"))
    credit_account = _normalize_ledger_account(action.get("credit_account"))
    source_account = _normalize_ledger_account(action.get("source_account"))
    destination_account = _normalize_ledger_account(action.get("destination_account"))

    if intent == "year_close_transfer":
        debit_account = debit_account or "current_earnings"
        credit_account = credit_account or "retained_earnings"
        source_account = source_account or "current_earnings"
        destination_account = destination_account or "retained_earnings"
    elif intent == "account_transfer":
        if not source_account or not destination_account:
            return _clarification_action("Indique la cuenta origen y la cuenta destino.")
        if source_account == destination_account:
            return _clarification_action("La cuenta origen y destino deben ser diferentes.")
        debit_account, credit_account = _journal_sides_for_transfer(source_account, destination_account)
        if not debit_account or not credit_account:
            return _clarification_action("Solo se permiten traslados controlados entre cuentas patrimoniales o contra caja.")
    elif intent == "journal_entry":
        if not debit_account or not credit_account:
            return _clarification_action("Indique cuenta al debe y cuenta al haber.")
        if debit_account == credit_account:
            return _clarification_action("La cuenta al debe y al haber deben ser diferentes.")

    if not debit_account or not credit_account:
        return _clarification_action("No pude determinar la partida doble.")
    if intent != "year_close_transfer" and amount is None:
        return _clarification_action("Indique el monto de la partida doble.")

    return {
        "intent": intent,
        "target_month": target_month,
        "target_cash": None,
        "amount": amount,
        "assumption": None,
        "value": None,
        "source_month": source_month,
        "debit_account": debit_account,
        "credit_account": credit_account,
        "source_account": source_account,
        "destination_account": destination_account,
        "cash_variability_pct": None,
        "replace_existing": bool(action.get("replace_existing")),
        "lever": None,
        "instruction_id": str(action.get("instruction_id") or _new_instruction_id()),
        "message": str(action.get("message") or ""),
        "created_at": str(action.get("created_at") or _utc_now()),
        "clarification": None,
    }


def _solve_ledger_adjustment(
    payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
    current_result: Any,
    months: list[str],
) -> Dict[str, Any]:
    target_month = str(normalized.get("target_month") or (months[-1] if months else ""))
    if not target_month:
        return _clarification("No hay mes disponible para registrar la partida.")

    debit_account = str(normalized.get("debit_account") or "")
    credit_account = str(normalized.get("credit_account") or "")
    amount = _round_money(normalized.get("amount"))
    source_month = str(normalized.get("source_month") or "")
    if normalized.get("intent") == "year_close_transfer" and not amount:
        source_month = source_month or _previous_year_end(target_month)
        amount = _statement_value(_esf_df(current_result), "Descripcion", LEDGER_ACCOUNT_LABELS["current_earnings"], source_month)
        if amount <= 0:
            return _not_viable(f"No hay resultado del ejercicio positivo que trasladar en {source_month}.")

    validation = _validate_journal_entry(current_result, target_month, debit_account, credit_account, amount, source_month)
    if validation:
        return validation

    journal_entry = _journal_entry_with_metadata(
        {
            "month": target_month,
            "debit_account": debit_account,
            "credit_account": credit_account,
            "amount": amount,
            "currency": "nio",
            "entry_type": normalized.get("intent"),
            "source_month": source_month or None,
        },
        normalized,
    )
    adjusted_payload = _append_journal_entry(payload, journal_entry)
    adjusted_result = build_financial_model(adjusted_payload)
    impact = _impact(current_result, adjusted_result, target_month)
    proposal = {
        "kind": "journal_entry",
        "entry_type": normalized.get("intent"),
        "target_month": target_month,
        "source_month": source_month or None,
        "amount": amount,
        "debit_account": debit_account,
        "credit_account": credit_account,
        "debit_label": _ledger_label(debit_account),
        "credit_label": _ledger_label(credit_account),
        "scope_label": "bloque seleccionado",
        "journal_entry": journal_entry,
        "journal_rows": [
            {"account": _ledger_label(debit_account), "debit": amount, "credit": 0},
            {"account": _ledger_label(credit_account), "debit": 0, "credit": amount},
        ],
        "explanation": _ledger_explanation(normalized, amount, source_month or None),
        "impact": impact,
    }
    return {
        "ok": True,
        "action": dict(normalized),
        "adjusted_payload": adjusted_payload,
        "proposal": proposal,
        "interpreted_action": dict(normalized),
        "existing_events_preserved": _events_from_payload(payload),
        "existing_journal_entries_preserved": _journal_entries_from_payload(payload),
        "new_events": [],
        "new_journal_entries": [journal_entry],
        "removed_events": [],
        "removed_journal_entries": [],
        "replaced_events": [],
        "incremental_impact": impact,
        "resulting_summary": adjusted_result.summary,
        "_adjusted_result": adjusted_result,
    }


def _solve_compound_events_adjustment(
    payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
    current_result: Any,
    months: list[str],
) -> Dict[str, Any]:
    events = [
        _event_with_metadata(event, normalized)
        for event in (normalized.get("events") or [])
        if isinstance(event, Mapping)
    ]
    if not events:
        return _clarification("Indique los movimientos contables a registrar.")

    adjusted_payload = deepcopy(dict(payload or {}))
    movements = dict(adjusted_payload.get("movements") or {})
    existing = list(movements.get("events") or adjusted_payload.get("events") or [])
    movements["events"] = [*existing, *events]
    adjusted_payload["movements"] = movements
    adjusted_result = build_financial_model(adjusted_payload)
    impact_month = str(normalized.get("target_month") or events[0].get("month") or (months[-1] if months else ""))
    proposal = {
        "kind": "compound_events",
        "target_month": impact_month,
        "events": events,
        "events_count": len(events),
        "event_labels": [_event_label(event) for event in events],
        "journal_rows": _compound_events_journal_rows(events),
        "explanation": _compound_events_explanation(events),
        "impact": _impact(current_result, adjusted_result, impact_month),
    }
    return {
        "ok": True,
        "action": dict(normalized),
        "adjusted_payload": adjusted_payload,
        "proposal": proposal,
        "interpreted_action": dict(normalized),
        "existing_events_preserved": _events_from_payload(payload),
        "existing_journal_entries_preserved": _journal_entries_from_payload(payload),
        "new_events": events,
        "new_journal_entries": [],
        "removed_events": [],
        "removed_journal_entries": [],
        "replaced_events": [],
        "incremental_impact": _impact(current_result, adjusted_result, impact_month),
        "resulting_summary": adjusted_result.summary,
        "_adjusted_result": adjusted_result,
    }


def _solve_cash_series_target(
    payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
    current_result: Any,
    months: list[str],
    *,
    comparison_result: Any,
    replaced_events: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    replaced_events = replaced_events or []
    target_cash = _round_money(normalized["target_cash"])
    variability_pct = float(normalized.get("cash_variability_pct") or 20.0)
    lever = str(normalized["lever"])
    if lever != "purchase_adjustment":
        return _clarification("Por ahora el ajuste mensual de caja promedio solo esta implementado para compras.")

    adjusted_payload = deepcopy(dict(payload or {}))
    targets_by_month = _build_series_targets(
        target_cash,
        variability_pct,
        months,
        str(current_result.summary.get("seed") or ""),
    )
    events: list[Dict[str, Any]] = []
    for month in months:
        month_target_cash = targets_by_month.get(month, target_cash)
        interim_result = build_financial_model(adjusted_payload)
        current_cash = _statement_value(
            _esf_df(interim_result),
            "Descripcion",
            "Efectivo y Equivalentes de Efectivo",
            month,
        )
        difference = _round_money(month_target_cash - current_cash)
        if abs(difference) <= 1:
            continue

        current_purchases = _account_movement_value(interim_result, "Inventarios", "Aumentos", month)
        event_amount = _round_money(-difference)
        if current_purchases + event_amount < -1:
            max_cash = _round_money(current_cash + current_purchases)
            return _not_viable(
                "Solo ajustando compras no se puede llegar al objetivo en todos los meses. "
                f"En {month}, bajando compras a cero, la caja maxima seria {max_cash:,.0f}. "
                "Use una palanca adicional como proveedores, prestamo o aporte de capital."
            )
        event = _event_with_metadata({
            "month": month,
            "account": "purchase_adjustment",
            "amount": event_amount,
            "currency": "nio",
        }, normalized)
        adjusted_payload = _append_event(adjusted_payload, event)
        events.append(event)

    adjusted_result = build_financial_model(adjusted_payload)
    current_cash_values = _cash_values(comparison_result, months)
    adjusted_cash_values = _cash_values(adjusted_result, months)
    target_cash_values = [targets_by_month.get(month, target_cash) for month in months]
    purchase_average_nio = _average_account_movement(adjusted_result, "Inventarios", "Aumentos", months)
    exchange_rate = float(current_result.metadata.get("exchange_rate") or 1.0)
    purchase_average_usd = _round_money(purchase_average_nio / exchange_rate) if exchange_rate else 0.0
    adjusted_average_cash = _round_money(sum(adjusted_cash_values) / len(adjusted_cash_values)) if adjusted_cash_values else 0.0
    current_average_cash = _round_money(sum(current_cash_values) / len(current_cash_values)) if current_cash_values else 0.0

    return {
        "ok": True,
        "action": dict(normalized),
        "adjusted_payload": adjusted_payload,
        "proposal": {
            "target_month": "todos los meses",
            "target_cash": target_cash,
            "cash_variability_pct": variability_pct,
            "target_cash_months": [{"month": month, "cash": targets_by_month.get(month, target_cash)} for month in months],
            "target_min_cash": min(target_cash_values) if target_cash_values else target_cash,
            "target_max_cash": max(target_cash_values) if target_cash_values else target_cash,
            "current_cash": current_average_cash,
            "adjusted_cash": adjusted_average_cash,
            "difference": _round_money(adjusted_average_cash - current_average_cash),
            "lever": lever,
            "event": events[0] if len(events) == 1 else None,
            "events": events,
            "events_count": len(events),
            "purchase_average_nio": purchase_average_nio,
            "purchase_average_usd": purchase_average_usd,
            "current_min_cash": min(current_cash_values) if current_cash_values else 0.0,
            "adjusted_min_cash": min(adjusted_cash_values) if adjusted_cash_values else 0.0,
            "adjusted_max_cash": max(adjusted_cash_values) if adjusted_cash_values else 0.0,
            "explanation": (
                "Reduce o aumenta compras mes a mes para que el saldo de caja de cada periodo quede alrededor "
                f"de {target_cash:,.0f}, con variacion de +/-{variability_pct:g}%. "
                f"Compras promedio resultantes: {purchase_average_nio:,.0f} cordobas "
                f"({purchase_average_usd:,.0f} USD)."
            ),
            "impact": _impact(comparison_result, adjusted_result, months[-1]) if months else {},
        },
        "interpreted_action": dict(normalized),
        "existing_events_preserved": _events_from_payload(payload),
        "new_events": events,
        "removed_events": [],
        "replaced_events": replaced_events,
        "incremental_impact": _impact(comparison_result, adjusted_result, months[-1]) if months else {},
        "resulting_summary": adjusted_result.summary,
        "_adjusted_result": adjusted_result,
    }


def _solve_assumption_change(
    payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
    current_result: Any,
    months: list[str],
    *,
    scope_mode: str,
) -> Dict[str, Any]:
    if not months:
        return _clarification("No hay meses disponibles para aplicar el supuesto.")

    cost_pct = float(normalized.get("value") or 0.0)
    cost_variability_pct = float(normalized.get("cash_variability_pct") or 0.0)
    adjusted_payload = _payload_with_cost_assumption(
        payload,
        cost_pct=cost_pct,
        cost_variability_pct=cost_variability_pct,
        months=months,
        scope_mode=scope_mode,
    )
    adjusted_result = build_financial_model(adjusted_payload)
    impact_month = months[-1]
    scope_label = "modelo completo" if scope_mode == "global" else "bloque seleccionado"
    period_label = months[0] if len(months) == 1 else f"{months[0]} a {months[-1]}"

    return {
        "ok": True,
        "action": dict(normalized),
        "adjusted_payload": adjusted_payload,
        "proposal": {
            "kind": "assumption_change",
            "target_month": period_label,
            "target_cash": None,
            "current_cash": _statement_value(_esf_df(current_result), "Descripcion", "Efectivo y Equivalentes de Efectivo", impact_month),
            "adjusted_cash": _statement_value(_esf_df(adjusted_result), "Descripcion", "Efectivo y Equivalentes de Efectivo", impact_month),
            "difference": 0,
            "lever": None,
            "assumption": "cost_pct",
            "assumption_label": "Costo de venta",
            "assumption_value": cost_pct,
            "assumption_variability_pct": cost_variability_pct,
            "scope": scope_mode,
            "scope_label": scope_label,
            "affected_months": months,
            "affected_months_count": len(months),
            "explanation": (
                f"Cambia el supuesto de costo de venta a {cost_pct:g}% con variacion de +/-{cost_variability_pct:g}% "
                f"para el {scope_label} ({period_label})."
            ),
            "impact": _impact(current_result, adjusted_result, impact_month),
        },
        "interpreted_action": dict(normalized),
        "existing_events_preserved": _events_from_payload(payload),
        "new_events": [],
        "removed_events": [],
        "replaced_events": [],
        "incremental_impact": _impact(current_result, adjusted_result, impact_month),
        "resulting_summary": adjusted_result.summary,
        "_adjusted_result": adjusted_result,
    }


def _solve_equity_cash_adjustment(
    payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
    current_result: Any,
    months: list[str],
    *,
    comparison_result: Any,
    replaced_events: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    replaced_events = replaced_events or []
    target_month = str(normalized.get("target_month") or (months[-1] if months else ""))
    amount = _round_money(normalized.get("amount"))
    lever = str(normalized.get("lever") or "")
    if lever == "capital_reclassification" and amount == 0:
        return _clarification("Indique el monto a reclasificar entre capital y resultados acumulados.")
    if lever in {"owner_withdrawal", "retained_earnings_distribution"} and amount <= 0:
        return _clarification("Indique un monto positivo para retirar de caja.")
    capital_before = _statement_value(_esf_df(current_result), "Descripcion", "Capital", target_month)
    retained_before = _statement_value(_esf_df(current_result), "Descripcion", "Resultados Acumulados", target_month)
    if lever == "owner_withdrawal" and amount - capital_before > 1:
        return _not_viable(f"El retiro excede el capital disponible en {target_month}. Capital actual: {capital_before:,.0f}.")
    if lever == "retained_earnings_distribution" and amount - retained_before > 1:
        return _not_viable(f"El retiro excede los resultados acumulados disponibles en {target_month}. Resultados acumulados: {retained_before:,.0f}.")
    if lever == "capital_reclassification":
        if amount > 0 and amount - capital_before > 1:
            return _not_viable(f"La reclasificacion excede el capital disponible en {target_month}. Capital actual: {capital_before:,.0f}.")
        if amount < 0 and abs(amount) - retained_before > 1:
            return _not_viable(f"La reclasificacion excede los resultados acumulados disponibles en {target_month}. Resultados acumulados: {retained_before:,.0f}.")

    event = _event_with_metadata({
        "month": target_month,
        "account": lever,
        "amount": amount,
        "currency": "nio",
    }, normalized)
    adjusted_payload = _append_event(payload, event)
    adjusted_result = build_financial_model(adjusted_payload)
    if months:
        impact_month = target_month if target_month in months else months[-1]
    else:
        impact_month = target_month

    explanations = {
        "owner_withdrawal": "Salida de banco contra capital; baja caja y baja capital sin tocar el ER.",
        "retained_earnings_distribution": "Salida de banco contra resultados acumulados; baja caja y baja resultados acumulados sin tocar el ER.",
        "capital_reclassification": "Reclasificacion patrimonial sin movimiento de caja entre capital y resultados acumulados.",
    }
    cash_before = _statement_value(_esf_df(comparison_result), "Descripcion", "Efectivo y Equivalentes de Efectivo", impact_month)
    cash_after = _statement_value(_esf_df(adjusted_result), "Descripcion", "Efectivo y Equivalentes de Efectivo", impact_month)

    return {
        "ok": True,
        "action": dict(normalized),
        "adjusted_payload": adjusted_payload,
        "proposal": {
            "target_month": target_month,
            "target_cash": None,
            "current_cash": cash_before,
            "adjusted_cash": cash_after,
            "difference": _round_money(cash_after - cash_before),
            "lever": lever,
            "event": event,
            "events": [event],
            "events_count": 1,
            "explanation": explanations.get(lever, "Ajuste patrimonial propuesto."),
            "impact": _impact(comparison_result, adjusted_result, impact_month),
        },
        "interpreted_action": dict(normalized),
        "existing_events_preserved": _events_from_payload(payload),
        "new_events": [event],
        "removed_events": [],
        "replaced_events": replaced_events,
        "incremental_impact": _impact(comparison_result, adjusted_result, impact_month),
        "resulting_summary": adjusted_result.summary,
        "_adjusted_result": adjusted_result,
    }


def _solve_undo_last_adjustment(
    payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
    current_result: Any,
    months: list[str],
) -> Dict[str, Any]:
    events = _events_from_payload(payload)
    journal_entries = _journal_entries_from_payload(payload)
    last_instruction = _last_chat_instruction_id([*events, *journal_entries])
    if not last_instruction:
        return _not_viable("No hay ajustes de chat para deshacer.")
    preserved = [event for event in events if str(event.get("instruction_id") or "") != last_instruction]
    removed = [event for event in events if str(event.get("instruction_id") or "") == last_instruction]
    preserved_journals = [entry for entry in journal_entries if str(entry.get("instruction_id") or "") != last_instruction]
    removed_journals = [entry for entry in journal_entries if str(entry.get("instruction_id") or "") == last_instruction]
    adjusted_payload = _payload_with_events(payload, preserved)
    adjusted_payload = _payload_with_journal_entries(adjusted_payload, preserved_journals)
    adjusted_result = build_financial_model(adjusted_payload)
    impact_month = months[-1] if months else ""

    return {
        "ok": True,
        "action": dict(normalized),
        "adjusted_payload": adjusted_payload,
        "proposal": {
            "target_month": impact_month,
            "target_cash": None,
            "current_cash": _statement_value(_esf_df(current_result), "Descripcion", "Efectivo y Equivalentes de Efectivo", impact_month),
            "adjusted_cash": _statement_value(_esf_df(adjusted_result), "Descripcion", "Efectivo y Equivalentes de Efectivo", impact_month),
            "difference": _round_money(
                _statement_value(_esf_df(adjusted_result), "Descripcion", "Efectivo y Equivalentes de Efectivo", impact_month)
                - _statement_value(_esf_df(current_result), "Descripcion", "Efectivo y Equivalentes de Efectivo", impact_month)
            ),
            "lever": "undo_last_adjustment",
            "event": None,
            "events": [],
            "events_count": 0,
            "removed_events_count": len(removed) + len(removed_journals),
            "explanation": f"Deshace el ultimo ajuste aplicado por chat ({len(removed) + len(removed_journals)} registro(s)).",
            "impact": _impact(current_result, adjusted_result, impact_month) if impact_month else {},
        },
        "interpreted_action": dict(normalized),
        "existing_events_preserved": preserved,
        "existing_journal_entries_preserved": preserved_journals,
        "new_events": [],
        "removed_events": removed,
        "new_journal_entries": [],
        "removed_journal_entries": removed_journals,
        "replaced_events": [],
        "incremental_impact": _impact(current_result, adjusted_result, impact_month) if impact_month else {},
        "resulting_summary": adjusted_result.summary,
        "_adjusted_result": adjusted_result,
    }


def heuristic_interpret_cash_instruction(message: str, months: list[str]) -> Dict[str, Any]:
    """Rule-based parser used by tests and as a readable reference for the LLM contract."""
    text = _normalize_text(message)
    ledger_action = _extract_ledger_action(text, months)
    if ledger_action:
        return normalize_action(ledger_action, months)
    assumption_action = _extract_assumption_action(text, months)
    if assumption_action:
        return normalize_action(assumption_action, months)
    equity_action = _extract_equity_action(text, months)
    if equity_action:
        equity_action["replace_existing"] = _is_replace_request(text)
        return normalize_action(equity_action, months)
    if _is_undo_request(text):
        return normalize_action({"intent": "undo_last_adjustment"}, months)
    target_cash = _extract_cash_amount(text)
    lever = _extract_lever(text)
    target_month = _extract_month(text, months) or (months[-1] if months else "")
    if target_cash is None or not lever:
        return _clarification_action(
            "No identifique una instruccion contable aplicable. Si ya hay una propuesta, use Aplicar ajuste; si quiere un movimiento nuevo, indique cuenta, monto y mes."
        )
    return normalize_action(
        {
            "intent": "target_cash_series" if _is_series_request(text) else "target_cash_balance",
            "target_month": target_month,
            "target_cash": target_cash,
            "cash_variability_pct": _extract_variability_pct(text),
            "replace_existing": _is_replace_request(text),
            "lever": lever,
        },
        months,
    )


def _append_event(payload: Mapping[str, Any], event: Mapping[str, Any]) -> Dict[str, Any]:
    adjusted_payload = deepcopy(dict(payload or {}))
    movements = dict(adjusted_payload.get("movements") or {})
    events = list(movements.get("events") or adjusted_payload.get("events") or [])
    events.append(dict(event))
    movements["events"] = events
    adjusted_payload["movements"] = movements
    return adjusted_payload


def _payload_with_events(payload: Mapping[str, Any], events: list[Mapping[str, Any]]) -> Dict[str, Any]:
    adjusted_payload = deepcopy(dict(payload or {}))
    movements = dict(adjusted_payload.get("movements") or {})
    movements["events"] = [dict(event) for event in events]
    adjusted_payload["movements"] = movements
    return adjusted_payload


def _append_journal_entry(payload: Mapping[str, Any], journal_entry: Mapping[str, Any]) -> Dict[str, Any]:
    adjusted_payload = deepcopy(dict(payload or {}))
    movements = dict(adjusted_payload.get("movements") or {})
    journal_entries = list(movements.get("journal_entries") or [])
    journal_entries.append(dict(journal_entry))
    movements["journal_entries"] = journal_entries
    adjusted_payload["movements"] = movements
    return adjusted_payload


def _payload_with_journal_entries(payload: Mapping[str, Any], journal_entries: list[Mapping[str, Any]]) -> Dict[str, Any]:
    adjusted_payload = deepcopy(dict(payload or {}))
    movements = dict(adjusted_payload.get("movements") or {})
    movements["journal_entries"] = [dict(entry) for entry in journal_entries]
    adjusted_payload["movements"] = movements
    return adjusted_payload


def _journal_entries_from_payload(payload: Mapping[str, Any]) -> list[Dict[str, Any]]:
    movements = dict((payload or {}).get("movements") or {})
    return [dict(entry) for entry in (movements.get("journal_entries") or []) if isinstance(entry, Mapping)]


def _payload_with_cost_assumption(
    payload: Mapping[str, Any],
    *,
    cost_pct: float,
    cost_variability_pct: float,
    months: list[str],
    scope_mode: str,
) -> Dict[str, Any]:
    adjusted_payload = deepcopy(dict(payload or {}))
    income = dict(adjusted_payload.get("income") or {})
    if scope_mode == "global":
        income["cost_pct"] = cost_pct
        income["cost_variability_pct"] = cost_variability_pct
        overrides = _income_overrides_by_month(income.get("monthly_overrides") or [])
        for month in months:
            if month in overrides:
                overrides[month].pop("cost_pct", None)
                overrides[month].pop("cost_variability_pct", None)
        income["monthly_overrides"] = _income_overrides_list(overrides)
    else:
        overrides = _income_overrides_by_month(income.get("monthly_overrides") or [])
        for month in months:
            record = dict(overrides.get(month) or {})
            record["month"] = month
            record["cost_pct"] = cost_pct
            record["cost_variability_pct"] = cost_variability_pct
            overrides[month] = record
        income["monthly_overrides"] = _income_overrides_list(overrides)
    adjusted_payload["income"] = income
    return adjusted_payload


def _income_overrides_by_month(raw: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw, Mapping):
        iterable = []
        for month, values in raw.items():
            record = dict(values or {}) if isinstance(values, Mapping) else {}
            record["month"] = month
            iterable.append(record)
    elif isinstance(raw, list):
        iterable = raw
    else:
        iterable = []
    for item in iterable:
        if not isinstance(item, Mapping):
            continue
        month = _normalize_month(item.get("month") or item.get("mes"))
        if not month:
            continue
        record = dict(item)
        record["month"] = month
        out[month] = record
    return out


def _income_overrides_list(overrides: Mapping[str, Mapping[str, Any]]) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    for month in sorted(overrides):
        record = {k: v for k, v in dict(overrides[month]).items() if k not in {None, ""}}
        record["month"] = month
        if any(k != "month" for k in record):
            out.append(record)
    return out


def _normalize_month(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 7 and re.match(r"^\d{4}-\d{2}", text):
        return text[:7]
    return ""


def _events_from_payload(payload: Mapping[str, Any]) -> list[Dict[str, Any]]:
    movements = dict((payload or {}).get("movements") or {})
    return [dict(event) for event in (movements.get("events") or (payload or {}).get("events") or []) if isinstance(event, Mapping)]


def _payload_for_action(payload: Mapping[str, Any], action: Mapping[str, Any]) -> tuple[Dict[str, Any], list[Dict[str, Any]]]:
    events = _events_from_payload(payload)
    if not action.get("replace_existing"):
        return deepcopy(dict(payload or {})), []
    lever = str(action.get("lever") or "")
    replaced = [
        event for event in events
        if event.get("source") == CHAT_SOURCE and str(event.get("account") or "") == lever
    ]
    preserved = [
        event for event in events
        if not (event.get("source") == CHAT_SOURCE and str(event.get("account") or "") == lever)
    ]
    return _payload_with_events(payload, preserved), replaced


def _event_with_metadata(event: Mapping[str, Any], action: Mapping[str, Any]) -> Dict[str, Any]:
    enriched = dict(event)
    enriched["source"] = CHAT_SOURCE
    enriched["instruction_id"] = str(action.get("instruction_id") or _new_instruction_id())
    enriched["locked"] = True
    enriched["message"] = str(action.get("message") or "")
    enriched["created_at"] = str(action.get("created_at") or _utc_now())
    return enriched


def _journal_entry_with_metadata(entry: Mapping[str, Any], action: Mapping[str, Any]) -> Dict[str, Any]:
    enriched = dict(entry)
    enriched["source"] = CHAT_SOURCE
    enriched["instruction_id"] = str(action.get("instruction_id") or _new_instruction_id())
    enriched["locked"] = True
    enriched["message"] = str(action.get("message") or "")
    enriched["created_at"] = str(action.get("created_at") or _utc_now())
    return enriched


def _event_label(event: Mapping[str, Any]) -> str:
    labels = {
        "asset_vehicle": "Alta de vehiculo",
        "asset_equipment": "Alta de mobiliario/equipo",
        "asset_real_estate": "Alta de bien inmueble",
        "owner_withdrawal": "Salida de efectivo contra capital",
        "retained_earnings_distribution": "Salida de efectivo contra resultados acumulados",
        "loan_pledge_new": "Nuevo credito prendario",
        "loan_commercial_new": "Nuevo credito comercial",
        "loan_personal_new": "Nuevo credito personal",
        "capital_contribution": "Aporte de capital",
        "supplier_new": "Nuevo saldo con proveedores",
    }
    account = str(event.get("account") or "")
    return f"{labels.get(account, account)}: {float(event.get('amount') or 0):,.0f}"


def _compound_events_explanation(events: list[Mapping[str, Any]]) -> str:
    labels = [_event_label(event) for event in events]
    return "Movimiento compuesto propuesto: " + "; ".join(labels) + "."


def _compound_events_journal_rows(events: list[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    for event in events:
        account = str(event.get("account") or "")
        amount = float(event.get("amount") or 0)
        if amount <= 0:
            continue
        if account == "asset_vehicle":
            rows.append({"account": "Vehiculos", "debit": amount, "credit": 0})
            rows.append({"account": "Efectivo y Equivalentes de Efectivo", "debit": 0, "credit": amount})
        elif account == "asset_equipment":
            rows.append({"account": "Mobiliario y Equipos", "debit": amount, "credit": 0})
            rows.append({"account": "Efectivo y Equivalentes de Efectivo", "debit": 0, "credit": amount})
        elif account == "asset_real_estate":
            rows.append({"account": "Bienes Inmuebles", "debit": amount, "credit": 0})
            rows.append({"account": "Efectivo y Equivalentes de Efectivo", "debit": 0, "credit": amount})
        elif account == "loan_pledge_new":
            rows.append({"account": "Efectivo y Equivalentes de Efectivo", "debit": amount, "credit": 0})
            rows.append({"account": "Creditos Prendarios", "debit": 0, "credit": amount})
        elif account == "loan_commercial_new":
            rows.append({"account": "Efectivo y Equivalentes de Efectivo", "debit": amount, "credit": 0})
            rows.append({"account": "Creditos Comerciales", "debit": 0, "credit": amount})
        elif account == "loan_personal_new":
            rows.append({"account": "Efectivo y Equivalentes de Efectivo", "debit": amount, "credit": 0})
            rows.append({"account": "Creditos Personales", "debit": 0, "credit": amount})
        elif account == "capital_contribution":
            rows.append({"account": "Efectivo y Equivalentes de Efectivo", "debit": amount, "credit": 0})
            rows.append({"account": "Capital", "debit": 0, "credit": amount})
        elif account == "owner_withdrawal":
            rows.append({"account": "Capital", "debit": amount, "credit": 0})
            rows.append({"account": "Efectivo y Equivalentes de Efectivo", "debit": 0, "credit": amount})
        elif account == "supplier_new":
            rows.append({"account": "Efectivo y Equivalentes de Efectivo", "debit": amount, "credit": 0})
            rows.append({"account": "Proveedores", "debit": 0, "credit": amount})
    return _net_journal_rows(rows)


def _net_journal_rows(rows: list[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    totals: Dict[str, float] = {}
    for row in rows:
        account = str(row.get("account") or "")
        totals[account] = totals.get(account, 0.0) + float(row.get("debit") or 0) - float(row.get("credit") or 0)
    out = []
    for account, net in totals.items():
        if abs(net) < 0.5:
            continue
        out.append({
            "account": account,
            "debit": round(net) if net > 0 else 0,
            "credit": round(abs(net)) if net < 0 else 0,
        })
    return out


def _last_chat_instruction_id(events: list[Mapping[str, Any]]) -> str:
    candidates = [
        event for event in events
        if event.get("source") == CHAT_SOURCE and event.get("instruction_id")
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda event: str(event.get("created_at") or ""))
    return str(candidates[-1].get("instruction_id") or "")


def _cash_values(result: Any, months: list[str]) -> list[float]:
    return [
        _statement_value(_esf_df(result), "Descripcion", "Efectivo y Equivalentes de Efectivo", month)
        for month in months
    ]


def _average_account_movement(result: Any, account: str, movement: str, months: list[str]) -> float:
    if not months:
        return 0.0
    values = [_account_movement_value(result, account, movement, month) for month in months]
    return _round_money(sum(values) / len(values))


def _build_series_targets(target_cash: float, variability_pct: float, months: list[str], seed: str) -> Dict[str, float]:
    if not months:
        return {}
    variability = max(0.0, min(0.5, variability_pct / 100.0))
    if variability <= 0 or len(months) == 1:
        return {month: _round_money(target_cash) for month in months}

    rng = random.Random(f"{seed}|cash_series|{target_cash}|{variability_pct}|{','.join(months)}")
    offsets = [rng.uniform(-variability, variability) for _ in months]
    mean_offset = sum(offsets) / len(offsets)
    centered = [offset - mean_offset for offset in offsets]
    max_abs = max(abs(offset) for offset in centered) or 1.0
    scale = min(1.0, variability / max_abs)
    bounded = [offset * scale for offset in centered]
    return {
        month: _round_money(target_cash * (1.0 + offset))
        for month, offset in zip(months, bounded)
    }


def _impact(current_result: Any, adjusted_result: Any, month: str) -> Dict[str, float]:
    labels = {
        "cash": "Efectivo y Equivalentes de Efectivo",
        "inventory": "Inventarios",
        "suppliers": "Proveedores",
        "liabilities": "Total Pasivos",
        "equity": "Total Patrimonio",
    }
    out: Dict[str, float] = {}
    for key, label in labels.items():
        current = _statement_value(_esf_df(current_result), "Descripcion", label, month)
        adjusted = _statement_value(_esf_df(adjusted_result), "Descripcion", label, month)
        out[key] = _round_money(adjusted - current)
    return out


def _statement_value(df: pd.DataFrame, label_column: str, label: str, month: str) -> float:
    if df is None or df.empty:
        return 0.0
    col = _month_column(df, month)
    if col is None:
        return 0.0
    label_col = label_column if label_column in df.columns else df.columns[0]
    matches = df[df[label_col].astype(str).str.strip().str.lower() == label.lower()]
    if matches.empty:
        return 0.0
    return _round_money(matches.iloc[0][col])


def _account_movement_value(result: Any, account: str, movement: str, month: str) -> float:
    df = _account_mov_df(result)
    if df is None or df.empty:
        return 0.0
    col = _month_column(df, month)
    if col is None:
        return 0.0
    matches = df[
        (df["Cuenta"].astype(str).str.strip().str.lower() == account.lower())
        & (df["Movimiento"].astype(str).str.strip().str.lower() == movement.lower())
    ]
    if matches.empty:
        return 0.0
    return _round_money(matches.iloc[0][col])


def _cash_flow_value(result: Any, concept: str, month: str) -> float:
    return _statement_value(_cash_flow_df(result), "Concepto", concept, month)


def _month_column(df: pd.DataFrame, month: str) -> Optional[Any]:
    for col in df.columns:
        if _format_month(col) == month:
            return col
    return None


def _format_month(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m")
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}", text):
        return text[:7]
    return text


def _normalize_lever(value: Any) -> str:
    key = _normalize_text(value).replace(" ", "_").replace("-", "_")
    aliases = {
        "compras": "purchase_adjustment",
        "compra": "purchase_adjustment",
        "purchase": "purchase_adjustment",
        "purchase_adjustment": "purchase_adjustment",
        "proveedores": "supplier_financing",
        "proveedor": "supplier_financing",
        "supplier": "supplier_financing",
        "supplier_financing": "supplier_financing",
        "prestamo": "loan_commercial_new",
        "credito": "loan_commercial_new",
        "credito_comercial": "loan_commercial_new",
        "loan": "loan_commercial_new",
        "loan_commercial_new": "loan_commercial_new",
        "aporte": "capital_contribution",
        "aporte_capital": "capital_contribution",
        "capital": "capital_contribution",
        "capital_contribution": "capital_contribution",
        "retiro": "owner_withdrawal",
        "retiro_capital": "owner_withdrawal",
        "owner_withdrawal": "owner_withdrawal",
        "resultados": "retained_earnings_distribution",
        "resultados_acumulados": "retained_earnings_distribution",
        "retained_earnings_distribution": "retained_earnings_distribution",
        "reclasificacion": "capital_reclassification",
        "capital_reclassification": "capital_reclassification",
    }
    lever = aliases.get(key, key)
    return lever if lever in ALLOWED_LEVERS else ""


def _extract_lever(text: str) -> str:
    if "resultado" in text and ("saca" in text or "retira" in text or "resta" in text or "distribu" in text):
        return "retained_earnings_distribution"
    if "capital" in text and ("saca" in text or "retira" in text or "resta" in text):
        return "owner_withdrawal"
    if ("reclas" in text or "mueve" in text or "traslada" in text) and "capital" in text and "resultado" in text:
        return "capital_reclassification"
    if "compra" in text:
        return "purchase_adjustment"
    if "proveedor" in text:
        return "supplier_financing"
    if "prestamo" in text or "credito" in text:
        return "loan_commercial_new"
    if "aporte" in text or "capital" in text:
        return "capital_contribution"
    return ""


def _extract_ledger_action(text: str, months: list[str]) -> Optional[Dict[str, Any]]:
    compound_action = _extract_asset_financing_action(text, months)
    if compound_action:
        return compound_action

    amount = _extract_money_amount(text)
    target_month = _extract_month(text, months) or (months[-1] if months else None)
    month_mentions = _extract_month_mentions(text)

    if "result" in text and "acumul" in text and any(marker in text for marker in ("cierre", "traslada", "trasladar", "mueve", "pasar")):
        source_month = None
        if month_mentions:
            if len(month_mentions) > 1:
                source_month = month_mentions[0]
                target_month = month_mentions[-1]
            else:
                mentioned_month = month_mentions[0]
                mentioned_year = int(mentioned_month[:4])
                source_years = [
                    int(year)
                    for year in re.findall(r"\b(20\d{2})\b", text)
                    if int(year) < mentioned_year
                ]
                if source_years:
                    source_month = f"{max(source_years)}-12"
                    target_month = mentioned_month
                elif mentioned_month.endswith("-12"):
                    source_month = mentioned_month
                    target_month = _next_month(mentioned_month)
                else:
                    target_month = mentioned_month
                    source_month = _previous_year_end(mentioned_month)
        if not source_month and target_month:
            source_month = _previous_year_end(target_month)
        if not target_month and source_month:
            target_month = _next_month(source_month)
        return {
            "intent": "year_close_transfer",
            "target_month": target_month,
            "source_month": source_month,
            "amount": amount,
            "debit_account": "current_earnings",
            "credit_account": "retained_earnings",
            "source_account": "current_earnings",
            "destination_account": "retained_earnings",
        }

    debit_account = _extract_account_after(text, ("debitando", "debita", "debe"))
    credit_account = _extract_account_after(text, ("acreditando", "acredita", "haber"))
    if debit_account and credit_account:
        return {
            "intent": "journal_entry",
            "target_month": target_month,
            "amount": amount,
            "debit_account": debit_account,
            "credit_account": credit_account,
        }

    if any(marker in text for marker in ("reclasifica", "reclasificar", "traslada", "trasladar", "mueve", "mover")):
        source_account, destination_account = _extract_transfer_accounts(text)
        if source_account and destination_account:
            return {
                "intent": "account_transfer",
                "target_month": target_month,
                "amount": amount,
                "source_account": source_account,
                "destination_account": destination_account,
            }
    return None


def _extract_asset_financing_action(text: str, months: list[str]) -> Optional[Dict[str, Any]]:
    if not any(token in text for token in ("vehiculo", "vehiculos", "equipo", "mobiliario", "inmueble", "vivienda")):
        return None
    if not any(token in text for token in ("efectivo", "caja", "banco", "credito", "prestamo", "prendario", "prendadio", "pasivo")):
        return None
    month = _extract_month(text, months) or (months[-1] if months else "")
    asset_account = "asset_vehicle"
    if "inmueble" in text or "vivienda" in text:
        asset_account = "asset_real_estate"
    elif "equipo" in text or "mobiliario" in text:
        asset_account = "asset_equipment"

    asset_amount = _extract_amount_near(text, ("vehiculo", "vehiculos", "equipo", "mobiliario", "inmueble", "vivienda", "incorporacion"))
    cash_amount = _extract_amount_near(text, ("efectivo", "caja", "banco"))
    loan_amount = _extract_amount_near(text, ("prendario", "prendadio", "pasivo", "credito", "prestamo"))
    numbers = _extract_all_money_amounts(text)
    if asset_amount is None and numbers:
        asset_amount = max(numbers)
    if cash_amount is None and asset_amount is not None and loan_amount is not None:
        cash_amount = asset_amount - loan_amount
    if loan_amount is None and asset_amount is not None and cash_amount is not None:
        loan_amount = asset_amount - cash_amount
    if not asset_amount or asset_amount <= 0:
        return None

    events = [{"month": month, "account": asset_account, "amount": asset_amount, "currency": "nio"}]
    if loan_amount and loan_amount > 0:
        loan_account = "loan_pledge_new" if "prend" in text else "loan_commercial_new"
        events.append({"month": month, "account": loan_account, "amount": loan_amount, "currency": "nio"})
    if cash_amount and loan_amount is None and cash_amount < asset_amount:
        events.append({"month": month, "account": "loan_pledge_new", "amount": asset_amount - cash_amount, "currency": "nio"})
    if len(events) < 2:
        return None
    return {"intent": "compound_events", "target_month": month, "events": events}


def _normalize_ledger_account(value: Any) -> str:
    text = _normalize_text(value)
    if text in LEDGER_ACCOUNT_LABELS:
        return text
    return LEDGER_ACCOUNT_ALIASES.get(text, "")


def _ledger_label(account: str) -> str:
    return LEDGER_ACCOUNT_LABELS.get(str(account or ""), str(account or ""))


def _journal_sides_for_transfer(source_account: str, destination_account: str) -> tuple[str, str]:
    equity_accounts = {"capital", "retained_earnings", "current_earnings"}
    if source_account in equity_accounts and destination_account in equity_accounts:
        return source_account, destination_account
    return "", ""


def _validate_journal_entry(
    current_result: Any,
    target_month: str,
    debit_account: str,
    credit_account: str,
    amount: float,
    source_month: str = "",
) -> Optional[Dict[str, Any]]:
    if amount <= 0:
        return _clarification("El monto de la partida debe ser positivo.")
    if debit_account not in LEDGER_ACCOUNT_LABELS or credit_account not in LEDGER_ACCOUNT_LABELS:
        return _clarification("La partida contiene una cuenta fuera del catalogo controlado.")
    if debit_account == credit_account:
        return _clarification("La cuenta al debe y al haber deben ser diferentes.")
    if debit_account == "current_earnings":
        balance_month = source_month or target_month
        current_balance = _statement_value(_esf_df(current_result), "Descripcion", _ledger_label("current_earnings"), balance_month)
        if amount - current_balance > 1:
            return _not_viable(f"El traslado excede Resultados del Ejercicio en {balance_month}: {current_balance:,.0f}.")
    if debit_account == "retained_earnings":
        retained = _statement_value(_esf_df(current_result), "Descripcion", _ledger_label("retained_earnings"), target_month)
        if amount - retained > 1:
            return _not_viable(f"El traslado excede Resultados Acumulados en {target_month}: {retained:,.0f}.")
    if debit_account == "capital":
        capital = _statement_value(_esf_df(current_result), "Descripcion", _ledger_label("capital"), target_month)
        if amount - capital > 1:
            return _not_viable(f"El traslado excede Capital en {target_month}: {capital:,.0f}.")
    return None


def _ledger_explanation(action: Mapping[str, Any], amount: float, source_month: Optional[str]) -> str:
    intent = action.get("intent")
    debit_label = _ledger_label(str(action.get("debit_account") or ""))
    credit_label = _ledger_label(str(action.get("credit_account") or ""))
    if intent == "year_close_transfer":
        source = f" desde {source_month}" if source_month else ""
        return f"Cierre patrimonial: debita {debit_label}{source} y acredita {credit_label} por {amount:,.0f}."
    if intent == "account_transfer":
        return f"Reclasificacion contable: debita {debit_label} y acredita {credit_label} por {amount:,.0f}."
    return f"Partida doble controlada: debita {debit_label} y acredita {credit_label} por {amount:,.0f}."


def _extract_account_after(text: str, markers: tuple[str, ...]) -> str:
    marker_pattern = "|".join(re.escape(marker) for marker in markers)
    stop = r"(?:\s+por\b|\s+y\b|\s+con\b|\s+en\b|\s+al\b|$)"
    match = re.search(rf"(?:{marker_pattern})\s+(.+?){stop}", text)
    if not match:
        return ""
    return _normalize_ledger_account(match.group(1).strip(" .,:;\"'"))


def _extract_transfer_accounts(text: str) -> tuple[str, str]:
    account_names = sorted(LEDGER_ACCOUNT_ALIASES, key=len, reverse=True)
    found: list[tuple[int, str]] = []
    for name in account_names:
        idx = text.find(name)
        if idx >= 0:
            account = LEDGER_ACCOUNT_ALIASES[name]
            if account not in [item[1] for item in found]:
                found.append((idx, account))
    found.sort(key=lambda item: item[0])
    if len(found) >= 2:
        return found[0][1], found[1][1]
    return "", ""


def _extract_month_mentions(text: str) -> list[str]:
    out: list[str] = []
    direct_matches = re.findall(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", text)
    for year, month in direct_matches:
        out.append(f"{year}-{month}")
    names = {
        "ene": "01", "enero": "01",
        "feb": "02", "febrero": "02",
        "mar": "03", "marzo": "03",
        "abr": "04", "abril": "04",
        "may": "05", "mayo": "05",
        "jun": "06", "junio": "06",
        "jul": "07", "julio": "07",
        "ago": "08", "agosto": "08",
        "sep": "09", "sept": "09", "septiembre": "09",
        "oct": "10", "octubre": "10",
        "nov": "11", "noviembre": "11",
        "dic": "12", "diciembre": "12",
    }
    pattern = r"\b(" + "|".join(sorted(names, key=len, reverse=True)) + r")\s+(20\d{2})\b"
    for match in re.finditer(pattern, text):
        out.append(f"{match.group(2)}-{names[match.group(1)]}")
    seen: list[str] = []
    for month in out:
        if month not in seen:
            seen.append(month)
    return seen


def _previous_year_end(month: str) -> str:
    year = int(str(month)[:4])
    return f"{year - 1}-12"


def _next_month(month: str) -> str:
    year = int(str(month)[:4])
    month_num = int(str(month)[5:7])
    if month_num == 12:
        return f"{year + 1}-01"
    return f"{year}-{month_num + 1:02d}"


def _expand_allowed_source_months(months: list[str]) -> list[str]:
    return list(months)


def _extract_assumption_action(text: str, months: list[str]) -> Optional[Dict[str, Any]]:
    if not any(marker in text for marker in ("costo", "costos", "costo de venta", "porcentaje de costo")):
        return None
    value = _extract_cost_pct(text)
    if value is None:
        return None
    return {
        "intent": "assumption_change",
        "target_month": None,
        "target_cash": None,
        "amount": None,
        "assumption": "cost_pct",
        "value": value,
        "cash_variability_pct": _extract_assumption_variability_pct(text),
        "replace_existing": _is_replace_request(text),
        "lever": None,
    }


def _extract_cost_pct(text: str) -> Optional[float]:
    patterns = [
        r"(?:costo(?: de venta)?|costos?|porcentaje de costo)\D{0,45}?(\d+(?:[.,]\d+)?)\s*%",
        r"(?:costo(?: de venta)?|costos?|porcentaje de costo)\D{0,45}?(\d+(?:[.,]\d+)?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                value = float(match.group(1).replace(",", "."))
            except ValueError:
                continue
            if 0 <= value <= 100:
                return value
    return None


def _extract_assumption_variability_pct(text: str) -> Optional[float]:
    match = re.search(r"(?:\+/-|\+-|mas\s*/?\s*menos|mas o menos)\s*(\d+(?:[.,]\d+)?)\s*%", text)
    if match:
        return abs(float(match.group(1).replace(",", ".")))
    match = re.search(r"(?:variacion|variabilidad|rango)\D{0,30}(\d+(?:[.,]\d+)?)\s*%", text)
    if match:
        return abs(float(match.group(1).replace(",", ".")))
    return None


def _extract_equity_action(text: str, months: list[str]) -> Optional[Dict[str, Any]]:
    amount = _extract_cash_amount(text)
    target_month = _extract_month(text, months) or (months[-1] if months else None)
    if _is_undo_request(text):
        return {"intent": "undo_last_adjustment"}
    if ("reclas" in text or "mueve" in text or "traslada" in text) and "capital" in text and "resultado" in text:
        if amount is None:
            return None
        signed_amount = amount
        if text.find("resultado") < text.find("capital"):
            signed_amount = -amount
        return {
            "intent": "equity_cash_adjustment",
            "target_month": target_month,
            "amount": signed_amount,
            "target_cash": None,
            "lever": "capital_reclassification",
        }
    if ("saca" in text or "retira" in text or "salida" in text) and ("banco" in text or "caja" in text or "efectivo" in text):
        if amount is None:
            return None
        if "resultado" in text:
            return {
                "intent": "equity_cash_adjustment",
                "target_month": target_month,
                "amount": amount,
                "target_cash": None,
                "lever": "retained_earnings_distribution",
            }
        if "capital" in text:
            return {
                "intent": "equity_cash_adjustment",
                "target_month": target_month,
                "amount": amount,
                "target_cash": None,
                "lever": "owner_withdrawal",
            }
    return None


def _extract_cash_amount(text: str) -> Optional[float]:
    amount_re = re.search(r"(\d+(?:[.,]\d+)?)\s*(mm|millon|millones|m)\b", text)
    if amount_re:
        number = float(amount_re.group(1).replace(",", "."))
        return number * 1_000_000
    amount_re = re.search(r"(\d+(?:[.,]\d+)?)\s*(k|mil)\b", text)
    if amount_re:
        number = float(amount_re.group(1).replace(",", "."))
        return number * 1_000
    candidates = re.findall(r"\d[\d.,]*", text)
    if not candidates:
        return None
    token = candidates[-1]
    if "," in token and "." in token:
        token = token.replace(",", "")
    elif "," in token and token.count(",") == 1 and len(token.split(",")[-1]) <= 2:
        token = token.replace(",", ".")
    else:
        token = token.replace(",", "")
    try:
        return float(token)
    except ValueError:
        return None


def _extract_money_amount(text: str) -> Optional[float]:
    scaled = _extract_scaled_amount(text)
    if scaled is not None:
        return scaled
    candidates = []
    for match in re.finditer(r"\b\d[\d.,]*\b", text):
        token = match.group(0)
        if re.fullmatch(r"20\d{2}", token):
            continue
        parsed = _parse_number_token(token)
        if parsed is None:
            continue
        candidates.append(parsed)
    if not candidates:
        return None
    return max(candidates)


def _extract_all_money_amounts(text: str) -> list[float]:
    values: list[float] = []
    for match in re.finditer(r"\b\d[\d.,]*\b", text):
        token = match.group(0)
        if re.fullmatch(r"20\d{2}", token):
            continue
        parsed = _parse_number_token(token)
        if parsed is not None and parsed >= 1000:
            values.append(parsed)
    return values


def _extract_amount_near(text: str, keywords: tuple[str, ...], window: int = 95) -> Optional[float]:
    after_candidates: list[float] = []
    for keyword in keywords:
        pattern = rf"{re.escape(keyword)}\D{{0,45}}(\d[\d.,]*)"
        for match in re.finditer(pattern, text):
            parsed = _parse_number_token(match.group(1))
            if parsed is not None and parsed >= 1000:
                after_candidates.append(parsed)
    if after_candidates:
        return after_candidates[0]

    candidates: list[float] = []
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), text):
            start = max(0, match.start() - window)
            end = min(len(text), match.end() + window)
            candidates.extend(_extract_all_money_amounts(text[start:end]))
    return max(candidates) if candidates else None


def _extract_scaled_amount(text: str) -> Optional[float]:
    amount_re = re.search(r"(\d+(?:[.,]\d+)?)\s*(mm|millon|millones|m)\b", text)
    if amount_re:
        return float(amount_re.group(1).replace(",", ".")) * 1_000_000
    amount_re = re.search(r"(\d+(?:[.,]\d+)?)\s*(k|mil)\b", text)
    if amount_re:
        return float(amount_re.group(1).replace(",", ".")) * 1_000
    return None


def _parse_number_token(token: str) -> Optional[float]:
    token = str(token or "").strip()
    if not token:
        return None
    if "," in token and "." in token:
        token = token.replace(",", "")
    elif "," in token and token.count(",") == 1 and len(token.split(",")[-1]) <= 2:
        token = token.replace(",", ".")
    else:
        token = token.replace(",", "")
    try:
        return float(token)
    except ValueError:
        return None


def _extract_variability_pct(text: str) -> Optional[float]:
    pct_values = []
    for match in re.finditer(r"([+-]?\s*\d+(?:[.,]\d+)?)\s*%", text):
        raw = match.group(1).replace(" ", "").replace(",", ".")
        try:
            pct_values.append(abs(float(raw)))
        except ValueError:
            continue
    if pct_values:
        return max(pct_values)

    match = re.search(r"(?:variacion|variabilidad|oscila\w*|rango)\D{0,20}(\d+(?:[.,]\d+)?)", text)
    if match:
        try:
            return abs(float(match.group(1).replace(",", ".")))
        except ValueError:
            return None
    return None


def _is_series_request(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "promedio",
            "todos los meses",
            "cada mes",
            "saldos de caja",
            "saldo de caja de todos",
            "oscil",
            "alrededor",
        )
    )


def _is_undo_request(text: str) -> bool:
    return any(marker in text for marker in ("deshacer", "deshace", "undo", "revierte", "revertir"))


def _is_replace_request(text: str) -> bool:
    return any(marker in text for marker in ("reemplaza", "reemplazar", "recalcula", "recalcular", "quita", "quitar", "elimina", "eliminar"))


def _extract_month(text: str, months: list[str]) -> str:
    direct = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", text)
    if direct:
        month = direct.group(0)
        if month in months:
            return month
    names = {
        "ene": "01",
        "enero": "01",
        "feb": "02",
        "febrero": "02",
        "mar": "03",
        "marzo": "03",
        "abr": "04",
        "abril": "04",
        "may": "05",
        "mayo": "05",
        "jun": "06",
        "junio": "06",
        "jul": "07",
        "julio": "07",
        "ago": "08",
        "agosto": "08",
        "sep": "09",
        "sept": "09",
        "septiembre": "09",
        "oct": "10",
        "octubre": "10",
        "nov": "11",
        "noviembre": "11",
        "dic": "12",
        "diciembre": "12",
    }
    pattern = r"\b(" + "|".join(sorted(names, key=len, reverse=True)) + r")\s+(20\d{2})\b"
    match = re.search(pattern, text)
    if match:
        month = f"{match.group(2)}-{names[match.group(1)]}"
        if month in months:
            return month
    return ""


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.split())


def _to_float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_money(value: Any) -> float:
    try:
        return float(round(float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _new_instruction_id() -> str:
    return f"chat_{uuid4().hex[:12]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clarification(message: str) -> Dict[str, Any]:
    return {"ok": False, "needs_clarification": True, "error": str(message)}


def _not_viable(message: str) -> Dict[str, Any]:
    return {"ok": False, "not_viable": True, "error": str(message)}


def _clarification_action(message: Any) -> Dict[str, Any]:
    return {
        "intent": "clarification_needed",
        "target_month": None,
        "target_cash": None,
        "amount": None,
        "assumption": None,
        "value": None,
        "source_month": None,
        "debit_account": None,
        "credit_account": None,
        "source_account": None,
        "destination_account": None,
        "cash_variability_pct": None,
        "replace_existing": False,
        "lever": None,
        "clarification": str(message or "Necesito mas informacion."),
    }

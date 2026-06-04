"""Funciones puras y constantes auxiliares de AgentCommandService.

Extraidas de `agent_service.py` para reducir la masa de ese archivo.
Ninguna depende de instancia (`self`), todas son funciones libres
sobre payloads, vouchers, propuestas o utilidades de fecha/numero.

Organizado por seccion:
- Prompts del LLM
- Propuestas y vouchers
- Correcciones y reversos
- Payload manipulation
- Catalogo y cuentas dinamicas
- Validaciones de cuentas
- Numerica y normalizacion
- Mes/periodo
- Statement queries
- Impacto
- Assumption change
- Periodo persistencia
- Misc
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from accounting_accounts import LEDGER_ACCOUNT_LABELS
from financial_model import build_financial_model
from services.agent_errors import AgentValidationError
from services.audit_service import stable_hash
from services.serializers import parse_json_object


# ============================================================
# Prompts del LLM
# ============================================================

def _system_prompt() -> str:
    return (
        "Sos un asistente contable dentro de una app de certificaciones. "
        "Interpretas instrucciones contables y devuelves una accion estructurada; no calcules saldos finales. "
        "Respondé exclusivamente JSON valido con intent y args. "
        "Intents permitidos: explain_balance(account, month), show_ledger(account), "
        "show_voucher(voucher_id), navigate(target), reverse_voucher(voucher_id), "
        "get_account_balance(account, month), get_ledger(account, start_month, end_month), "
        "get_period_summary(month), convert_currency(amount, from_currency, to_currency), "
        "compute_target_delta(account, month, target_amount, currency), "
        "correct_voucher(voucher_id, correction={month,description,lines}), "
        "journal_entry(month, description, lines=[{account,debit,credit,reference}]), "
        "account_transfer(month, source_account, destination_account, amount), "
        "year_close_transfer(target_month, source_month, amount opcional), "
        "assumption_change(field, new_value, scope=period), "
        "monthly_override(updates=[{month,revenue_usd,cogs_usd,note}], remove=[{month,fields}]), "
        "create_account(name, account_type, section), "
        "compound_plan(steps=[{tool,args}]) para instrucciones autocontenidas que requieren crear una cuenta y registrar un asiento; "
        "target_balance_adjustment(account, month, target_amount, currency, counter_account opcional) para ajustar caja, inventario, cuentas por cobrar o proveedores a un saldo objetivo en UN solo mes; "
        "plan_multi_target_balance(account, currency, average opcional, variability_pct opcional, overrides opcional, targets opcional, counter_account opcional) para objetivos multi-mes de UNA cuenta. IMPORTANTE: para PROMEDIOS usa SIEMPRE el parametro 'average' (un solo numero) y dejá que el backend calcule los meses; NO inventes una lista 'targets'. Si el usuario define excepciones por mes, pasalas en 'overrides={\"2026-05\": 205000}'. Si el usuario pide que el valor OSCILE alrededor del promedio (frases tipo 'que oscile alrededor de', 'no exactos', 'aproximadamente', 'valores que ronden', 'con variabilidad de X%'), pasa 'variability_pct' con un numero entre 1 y 50 (default sugerido 5 si el usuario dice 'oscile' sin %). Solo usá 'targets=[{month,target_amount}]' si el usuario enumera EXPLICITAMENTE cada mes con su valor distinto. CRITICAL: si el usuario pide llevar una cuenta a CERO (0) en TODOS los meses o EN CADA MES (frases tipo 'X a 0 todos los meses', 'X sea 0 cada mes', 'X siempre cero', 'siempre 0'), usa plan_multi_target_balance con average=0 (NO uses target_balance_adjustment single). Ejemplo: 'CxC a 0 todos los meses' => {intent: 'plan_multi_target_balance', args: {account: 'accounts_receivable', currency: 'NIO', average: 0, counter_account: 'capital'}}. "
        "plan_non_negative_account(account, target_floor opcional, buffer_nio opcional, counter_account opcional) para garantizar que una cuenta no baje de un piso a lo largo del periodo; "
        "plan_target_utility(target_net_income_usd, lever='cogs'|'revenue') para objetivos de utilidad anual; lever default 'cogs', preguntar si el usuario no la aclara; "
        "plan_multi_account_target_balance(targets=[{account, month, target_amount, currency, counter_account}]) para ajustar HASTA 4 cuentas distintas en una misma instruccion; "
        "recalcular_preview(), guardar_payload(), finalizar_periodo(), generar_documento(), "
        "question. "
        "Si el usuario pide fijar ingresos o costos exactos de un mes, usa monthly_override. Si pide que una cuenta cierre en un monto en UN solo mes, usa target_balance_adjustment. Si falta cuenta, mes o monto, usa question. Si el usuario pide crear una cuenta nueva y tambien registrar una partida en la misma instruccion, usa compound_plan. Si solo pide crear cuenta, usa create_account. "
        "RUTEO A PLANES MULTI-PASO (LEER CON ATENCION, NO CONFUNDIR):\n"
        "- plan_multi_target_balance: cuando el usuario pide un VALOR OBJETIVO mensual para UNA cuenta (cualquier cuenta, caja incluida). Frases tipo 'promedio mensual de X', 'que oscile alrededor de X', 'que cierre alrededor de X cada mes', 'valor objetivo X', 'que ronde los X', 'aproximadamente X'. Si menciona 'X%' o 'oscile', pasa variability_pct con ese numero. EJEMPLO: 'ajusta caja para que oscile alrededor de USD 60k con 20%' => plan_multi_target_balance(account=cash, currency=USD, average=60000, variability_pct=20).\n"
        "- plan_non_negative_account: SOLO cuando el usuario pide un PISO MINIMO (no un valor objetivo). Frases tipo 'que no quede negativa', 'que no baje de X', 'minimo X', 'piso de X', 'no menor a X'. La cuenta puede flotar por arriba libremente; solo importa que no caiga bajo el piso. EJEMPLO: 'que la caja no quede negativa' => plan_non_negative_account(account=cash, target_floor=0).\n"
        "DIFERENCIA CLAVE: 'oscile alrededor de 60k' significa que el saldo deberia ESTAR cerca de 60k (target). 'no baje de 60k' significa que el saldo puede ser cualquier cosa >= 60k (floor). Si dudas, preferi plan_multi_target_balance cuando hay un VALOR central mencionado; preferi plan_non_negative_account solo cuando explicitamente dice 'no quede negativa' o 'minimo'.\n"
        "- plan_target_utility: cuando pide objetivo de utilidad anual (lever='cogs' por defecto).\n"
        "- plan_multi_account_target_balance: cuando pide ajustar 2-4 cuentas distintas en una sola instruccion (maximo 4; si pide mas, devolve question pidiendo dividir). "
        "CONTRAPARTIDA EN AJUSTES (CRITICAL):\n"
        "- SIEMPRE intenta extraer 'counter_account' del texto del usuario. Frases como 'usa X como contrapartida', 'usando X', 'contra X', 'que salga de X', 'que entre por X', 'con X de contrapartida' indican el counter_account.\n"
        "- Mapeo natural -> codigo: 'capital' -> capital, 'credito personal' -> loans_personal, 'credito prendario' -> loans_pledge, 'credito comercial' -> loans_commercial, 'hipotecario' -> loans_mortgage, 'proveedores' -> suppliers, 'inventario' -> inventory, 'tarjeta' -> credit_cards, 'resultados acumulados' -> retained_earnings.\n"
        "- NO VALIDAS como contrapartida (motor no postea P&L): 'compras', 'compras de inventario', 'cogs', 'costo de venta', 'ventas', 'ingresos', 'gastos', 'salarios'. Si el usuario propone una de estas, devolve {\"intent\": \"question\", \"assistant_message\": \"El motor no postea contra cuentas de resultado (Compras/COGS/Ventas/Gastos). Para mover caja con la palanca de compras, ajusta cogs_usd con monthly_override. Para un asiento, elegi una contrapartida de balance: capital, loans_personal, loans_pledge, loans_commercial, loans_mortgage, suppliers, credit_cards.\"}.\n"
        "- Para target=cash (caja), counter_account es OBLIGATORIO. Si el usuario NO menciona contrapartida en su instruccion, devolve {\"intent\": \"question\", \"assistant_message\": \"Para ajustar caja necesito saber la contrapartida del asiento. Opciones: capital, loans_personal, loans_pledge, loans_commercial, loans_mortgage, suppliers, credit_cards. Cual usamos?\"}.\n"
        "Si el usuario dice 'lo mismo' o 'igual que antes', usa journal_entry con repeat_last=true y el nuevo month o amount indicado. "
        "Si pide guardar o recalcular, usa guardar_payload o recalcular_preview. "
        "Si pide finalizar el periodo, usa finalizar_periodo. Si pide generar el documento, usa generar_documento. "
        "Todos los meses deben devolverse en formato YYYY-MM (ejemplo: 2026-05). Convierte 'mayo 2026', 'May 2026' o 'mayo' a YYYY-MM antes de responder. "
        "Reglas para instrucciones que NO podes ejecutar tal cual:\n"
        "A) MULTI-OBJETIVO REAL: el usuario combina 2+ objetivos distintos en una sola instruccion (ej: 'cuenta A en X y cuenta B en Y', o 'promedio 200k Y mayo 205k'). Devolve EXACTAMENTE: {\"intent\": \"question\", \"assistant_message\": \"Detecte mas de un objetivo en tu instruccion: 1) <primer objetivo>; 2) <segundo objetivo>. Hoy puedo procesar uno por vez. Reescribi eligiendo solo uno (por ejemplo: 'ajusta inventario a USD 205k en mayo 2026').\"}.\n"
        "B) OBJETIVO GENUINAMENTE NO SOPORTADO: solo si el pedido no encaja en ningun intent ni plan listado arriba. Ejemplos: optimizar multiples variables simultaneas (revenue + cogs + gastos juntos), mezclar restricciones con targets en una sola instruccion ('no negativa Y promedio 200k Y total anual X'), mas de 4 cuentas en un mismo plan, planes que cruzan periodos. ANTES de usar B verifica que ningun intent ni plan aplica; en duda elegi un plan_*. Para casos verdaderamente no soportados devolve {\"intent\": \"question\", \"assistant_message\": \"Entendi que queres <intencion>. LIMITACION: ese pedido combina restricciones que hoy no resuelvo en una sola operacion. Sugerencia: dividilo en pedidos mas chicos (ej: primero 'ajusta inventario para que promedio sea USD 200k', despues 'que caja no quede negativa').\"}. NO listes objetivos numerados (es uno solo).\n"
        "C) REFERENCIAL: el usuario responde con frases cortas como 'el primero', 'ese', 'uno', '1', '2', 'si', 'no', 'hazlo', 'aplica el primer ajuste', 'dale'. Son respuestas a preguntas previas, no instrucciones nuevas. Devolve {\"intent\": \"question\", \"assistant_message\": \"Reescribi la instruccion completa con cuenta, mes y monto (ej: 'ajusta inventario a USD 205k en mayo 2026').\"}.\n"
        "Antes de elegir A/B/C: si la instruccion ES un ajuste puntual de UN mes con cuenta y monto, usa target_balance_adjustment normalmente, no estas reglas."
    )


def _user_prompt(*, message: str, ui_context: Mapping[str, Any]) -> str:
    ctx = dict(ui_context or {})
    period_hint = ""
    period_meta = ctx.get("period") if isinstance(ctx.get("period"), Mapping) else {}
    start = str(period_meta.get("start_month") or period_meta.get("mes_inicial") or "").strip()
    end = str(period_meta.get("end_month") or period_meta.get("mes_final") or "").strip()
    if start and end:
        period_hint = (
            f"\nRango del periodo activo: {start} a {end} (formato YYYY-MM). "
            "Cualquier mes fuera de ese rango es invalido y debe rechazarse.\n"
        )
    return (
        "Mensaje del usuario:\n"
        f"{message}\n\n"
        "Contexto UI disponible:\n"
        f"{ctx}\n"
        f"{period_hint}\n"
        "Ejemplos de cuentas validas: Efectivo y Equivalentes de Efectivo, "
        "Resultados Acumulados, Resultados del Ejercicio, Inventarios, Proveedores."
    )


# ============================================================
# Propuestas y vouchers
# ============================================================

def _proposal_payload(
    *,
    kind: str,
    title: str,
    assistant_message: str,
    month: str | None,
    rows: list[dict[str, Any]],
    technical_records: list[dict[str, Any]],
    original_message: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = {
        "kind": kind,
        "title": title,
        "assistant_message": assistant_message,
        "month": month,
        "journal_rows": rows,
        "technical_records": technical_records,
        "original_message": original_message,
    }
    if extra:
        data.update(extra)
    return data


def _append_saved_voucher(payload: Mapping[str, Any], voucher: Mapping[str, Any]) -> dict[str, Any]:
    projected = deepcopy(dict(payload or {}))
    accounting = dict(projected.get("accounting") or {})
    vouchers = list(accounting.get("vouchers") or [])
    vouchers.append(dict(voucher))
    accounting["vouchers"] = vouchers
    projected["accounting"] = accounting
    return projected


def _saved_vouchers(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    accounting = dict((payload or {}).get("accounting") or {})
    return [dict(voucher) for voucher in accounting.get("vouchers") or [] if isinstance(voucher, Mapping)]


def _voucher_rows(voucher: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "account": line.get("account"),
            "debit": line.get("debit") or 0,
            "credit": line.get("credit") or 0,
            "reference": line.get("reference") or line.get("ref") or "",
        }
        for line in voucher.get("lines") or []
    ]


# ============================================================
# Correcciones y reversos
# ============================================================

def _has_correction_payload(args: Mapping[str, Any]) -> bool:
    correction = args.get("correction") if isinstance(args.get("correction"), Mapping) else args
    lines = correction.get("lines") if isinstance(correction, Mapping) else None
    return bool(isinstance(lines, list) and lines)


def _resolve_correctable_voucher(
    payload: Mapping[str, Any],
    all_vouchers: list[Mapping[str, Any]],
    persisted_vouchers: list[Mapping[str, Any]],
    voucher_id: str,
) -> tuple[dict[str, Any], str]:
    target = str(voucher_id or "").upper()
    persisted = next((dict(voucher) for voucher in persisted_vouchers if str(voucher.get("voucher_id") or "").upper() == target), None)
    if persisted:
        return persisted, "persisted"
    generated = next((dict(voucher) for voucher in all_vouchers if str(voucher.get("voucher_id") or "").upper() == target), None)
    if not generated:
        raise AgentValidationError(f"No encontre el comprobante {voucher_id}.")
    instruction_id = str(generated.get("instruction_id") or "").strip()
    entries = dict((payload or {}).get("movements") or {}).get("journal_entries") or []
    if instruction_id and any(str(entry.get("instruction_id") or "").strip() == instruction_id for entry in entries if isinstance(entry, Mapping)):
        return generated, "journal_entry"
    raise AgentValidationError("Ese comprobante es generado automaticamente; en esta fase solo puedo corregir comprobantes guardados o partidas del chat.")


def _find_reversal_for(vouchers: list[Mapping[str, Any]], original_voucher_id: str) -> str:
    original = str(original_voucher_id or "").upper()
    for voucher in vouchers:
        if str(voucher.get("type") or "").lower() != "reversal":
            continue
        if str(voucher.get("reference_voucher_id") or "").upper() == original:
            return str(voucher.get("voucher_id") or "")
    return ""


def _unique_reversal_id(original_voucher_id: str, vouchers: list[Mapping[str, Any]]) -> str:
    existing = {str(voucher.get("voucher_id") or "").upper() for voucher in vouchers}
    original = str(original_voucher_id or "").upper()
    for _ in range(20):
        candidate = f"REV-{original}-{uuid.uuid4().hex[:6].upper()}"
        if candidate.upper() not in existing:
            return candidate
    return f"REV-{original}-{uuid.uuid4().hex[:12].upper()}"


def _journal_entry_from_reversal_voucher(voucher: Mapping[str, Any], *, command_id: str, original_message: str) -> dict[str, Any]:
    return {
        "entry_id": f"JE-{uuid.uuid4().hex[:8].upper()}",
        "voucher_id": str(voucher.get("voucher_id") or ""),
        "reference_voucher_id": str(voucher.get("reference_voucher_id") or ""),
        "month": str(voucher.get("month") or "")[:7],
        "description": str(voucher.get("description") or ""),
        "lines": [
            {
                "account": line.get("account"),
                "debit": line.get("debit") or 0,
                "credit": line.get("credit") or 0,
                "reference": line.get("reference") or "",
            }
            for line in voucher.get("lines") or []
            if isinstance(line, Mapping)
        ],
        "currency": "nio",
        "entry_type": "voucher_reversal",
        "source": "chat_financiero",
        "instruction_id": command_id,
        "locked": True,
        "message": original_message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# Payload manipulation
# ============================================================

def _append_journal_entry(payload: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    projected = deepcopy(dict(payload or {}))
    movements = dict(projected.get("movements") or {})
    entries = list(movements.get("journal_entries") or [])
    entries.append(dict(entry))
    movements["journal_entries"] = entries
    projected["movements"] = movements
    return projected


def _append_dynamic_account(payload: Mapping[str, Any], account: Mapping[str, Any]) -> dict[str, Any]:
    projected = deepcopy(dict(payload or {}))
    accounting = dict(projected.get("accounting") or {})
    accounts = list(accounting.get("dynamic_accounts") or [])
    clean = {
        "code": str(account.get("code") or "").strip(),
        "niif_code": str(account.get("niif_code") or "").strip(),
        "name": str(account.get("name") or "").strip(),
        "account_type": _normalize_account_type(account.get("account_type")),
        "section": _normalize_account_section(account.get("section")),
        "normal_balance": str(account.get("normal_balance") or "").strip(),
        "parent_code": str(account.get("parent_code") or "").strip(),
        "aliases": list(account.get("aliases") or []),
        "is_postable": bool(account.get("is_postable", True)),
        "source": str(account.get("source") or "chat_financiero").strip(),
    }
    existing_codes = {str(item.get("code") or "").strip() for item in accounts if isinstance(item, Mapping)}
    existing_names = {str(item.get("name") or "").strip().lower() for item in accounts if isinstance(item, Mapping)}
    if clean["code"] and clean["name"].lower() not in existing_names and clean["code"] not in existing_codes:
        accounts.append(clean)
    accounting["dynamic_accounts"] = accounts
    projected["accounting"] = accounting
    return projected


def _dynamic_account_lookup(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    accounting = dict((payload or {}).get("accounting") or {})
    lookup: dict[str, dict[str, Any]] = {}
    for item in accounting.get("dynamic_accounts") or []:
        if not isinstance(item, Mapping):
            continue
        account = dict(item)
        for value in [account.get("code"), account.get("name")]:
            key = str(value or "").strip()
            if key:
                lookup[key] = account
    return lookup


def _account_catalog_payload(account: Any) -> dict[str, Any]:
    try:
        aliases = json.loads(account.aliases_json or "[]")
    except Exception:
        aliases = []
    return {
        "code": account.code,
        "niif_code": account.niif_code,
        "name": account.name,
        "account_type": account.account_type,
        "section": account.section,
        "normal_balance": account.normal_balance,
        "parent_code": account.parent_code,
        "aliases": aliases if isinstance(aliases, list) else [],
        "is_postable": bool(getattr(account, "is_postable", 1)),
        "source": account.source,
    }


# ============================================================
# Journal validation utilities
# ============================================================

def _journal_totals(lines: list[Mapping[str, Any]]) -> dict[str, Any]:
    debit = round(sum(float(line.get("debit") or 0) for line in lines), 2)
    credit = round(sum(float(line.get("credit") or 0) for line in lines), 2)
    return {"debit": debit, "credit": credit, "balanced": abs(debit - credit) <= 0.01}


def _journal_pair(lines: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    debit_lines = [line for line in lines if float(line.get("debit") or 0) > 0]
    credit_lines = [line for line in lines if float(line.get("credit") or 0) > 0]
    if len(debit_lines) != 1 or len(credit_lines) != 1:
        return None
    debit_amount = round(float(debit_lines[0].get("debit") or 0), 2)
    credit_amount = round(float(credit_lines[0].get("credit") or 0), 2)
    if abs(debit_amount - credit_amount) > 0.01:
        return None
    return {
        "debit_account": debit_lines[0].get("account"),
        "credit_account": credit_lines[0].get("account"),
        "amount": debit_amount,
    }


def _valid_account_suggestions(payload: Mapping[str, Any]) -> list[str]:
    labels = list(LEDGER_ACCOUNT_LABELS.values())
    dynamic = _dynamic_account_lookup(payload)
    for account in dynamic.values():
        name = str(account.get("name") or "").strip()
        if name and name not in labels:
            labels.append(name)
    return sorted(labels)


# ============================================================
# Numerica y normalizacion
# ============================================================

def _to_float(value: Any) -> float | None:
    try:
        if value in {None, ""}:
            return None
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def _account_code(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    plain = "".join(ch for ch in raw if not unicodedata.combining(ch)).lower()
    code = re.sub(r"[^a-z0-9]+", "_", plain).strip("_")
    return code[:100] or f"cuenta_{uuid.uuid4().hex[:8]}"


def _normalize_account_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "asset": "activo",
        "activo": "activo",
        "liability": "pasivo",
        "pasivo": "pasivo",
        "equity": "patrimonio",
        "patrimonio": "patrimonio",
        "revenue": "ingreso",
        "ingreso": "ingreso",
        "income": "ingreso",
        "expense": "gasto",
        "gasto": "gasto",
        "cost": "costo",
        "costo": "costo",
    }
    return aliases.get(raw, raw)


def _normalize_account_section(value: Any) -> str:
    raw = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "current": "corriente",
        "corriente": "corriente",
        "non_current": "no_corriente",
        "nocorriente": "no_corriente",
        "no_corriente": "no_corriente",
        "patrimonio": "patrimonio",
        "equity": "patrimonio",
        "ingresos": "ingresos",
        "revenue": "ingresos",
        "costo_ventas": "costo_ventas",
        "costo_de_ventas": "costo_ventas",
        "gastos_operativos": "gastos_operativos",
        "gasto_operativo": "gastos_operativos",
        "gastos_financieros": "gastos_financieros",
        "gasto_financiero": "gastos_financieros",
    }
    return aliases.get(raw, raw)


def _valid_type_section(account_type: str, section: str) -> bool:
    allowed = {
        "activo": {"corriente", "no_corriente"},
        "pasivo": {"corriente", "no_corriente"},
        "patrimonio": {"patrimonio"},
        "ingreso": {"ingresos"},
        "costo": {"costo_ventas"},
        "gasto": {"gastos_operativos", "gastos_financieros"},
    }
    return section in allowed.get(account_type, set())


# ============================================================
# Mes/periodo
# ============================================================

def _last_payload_month(payload: Mapping[str, Any]) -> str:
    period = dict(payload.get("period") or {})
    return str(period.get("end_month") or "")[:7]


def _payload_months(payload: Mapping[str, Any]) -> list[str]:
    period = dict(payload.get("period") or {})
    start = str(period.get("start_month") or "")[:7]
    end = str(period.get("end_month") or "")[:7]
    if start and end:
        return _months_between(start, end)
    try:
        result = build_financial_model(payload)
        return [str(m) for m in (result.summary.get("all_months") or result.summary.get("months") or [])]
    except Exception:
        return []


def _previous_year_end(month: str) -> str:
    year = int(str(month or "0000-01")[:4] or 0)
    return f"{year - 1}-12"


def _months_between(start: str, end: str) -> list[str]:
    import pandas as pd

    try:
        return [d.strftime("%Y-%m") for d in pd.period_range(start=start[:7], end=end[:7], freq="M")]
    except Exception:
        return []


# ============================================================
# Statement queries
# ============================================================

def _statement_value(result, description: str, month: str) -> float:
    df = result.df_esf_mensual_full
    col = _month_column(df, month)
    if not month or col is None:
        return 0.0
    rows = df[df["Descripcion"] == description]
    if rows.empty:
        return 0.0
    try:
        return float(rows.iloc[0][col] or 0)
    except Exception:
        return 0.0


def _month_column(df, month: str):
    if month in df.columns:
        return month
    for col in df.columns:
        if str(col).startswith(month):
            return col
    return None


def _er_value(result, description: str) -> float:
    df = result.df_er_full
    if df is None or df.empty:
        return 0.0
    rows = df[df["Descripcion"] == description]
    if rows.empty:
        return 0.0
    accum_cols = [col for col in df.columns if str(col).startswith("Acumulado")]
    col = accum_cols[0] if accum_cols else df.columns[-2]
    try:
        return float(rows.iloc[0][col] or 0)
    except Exception:
        return 0.0


# ============================================================
# Impacto y titulos
# ============================================================

def _impact_deltas(impact: Mapping[str, Any]) -> dict[str, float]:
    out = {"cash_delta": 0.0, "assets_delta": 0.0, "liabilities_delta": 0.0, "equity_delta": 0.0, "income_delta": 0.0}
    mapping = {
        "caja": "cash_delta",
        "activos": "assets_delta",
        "pasivos": "liabilities_delta",
        "patrimonio": "equity_delta",
        "resultado": "income_delta",
    }
    for item in impact.get("items") or []:
        key = mapping.get(str(item.get("key") or ""))
        if key:
            out[key] = round(float(item.get("delta") or 0), 2)
    return out


def _journal_title(intent: str) -> str:
    if intent == "year_close_transfer":
        return "Cierre de resultados a acumulados"
    if intent == "account_transfer":
        return "Reclasificacion contable"
    return "Partida doble"


# ============================================================
# Assumption change
# ============================================================

ASSUMPTION_FIELD_MAP = {
    "cost_pct": ("income", "cost_pct", "pct"),
    "porcentaje_costo": ("income", "cost_pct", "pct"),
    "ingresos_base_usd": ("income", "base_income_usd", "positive"),
    "base_income_usd": ("income", "base_income_usd", "positive"),
    "income_base_usd": ("income", "base_income_usd", "positive"),
    "variabilidad_ingresos_pct": ("income", "income_variability_pct", "pct"),
    "income_variability_pct": ("income", "income_variability_pct", "pct"),
    "variabilidad_costos_pct": ("income", "cost_variability_pct", "pct"),
    "cost_variability_pct": ("income", "cost_variability_pct", "pct"),
    "cash_sales_pct": ("income", "cash_sales_pct", "pct"),
    "porcentaje_contado": ("income", "cash_sales_pct", "pct"),
    "seed": ("period", "seed", "string"),
}


def _apply_cost_assumption(
    payload: Mapping[str, Any],
    *,
    months: list[str],
    cost_pct: float,
    cost_variability_pct: float | None,
    global_scope: bool,
) -> dict[str, Any]:
    projected = deepcopy(dict(payload or {}))
    income = dict(projected.get("income") or {})
    if global_scope:
        income["cost_pct"] = cost_pct
        if cost_variability_pct is not None:
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
            if cost_variability_pct is not None:
                record["cost_variability_pct"] = cost_variability_pct
            overrides[month] = record
        income["monthly_overrides"] = _income_overrides_list(overrides)
    projected["income"] = income
    return projected


def _normalize_assumption_field(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = raw.replace(" ", "_").replace("-", "_")
    aliases = {
        "costo": "cost_pct",
        "costo_venta": "cost_pct",
        "costo_de_venta": "cost_pct",
        "ventas_contado": "cash_sales_pct",
        "contado": "cash_sales_pct",
        "ingresos": "ingresos_base_usd",
        "ingreso_base": "ingresos_base_usd",
        "variabilidad_ingresos": "variabilidad_ingresos_pct",
        "variabilidad_costos": "variabilidad_costos_pct",
        "semilla": "seed",
    }
    raw = aliases.get(raw, raw)
    if raw not in ASSUMPTION_FIELD_MAP:
        allowed = ", ".join(sorted({"cost_pct", "ingresos_base_usd", "variabilidad_ingresos_pct", "variabilidad_costos_pct", "cash_sales_pct", "seed"}))
        raise AgentValidationError(f"Supuesto no permitido. Campos permitidos: {allowed}.")
    # Use public Spanish key for ingreso base so proposal text matches app language.
    if raw == "base_income_usd":
        return "ingresos_base_usd"
    if raw == "income_variability_pct":
        return "variabilidad_ingresos_pct"
    if raw == "cost_variability_pct":
        return "variabilidad_costos_pct"
    return raw


def _normalize_assumption_value(kind: str, value: Any) -> Any:
    if kind == "string":
        out = str(value or "").strip()
        if not out:
            raise AgentValidationError("El valor de seed no puede estar vacio.")
        return out
    number = _to_float(value)
    if number is None:
        raise AgentValidationError("Indique un valor valido para el supuesto.")
    if kind == "positive":
        if number <= 0:
            raise AgentValidationError("El ingreso base USD debe ser mayor que cero.")
        return float(number)
    if kind == "pct":
        if 0 <= number <= 1:
            number *= 100.0
        if number < 0 or number > 100:
            raise AgentValidationError("El porcentaje debe estar entre 0 y 100%.")
        return float(number)
    return number


def _apply_assumption_change(payload: Mapping[str, Any], *, field: str, value: Any) -> tuple[dict[str, Any], Any, Any]:
    target, key, kind = ASSUMPTION_FIELD_MAP[field]
    after_value = _normalize_assumption_value(kind, value)
    projected = deepcopy(dict(payload or {}))
    section = dict(projected.get(target) or {})
    before_value = section.get(key)
    section[key] = after_value
    projected[target] = section
    return projected, before_value, after_value


def _income_overrides_by_month(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, Mapping):
        raw = [dict({"month": k}, **dict(v or {})) for k, v in raw.items()]
    for item in raw or []:
        if isinstance(item, Mapping) and item.get("month"):
            out[str(item["month"])[:7]] = dict(item)
    return out


def _income_overrides_list(overrides: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for month in sorted(overrides):
        record = dict(overrides[month])
        record["month"] = month
        if any(k != "month" for k in record):
            out.append(record)
    return out


# ============================================================
# Periodo persistencia
# ============================================================

def _sync_period_fields(periodo, payload: Mapping[str, Any]) -> None:
    period = dict(payload.get("period") or {})
    income = dict(payload.get("income") or {})
    if period.get("start_month"):
        periodo.mes_inicial = str(period["start_month"])[:7]
    if period.get("end_month"):
        periodo.mes_final = str(period["end_month"])[:7]
    if period.get("exchange_rate") is not None:
        periodo.tasa_cambio = float(period["exchange_rate"])
    periodo.seed = str(period.get("seed") or periodo.seed or "")
    for attr, key in [
        ("ingresos_base_usd", "base_income_usd"),
        ("variabilidad_ingresos_pct", "income_variability_pct"),
        ("cost_pct", "cost_pct"),
        ("variabilidad_costos_pct", "cost_variability_pct"),
        ("cash_sales_pct", "cash_sales_pct"),
    ]:
        if income.get(key) is not None:
            setattr(periodo, attr, float(income[key]))


def _periodo_snapshot(periodo) -> dict[str, Any]:
    return {
        "id": periodo.id,
        "estado": periodo.estado,
        "mes_inicial": periodo.mes_inicial,
        "mes_final": periodo.mes_final,
        "payload_hash": stable_hash(parse_json_object(periodo.payload_json)),
    }


# ============================================================
# Misc
# ============================================================

def _elapsed_seconds(started_at: float) -> float:
    return max(0.0, time.monotonic() - started_at)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")

_SPANISH_MONTH_NAMES = {
    "enero": 1, "ene": 1,
    "febrero": 2, "feb": 2,
    "marzo": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "mayo": 5, "may": 5,
    "junio": 6, "jun": 6,
    "julio": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "septiembre": 9, "setiembre": 9, "sep": 9, "set": 9,
    "octubre": 10, "oct": 10,
    "noviembre": 11, "nov": 11,
    "diciembre": 12, "dic": 12,
}

_SPANISH_MONTH_RE = re.compile(
    r"\b(" + "|".join(_SPANISH_MONTH_NAMES.keys()) + r")\b[^\d]{0,4}(\d{4})",
    re.IGNORECASE,
)


def _normalize_month_value(value: Any) -> str:
    """Normaliza meses a formato YYYY-MM aceptando entrada natural en espanol.

    Acepta: '2026-05', '2026-5', '2026-05-15', 'mayo 2026', 'May 2026',
    '2026/05', etc. Devuelve '' si no se puede interpretar.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if _MONTH_PATTERN.match(text[:7]):
        return text[:7]
    candidate = text.replace("/", "-").replace(".", "-")
    if _MONTH_PATTERN.match(candidate[:7]):
        return candidate[:7]
    parts = candidate.split("-")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        year = int(parts[0])
        month = int(parts[1])
        if 1900 <= year <= 2200 and 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    match = _SPANISH_MONTH_RE.search(text.lower())
    if match:
        month = _SPANISH_MONTH_NAMES[match.group(1)]
        year = int(match.group(2))
        return f"{year:04d}-{month:02d}"
    return ""


def _extract_target_month(proposal_payload: Mapping[str, Any], args: Mapping[str, Any]) -> str | None:
    month_field = str(proposal_payload.get("month") or "").strip()[:7]
    if _MONTH_PATTERN.match(month_field):
        return month_field
    for key in ("target_month", "month", "source_month"):
        val = str((args or {}).get(key) or "").strip()[:7]
        if _MONTH_PATTERN.match(val):
            return val
    records = proposal_payload.get("technical_records") or []
    if records and isinstance(records[0], Mapping):
        record_month = str(records[0].get("month") or "").strip()[:7]
        if _MONTH_PATTERN.match(record_month):
            return record_month
    return None

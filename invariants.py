"""Invariantes contables verificables (Fase 1 del plan de mejora).

I3 — Conciliacion mayor vs ESF: el saldo final por cuenta del libro mayor
(accounting_model) debe coincidir, mes a mes, con la fila correspondiente
del ESF mensual (financial_model). Ambos son derivaciones paralelas de los
mismos datos mensuales; una diferencia indica un comprobante faltante o
sobrante en el mayor, o un bug en uno de los dos motores.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

import pandas as pd

from accounting_accounts import LEDGER_ACCOUNT_LABELS


# Cuentas de balance a conciliar (claves del catalogo unico). Las cuentas de
# resultado (ingresos, costos, gastos) se cierran mensualmente contra
# Resultados del Ejercicio, por lo que su saldo del mayor es cero al corte y
# no tienen fila propia en el ESF.
_BALANCE_KEYS = [
    "cash",
    "accounts_receivable",
    "inventory",
    "ppe_real_estate",
    "ppe_equipment",
    "ppe_vehicles",
    "accum_depreciation",
    "credit_cards",
    "suppliers",
    "taxes_payable",
    "accrued_expenses",
    "loans_mortgage",
    "loans_consumo",
    "loans_personal",
    "loans_pledge",
    "loans_commercial",
    "capital",
    "retained_earnings",
    "current_earnings",
]


def _num(value: Any) -> float:
    try:
        number = pd.to_numeric(value, errors="coerce")
        return 0.0 if pd.isna(number) else float(number)
    except Exception:
        return 0.0


def _esf_row_spec(key: str) -> tuple[str, int]:
    """Fila del ESF y signo para comparar contra el saldo normal del mayor.

    La depreciacion acumulada se muestra negativa en el ESF, pero su saldo
    normal en el mayor es acreedor (positivo).
    """
    if key == "accum_depreciation":
        return "(-) Depreciacion Acumulada", -1
    return LEDGER_ACCOUNT_LABELS[key], 1


def validate_ledger_vs_esf(
    accounting: Mapping[str, Any],
    df_esf: pd.DataFrame,
    months: List[str],
    *,
    tolerance: float = 1.0,
) -> Dict[str, Any]:
    """Concilia el saldo de cierre del mayor contra el ESF mensual.

    Devuelve {"ok", "errors": [{account, month, ledger, esf, difference}],
    "checks": [...]} con tolerancia de redondeo en cordobas.
    """
    checks: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    if df_esf is None or df_esf.empty or not months:
        return {
            "ok": False,
            "errors": [{"account": "", "month": "", "ledger": 0.0, "esf": 0.0, "difference": 0.0,
                        "message": "ESF vacio para conciliar contra el mayor"}],
            "checks": checks,
        }

    trace = dict(accounting.get("trace") or {})
    desc_col = df_esf.columns[0]
    descriptions = df_esf[desc_col].astype(str).str.strip()

    col_by_month: Dict[str, Any] = {}
    for col in df_esf.columns[1:]:
        text = col.strftime("%Y-%m") if hasattr(col, "strftime") else str(col)[:7]
        col_by_month[text] = col

    for key in _BALANCE_KEYS:
        ledger_label = LEDGER_ACCOUNT_LABELS[key]
        esf_label, sign = _esf_row_spec(key)
        rows = df_esf[descriptions == esf_label]
        if rows.empty:
            continue
        row = rows.iloc[0]
        ledger_closing = 0.0
        for raw_month in months:
            month = str(raw_month)[:7]
            col = col_by_month.get(month)
            if col is None:
                continue
            esf_value = _num(row[col]) * sign
            month_trace = trace.get(f"{ledger_label}|{month}")
            if month_trace is not None:
                ledger_closing = _num(month_trace.get("closing_balance"))
            difference = round(ledger_closing - esf_value, 2)
            passed = abs(difference) <= tolerance
            checks.append({
                "rule": "I3: saldo mayor = fila ESF",
                "account": ledger_label,
                "month": month,
                "ledger": round(ledger_closing, 2),
                "esf": round(esf_value, 2),
                "ok": passed,
            })
            if not passed:
                errors.append({
                    "account": ledger_label,
                    "month": month,
                    "ledger": round(ledger_closing, 2),
                    "esf": round(esf_value, 2),
                    "difference": difference,
                })

    return {"ok": not errors, "errors": errors, "checks": checks}

from __future__ import annotations

import calendar
import random
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

import pandas as pd

from validators import validate_er, validate_esf


DEFAULT_EXCHANGE_RATE = 36.6243

DEFAULT_EXPENSES_USD: Dict[str, float] = {
    "Sueldos y Salarios": 2700.0,
    "Servicios Publicos": 600.0,
    "Alcaldia y DGI": 50.0,
    "Combustible": 500.0,
    "Publicidad": 1500.0,
    "Mantenimientos": 0.0,
    "Renta": 440.0,
    "Seguros": 0.0,
    "Otros Gastos": 350.0,
}

EXPENSE_ORDER = [
    "Sueldos y Salarios",
    "Servicios Publicos",
    "Alcaldia y DGI",
    "Combustible",
    "Publicidad",
    "Gastos Financieros",
    "Mantenimientos",
    "Renta",
    "Gasto por Depreciacion",
    "Seguros",
    "Otros Gastos",
]

DEFAULT_BALANCES_NIO: Dict[str, float] = {
    "cash": 410_193.0,
    "accounts_receivable": 62_261.0,
    "inventory": 5_310_538.0,
    "ppe_real_estate": 0.0,
    "ppe_equipment": 549_366.0,
    "ppe_vehicles": 0.0,
    "accum_depreciation": -68_676.0,
    "credit_cards": 183_122.0,
    "suppliers": 0.0,
    "taxes_payable": 0.0,
    "accrued_expenses": 0.0,
    "loans_mortgage": 0.0,
    "loans_consumo": 0.0,
    "loans_personal": 47_612.0,
    "loans_pledge": 0.0,
    "loans_commercial": 0.0,
    "retained_earnings": 3_424_750.0,
}


@dataclass
class FinancialModelResult:
    df_certificacion: pd.DataFrame
    df_er: pd.DataFrame
    df_movimientos: pd.DataFrame
    df_esf_mensual: pd.DataFrame
    df_datos: pd.DataFrame
    validations: Dict[str, Any]
    summary: Dict[str, Any]
    metadata: Dict[str, Any]


def build_financial_model(payload: Mapping[str, Any]) -> FinancialModelResult:
    """Build the financial statements that used to be prepared in Excel."""
    payload = dict(payload or {})
    period = dict(payload.get("period") or {})
    income = dict(payload.get("income") or {})
    expenses_payload = dict(payload.get("expenses") or {})
    balances_payload = dict(payload.get("balances") or {})
    movement_payload = dict(payload.get("movements") or {})

    months = _build_months(
        end_month=str(period.get("end_month") or period.get("mes_final") or ""),
        count=_to_int(period.get("months") or period.get("cantidad_meses"), 6),
    )
    seed = str(period.get("seed") or period.get("semilla") or _default_seed(months, payload))
    rng = random.Random(seed)

    exchange_rate = _to_float(period.get("exchange_rate") or period.get("tasa_cambio"), DEFAULT_EXCHANGE_RATE)
    base_income_usd = _to_float(income.get("base_income_usd") or income.get("ingresos_base_usd"), 100_000.0)
    income_variability = _pct(income.get("income_variability_pct") or income.get("variabilidad_ingresos_pct"), 15.0)
    cost_pct = _pct(income.get("cost_pct") or income.get("porcentaje_costo"), 70.0)
    cost_variability = _pct(income.get("cost_variability_pct") or income.get("variabilidad_costo_pct"), 5.0)
    cash_sales_pct = _pct(income.get("cash_sales_pct") or income.get("porcentaje_contado"), 85.0)
    credit_sales_pct = max(0.0, min(1.0, 1.0 - cash_sales_pct))

    purchase_base_usd = _to_float(
        movement_payload.get("purchase_base_usd") or movement_payload.get("compras_base_usd"),
        base_income_usd,
    )
    purchase_variability = _pct(
        movement_payload.get("purchase_variability_pct") or movement_payload.get("variabilidad_compras_pct"),
        10.0,
    )
    loan_interest_monthly_pct = _pct(
        movement_payload.get("loan_interest_monthly_pct") or movement_payload.get("interes_mensual_creditos_pct"),
        0.0,
    )

    balances = {**DEFAULT_BALANCES_NIO}
    for key, value in balances_payload.items():
        if key in balances:
            balances[key] = _to_float(value, balances[key])

    expenses_usd = {**DEFAULT_EXPENSES_USD}
    for key, value in expenses_payload.items():
        normalized = _match_key(key, expenses_usd.keys())
        if normalized:
            expenses_usd[normalized] = _to_float(value, expenses_usd[normalized])

    events = _index_events(movement_payload.get("events") or payload.get("events") or [], months, exchange_rate)

    revenue_factors: List[float] = []
    cost_rates: List[float] = []
    purchase_factors: List[float] = []
    for _ in months:
        revenue_factors.append(1.0 + rng.uniform(-income_variability, income_variability))
        cost_rates.append(max(0.0, min(1.0, cost_pct + rng.uniform(-cost_variability, cost_variability))))
        purchase_factors.append(1.0 + rng.uniform(-purchase_variability, purchase_variability))

    monthly: List[Dict[str, float]] = []
    state = dict(balances)
    result_accum = 0.0

    for idx, month in enumerate(months):
        key = _month_key(month)
        month_events = events.get(key, {})

        revenue = _round(base_income_usd * exchange_rate * revenue_factors[idx])
        cash_sales = _round(revenue * cash_sales_pct)
        credit_sales = _round(revenue * credit_sales_pct)
        cogs = _round(revenue * cost_rates[idx])
        gross_profit = revenue - cogs
        purchases = _round(purchase_base_usd * exchange_rate * purchase_factors[idx])

        additions_real_estate = month_events.get("asset_real_estate", 0.0)
        additions_equipment = month_events.get("asset_equipment", 0.0)
        additions_vehicles = month_events.get("asset_vehicle", 0.0)
        state["ppe_real_estate"] += additions_real_estate - month_events.get("asset_real_estate_sale", 0.0)
        state["ppe_equipment"] += additions_equipment - month_events.get("asset_equipment_sale", 0.0)
        state["ppe_vehicles"] += additions_vehicles - month_events.get("asset_vehicle_sale", 0.0)

        depreciation = _round(
            max(state["ppe_real_estate"], 0.0) / (40 * 12)
            + max(state["ppe_equipment"], 0.0) / (8 * 12)
            + max(state["ppe_vehicles"], 0.0) / (5 * 12)
        )

        new_loans = {
            "loans_mortgage": month_events.get("loan_mortgage_new", 0.0),
            "loans_consumo": month_events.get("loan_consumo_new", 0.0),
            "loans_personal": month_events.get("loan_personal_new", 0.0),
            "loans_pledge": month_events.get("loan_pledge_new", 0.0),
            "loans_commercial": month_events.get("loan_commercial_new", 0.0),
        }
        loan_payments = {
            "loans_mortgage": month_events.get("loan_mortgage_payment", 0.0),
            "loans_consumo": month_events.get("loan_consumo_payment", 0.0),
            "loans_personal": month_events.get("loan_personal_payment", 0.0),
            "loans_pledge": month_events.get("loan_pledge_payment", 0.0),
            "loans_commercial": month_events.get("loan_commercial_payment", 0.0),
        }
        financial_expense = _round(sum(max(state[k], 0.0) * loan_interest_monthly_pct for k in new_loans))

        fixed_expenses = {
            label: _round(amount * exchange_rate)
            for label, amount in expenses_usd.items()
        }
        expenses_nio = {
            "Sueldos y Salarios": fixed_expenses.get("Sueldos y Salarios", 0.0),
            "Servicios Publicos": fixed_expenses.get("Servicios Publicos", 0.0),
            "Alcaldia y DGI": fixed_expenses.get("Alcaldia y DGI", 0.0),
            "Combustible": fixed_expenses.get("Combustible", 0.0),
            "Publicidad": fixed_expenses.get("Publicidad", 0.0),
            "Gastos Financieros": financial_expense,
            "Mantenimientos": fixed_expenses.get("Mantenimientos", 0.0),
            "Renta": fixed_expenses.get("Renta", 0.0),
            "Gasto por Depreciacion": depreciation,
            "Seguros": fixed_expenses.get("Seguros", 0.0),
            "Otros Gastos": fixed_expenses.get("Otros Gastos", 0.0),
        }
        total_expenses = _round(sum(expenses_nio.values()))
        net_income = gross_profit - total_expenses
        result_accum += net_income

        collections = state["accounts_receivable"]
        state["accounts_receivable"] = _round(state["accounts_receivable"] + credit_sales - collections)
        state["inventory"] = _round(state["inventory"] + purchases - cogs)

        credit_card_new = month_events.get("credit_card_new", 0.0)
        credit_card_payment = month_events.get("credit_card_payment", state["credit_cards"] + credit_card_new)
        supplier_new = month_events.get("supplier_new", 0.0)
        supplier_payment = month_events.get("supplier_payment", 0.0)
        taxes_new = month_events.get("taxes_new", 0.0)
        taxes_payment = month_events.get("taxes_payment", 0.0)
        accrued_new = month_events.get("accrued_new", 0.0)
        accrued_payment = month_events.get("accrued_payment", 0.0)

        state["credit_cards"] = max(0.0, _round(state["credit_cards"] + credit_card_new - credit_card_payment))
        state["suppliers"] = max(0.0, _round(state["suppliers"] + supplier_new - supplier_payment))
        state["taxes_payable"] = max(0.0, _round(state["taxes_payable"] + taxes_new - taxes_payment))
        state["accrued_expenses"] = max(0.0, _round(state["accrued_expenses"] + accrued_new - accrued_payment))

        for loan_key, new_amount in new_loans.items():
            state[loan_key] = max(0.0, _round(state[loan_key] + new_amount - loan_payments[loan_key]))

        state["accum_depreciation"] = _round(state["accum_depreciation"] - depreciation)

        cash_operating_expenses = total_expenses - depreciation
        principal_payments = sum(loan_payments.values())
        new_credit_cash = sum(new_loans.values())
        asset_purchases = additions_real_estate + additions_equipment + additions_vehicles
        owner_withdrawal = month_events.get("owner_withdrawal", 0.0)
        capital_contribution = month_events.get("capital_contribution", 0.0)
        sale_ppe = (
            month_events.get("asset_real_estate_sale", 0.0)
            + month_events.get("asset_equipment_sale", 0.0)
            + month_events.get("asset_vehicle_sale", 0.0)
        )

        state["cash"] = _round(
            state["cash"]
            + cash_sales
            + collections
            + new_credit_cash
            + sale_ppe
            + capital_contribution
            - cash_operating_expenses
            - purchases
            - credit_card_payment
            - supplier_payment
            - taxes_payment
            - accrued_payment
            - principal_payments
            - asset_purchases
            - owner_withdrawal
        )

        current_assets = state["cash"] + state["accounts_receivable"] + state["inventory"]
        non_current_assets = (
            state["ppe_real_estate"]
            + state["ppe_equipment"]
            + state["ppe_vehicles"]
            + state["accum_depreciation"]
        )
        total_assets = current_assets + non_current_assets
        current_liabilities = state["credit_cards"] + state["suppliers"] + state["taxes_payable"] + state["accrued_expenses"]
        non_current_liabilities = (
            state["loans_mortgage"]
            + state["loans_consumo"]
            + state["loans_personal"]
            + state["loans_pledge"]
            + state["loans_commercial"]
        )
        total_liabilities = current_liabilities + non_current_liabilities
        retained = balances["retained_earnings"]
        capital = total_assets - total_liabilities - retained - result_accum
        total_equity = capital + retained + result_accum

        monthly.append({
            "month": month,
            "revenue_factor": revenue_factors[idx],
            "cost_rate": cost_rates[idx],
            "purchase_factor": purchase_factors[idx],
            "revenue": revenue,
            "cash_sales": cash_sales,
            "credit_sales": credit_sales,
            "cogs": cogs,
            "gross_profit": gross_profit,
            "expenses": expenses_nio,
            "total_expenses": total_expenses,
            "net_income": net_income,
            "purchases": purchases,
            "collections": collections,
            "depreciation": depreciation,
            "financial_expense": financial_expense,
            "cash": state["cash"],
            "accounts_receivable": state["accounts_receivable"],
            "inventory": state["inventory"],
            "ppe_real_estate": state["ppe_real_estate"],
            "ppe_equipment": state["ppe_equipment"],
            "ppe_vehicles": state["ppe_vehicles"],
            "accum_depreciation": state["accum_depreciation"],
            "credit_cards": state["credit_cards"],
            "suppliers": state["suppliers"],
            "taxes_payable": state["taxes_payable"],
            "accrued_expenses": state["accrued_expenses"],
            "loans_mortgage": state["loans_mortgage"],
            "loans_consumo": state["loans_consumo"],
            "loans_personal": state["loans_personal"],
            "loans_pledge": state["loans_pledge"],
            "loans_commercial": state["loans_commercial"],
            "current_assets": current_assets,
            "non_current_assets": non_current_assets,
            "total_assets": total_assets,
            "current_liabilities": current_liabilities,
            "non_current_liabilities": non_current_liabilities,
            "total_liabilities": total_liabilities,
            "capital": capital,
            "retained_earnings": retained,
            "result_accum": result_accum,
            "total_equity": total_equity,
            "total_liabilities_equity": total_liabilities + total_equity,
            "balance_check": total_assets - (total_liabilities + total_equity),
        })

    df_er = _build_er_dataframe(monthly, months, base_income_usd, exchange_rate, cash_sales_pct)
    df_mov = _build_mov_dataframe(monthly, months, balances)
    df_esf = _build_esf_dataframe(monthly, months)
    df_cert = _build_cert_dataframe(payload, months, df_er, seed)
    df_datos = _build_datos_dataframe(payload)

    v_er = validate_er(df_er, tolerance=1.0)
    v_esf = validate_esf(df_esf, tolerance=1.0, mode="mensual")
    balance_errors = [
        {"month": _month_key(item["month"]), "difference": item["balance_check"]}
        for item in monthly
        if abs(item["balance_check"]) > 1.0
    ]
    validations = {
        "er": v_er,
        "esf": v_esf,
        "balance": {"ok": not balance_errors, "errors": balance_errors},
    }
    summary = {
        "seed": seed,
        "months": [_month_key(m) for m in months],
        "income_total": _round(sum(item["revenue"] for item in monthly)),
        "income_average": _round(sum(item["revenue"] for item in monthly) / len(monthly)),
        "net_income_total": _round(sum(item["net_income"] for item in monthly)),
        "net_income_average": _round(sum(item["net_income"] for item in monthly) / len(monthly)),
        "ending_assets": _round(monthly[-1]["total_assets"]),
        "ending_liabilities": _round(monthly[-1]["total_liabilities"]),
        "ending_equity": _round(monthly[-1]["total_equity"]),
        "er_ok": bool(v_er.get("ok")),
        "esf_ok": bool(v_esf.get("ok")),
        "balance_ok": not balance_errors,
    }
    metadata = {
        "exchange_rate": exchange_rate,
        "seed": seed,
        "income_variability_pct": income_variability * 100,
        "cost_variability_pct": cost_variability * 100,
        "revenue_factors": revenue_factors,
        "cost_rates": cost_rates,
        "purchase_factors": purchase_factors,
    }
    return FinancialModelResult(df_cert, df_er, df_mov, df_esf, df_datos, validations, summary, metadata)


def result_to_json(result: FinancialModelResult) -> Dict[str, Any]:
    return {
        "ok": bool(
            result.validations["er"].get("ok")
            and result.validations["esf"].get("ok")
            and result.validations["balance"].get("ok")
        ),
        "summary": _json_safe(result.summary),
        "validations": _json_safe(result.validations),
        "preview": {
            "er": _df_preview(result.df_er, max_rows=40),
            "esf_mensual": _df_preview(result.df_esf_mensual, max_rows=45),
            "movimientos": _df_preview(result.df_movimientos, max_rows=55),
            "certificacion": _df_preview(result.df_certificacion, max_rows=30),
        },
        "metadata": _json_safe(result.metadata),
    }


def _build_months(*, end_month: str, count: int) -> List[pd.Timestamp]:
    count = max(1, min(int(count or 1), 36))
    if not end_month:
        end = pd.Timestamp.today().to_period("M").to_timestamp(how="end")
    else:
        end = pd.to_datetime(end_month, errors="coerce")
        if pd.isna(end):
            raise ValueError("Mes final no valido")
        end = pd.Timestamp(end).to_period("M").to_timestamp(how="end")
    start_period = end.to_period("M") - (count - 1)
    return [p.to_timestamp(how="end") for p in pd.period_range(start_period, end.to_period("M"), freq="M")]


def _default_seed(months: List[pd.Timestamp], payload: Mapping[str, Any]) -> str:
    client = dict(payload.get("client") or {})
    name = str(client.get("nombre_completo") or client.get("name") or "").strip().lower()
    return f"{name or 'certificacion'}-{_month_key(months[-1])}-{len(months)}"


def _build_er_dataframe(
    monthly: List[Dict[str, Any]],
    months: List[pd.Timestamp],
    base_income_usd: float,
    exchange_rate: float,
    cash_sales_pct: float,
) -> pd.DataFrame:
    columns = ["Descripcion", "Base", *months, "Acumulado del periodo", "Promedio Mensual"]
    n = len(monthly)

    def values(field: str) -> List[float]:
        return [_round(item[field]) for item in monthly]

    def sum_avg(vals: Iterable[float]) -> tuple[float, float]:
        vals = list(vals)
        return _round(sum(vals)), _round(sum(vals) / n)

    rows: List[List[Any]] = []
    rows.append(["", "", *[item["revenue_factor"] for item in monthly], "", ""])
    rows.append(["", "", *[item["cost_rate"] for item in monthly], "", ""])
    rows.append(["", "", *[exchange_rate for _ in monthly], "", ""])
    rows.append([cash_sales_pct, "", *values("cash_sales"), "", ""])
    rows.append([1.0 - cash_sales_pct, "", *values("credit_sales"), "", ""])
    rows.append(["", "", *["" for _ in monthly], "", ""])

    revenue = values("revenue")
    acc, avg = sum_avg(revenue)
    rows.append(["Ingresos", base_income_usd, *revenue, acc, avg])
    rows.append(["", "", *["" for _ in monthly], "", ""])

    cogs = values("cogs")
    acc, avg = sum_avg(cogs)
    rows.append(["(-) Costo de ventas", "", *cogs, acc, avg])
    rows.append(["", "", *["" for _ in monthly], "", ""])

    gross = values("gross_profit")
    acc, avg = sum_avg(gross)
    rows.append(["(=) Ingresos Brutos", "", *gross, acc, avg])
    rows.append(["", "", *["" for _ in monthly], "", ""])
    rows.append(["(-) Gastos operativos", "", *["" for _ in monthly], "", ""])

    expense_rows: List[List[Any]] = []
    for label in EXPENSE_ORDER:
        vals = [_round(item["expenses"].get(label, 0.0)) for item in monthly]
        acc, avg = sum_avg(vals)
        expense_rows.append([label, "", *vals, acc, avg])
    rows.extend(expense_rows)

    total_exp = [_round(sum(row[2 + i] for row in expense_rows)) for i in range(n)]
    total_acc = _round(sum(row[-2] for row in expense_rows))
    total_avg = _round(sum(row[-1] for row in expense_rows))
    rows.append(["Total gastos operativos", "", *total_exp, total_acc, total_avg])
    rows.append(["", "", *["" for _ in monthly], "", ""])

    net = [_round(g - e) for g, e in zip(gross, total_exp)]
    rows.append(["Ingresos/Utilidad Neta", "", *net, _round(sum(net)), _round(sum(net) / n)])
    return pd.DataFrame(rows, columns=columns)


def _build_mov_dataframe(
    monthly: List[Dict[str, Any]],
    months: List[pd.Timestamp],
    balances: Mapping[str, float],
) -> pd.DataFrame:
    columns = ["Descripcion", "Saldos Iniciales", *months]

    def row(label: str, initial: Any = "", field: Optional[str] = None) -> List[Any]:
        values = [_round(item.get(field, 0.0)) if field else "" for item in monthly]
        return [label, initial, *values]

    rows = [
        ["Corrientes", "", *["" for _ in months]],
        row("Efectivo y Equivalentes de Efectivo", balances.get("cash", 0.0), "cash"),
        row("Ingresos", "", "revenue"),
        row("Ventas Contado", "", "cash_sales"),
        row("Recuperacion de Cartera", "", "collections"),
        row("Egresos", "", "total_expenses"),
        row("Compras", "", "purchases"),
        row("Costo de Venta", "", "cogs"),
        row("Cuentas por cobrar", balances.get("accounts_receivable", 0.0), "accounts_receivable"),
        row("Inventario", balances.get("inventory", 0.0), "inventory"),
        row("Total Corrientes", "", "current_assets"),
        ["No Corrientes", "", *["" for _ in months]],
        row("Vivienda", balances.get("ppe_real_estate", 0.0), "ppe_real_estate"),
        row("Equipos", balances.get("ppe_equipment", 0.0), "ppe_equipment"),
        row("Vehiculos", balances.get("ppe_vehicles", 0.0), "ppe_vehicles"),
        row("(-) Depreciacion Acumulada", balances.get("accum_depreciation", 0.0), "accum_depreciation"),
        row("(+) Gasto por Depreciacion", "", "depreciation"),
        row("Total No Corriente", "", "non_current_assets"),
        row("Total Activos", "", "total_assets"),
        ["Pasivos", "", *["" for _ in months]],
        row("Tarjetas", balances.get("credit_cards", 0.0), "credit_cards"),
        row("Proveedores", balances.get("suppliers", 0.0), "suppliers"),
        row("Cuentas por pagar", balances.get("taxes_payable", 0.0), "taxes_payable"),
        row("Gastos Acumulados por pagar", balances.get("accrued_expenses", 0.0), "accrued_expenses"),
        row("Total Corrientes", "", "current_liabilities"),
        row("Hipotecarios", balances.get("loans_mortgage", 0.0), "loans_mortgage"),
        row("Consumo", balances.get("loans_consumo", 0.0), "loans_consumo"),
        row("Personales", balances.get("loans_personal", 0.0), "loans_personal"),
        row("Prendarios", balances.get("loans_pledge", 0.0), "loans_pledge"),
        row("Comerciales", balances.get("loans_commercial", 0.0), "loans_commercial"),
        row("Total No Corrientes", "", "non_current_liabilities"),
        row("Total Pasivos", "", "total_liabilities"),
        ["Patrimonio", "", *["" for _ in months]],
        row("Capital", "", "capital"),
        row("Resultados Acumulados", balances.get("retained_earnings", 0.0), "retained_earnings"),
        row("Resultados del Ejercicio", "", "result_accum"),
        row("Total Patrimonio", "", "total_equity"),
        row("Total Pasivo + Patrimonio", "", "total_liabilities_equity"),
    ]
    return pd.DataFrame(rows, columns=columns)


def _build_esf_dataframe(monthly: List[Dict[str, Any]], months: List[pd.Timestamp]) -> pd.DataFrame:
    columns = ["Descripcion", *months]

    def row(label: str, field: Optional[str] = None) -> List[Any]:
        values = [_round(item.get(field, 0.0)) if field else "" for item in monthly]
        return [label, *values]

    rows = [
        ["Activos", *["" for _ in months]],
        ["Corrientes", *["" for _ in months]],
        row("Efectivo y Equivalentes de Efectivo", "cash"),
        row("Cuentas por Cobrar Clientes", "accounts_receivable"),
        row("Inventarios", "inventory"),
        row("Total Corrientes", "current_assets"),
        ["No Corrientes", *["" for _ in months]],
        ["Propiedad Planta y Equipos", *["" for _ in months]],
        row("Bienes Inmuebles", "ppe_real_estate"),
        row("Mobiliario y Equipos", "ppe_equipment"),
        row("Vehiculos", "ppe_vehicles"),
        row("(-) Depreciacion Acumulada", "accum_depreciation"),
        row("Total No Corriente", "non_current_assets"),
        row("Total Activos", "total_assets"),
        ["Pasivos", *["" for _ in months]],
        ["Corrientes", *["" for _ in months]],
        row("Tarjetas de Credito", "credit_cards"),
        row("Proveedores", "suppliers"),
        row("Impuestos por Pagar", "taxes_payable"),
        row("Gastos Acumulados por pagar", "accrued_expenses"),
        row("Total Corrientes", "current_liabilities"),
        ["No Corrientes", *["" for _ in months]],
        row("Creditos Hipotecarios", "loans_mortgage"),
        row("Creditos Consumo", "loans_consumo"),
        row("Creditos Personales", "loans_personal"),
        row("Creditos Prendarios", "loans_pledge"),
        row("Creditos Comerciales", "loans_commercial"),
        row("Total No Corrientes", "non_current_liabilities"),
        row("Total Pasivos", "total_liabilities"),
        ["Patrimonio", *["" for _ in months]],
        row("Capital", "capital"),
        row("Resultados Acumulados", "retained_earnings"),
        row("Resultados del Ejercicio", "result_accum"),
        row("Total Patrimonio", "total_equity"),
        row("Total Pasivo + Patrimonio", "total_liabilities_equity"),
    ]
    return pd.DataFrame(rows, columns=columns)


def _build_cert_dataframe(
    payload: Mapping[str, Any],
    months: List[pd.Timestamp],
    df_er: pd.DataFrame,
    seed: str,
) -> pd.DataFrame:
    client = dict(payload.get("client") or {})
    end_month = months[-1]
    start_month = months[0].to_period("M").to_timestamp(how="start")
    revenue_row = _row_by_label(df_er, "Ingresos")
    net_row = _row_by_label(df_er, "Ingresos/Utilidad Neta")
    accum_col = "Acumulado del periodo"
    avg_col = "Promedio Mensual"

    rows = [
        ("Nombre completo", client.get("nombre_completo") or client.get("name") or ""),
        ("Cedula", client.get("cedula") or ""),
        ("Fecha Inicio", start_month),
        ("Fecha Fin", end_month),
        ("Estado Civil", client.get("estado_civil") or ""),
        ("Profesion", client.get("profesion") or ""),
        ("Sexo", client.get("sexo") or ""),
        ("Domicilio", client.get("domicilio") or ""),
        ("Direccion Personal", client.get("direccion_personal") or ""),
        ("Direccion Negocio", client.get("direccion_negocio") or ""),
        ("Primer Apellido", client.get("primer_apellido") or _last_name(client.get("nombre_completo") or "")),
        ("Ingresos Brutos", revenue_row.get(accum_col, 0.0)),
        ("Ingresos Promedio", revenue_row.get(avg_col, 0.0)),
        ("Utilidad del Periodo", net_row.get(accum_col, 0.0)),
        ("Utilidad Promedio", net_row.get(avg_col, 0.0)),
        ("Banco", client.get("banco") or ""),
        ("Fecha Certificacion", _coerce_date(client.get("fecha_certificacion")) or pd.Timestamp.today().normalize()),
        ("Contacto", client.get("contacto") or ""),
        ("Regimen", client.get("regimen") or ""),
        ("Matricula COMMEMA No.", client.get("matricula") or ""),
        ("Giro del Negocio", client.get("giro_negocio") or ""),
        ("Antiguedad", client.get("antiguedad") or ""),
        ("Empleados", client.get("empleados") or ""),
        ("Semilla Modelo", seed),
    ]
    return pd.DataFrame({"Descripcion": [r[0] for r in rows], "Datos": [r[1] for r in rows], "Check List": [1 for _ in rows]})


def _build_datos_dataframe(payload: Mapping[str, Any]) -> pd.DataFrame:
    client = dict(payload.get("client") or {})
    rows = [
        ["Giro del Negocio", "", client.get("giro_negocio") or ""],
        ["Direccion del Negocio", "", client.get("direccion_negocio") or ""],
        ["Antiguedad", "", client.get("antiguedad") or ""],
        ["Empleados", "", client.get("empleados") or ""],
        ["Regimen", "", client.get("regimen") or ""],
        ["Contacto", "", client.get("contacto") or ""],
    ]
    return pd.DataFrame(rows)


def _index_events(raw_events: Any, months: List[pd.Timestamp], exchange_rate: float) -> Dict[str, Dict[str, float]]:
    allowed = {_month_key(m) for m in months}
    out: Dict[str, Dict[str, float]] = {key: {} for key in allowed}
    if not isinstance(raw_events, list):
        return out
    for event in raw_events:
        if not isinstance(event, Mapping):
            continue
        month_key = str(event.get("month") or event.get("mes") or "").strip()[:7]
        if month_key not in allowed:
            continue
        account = _normalize_account(event.get("account") or event.get("cuenta") or "")
        if not account:
            continue
        amount_nio = event.get("amount_nio")
        if amount_nio is None:
            amount = _to_float(event.get("amount") or event.get("monto") or 0.0, 0.0)
            currency = str(event.get("currency") or event.get("moneda") or "nio").strip().lower()
            amount_nio = amount * exchange_rate if currency in {"usd", "dolar", "dolares"} else amount
        amount_nio = _round(_to_float(amount_nio, 0.0))
        out[month_key][account] = out[month_key].get(account, 0.0) + amount_nio
    return out


def _normalize_account(value: Any) -> str:
    key = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "retiro_patrimonio": "owner_withdrawal",
        "retiro": "owner_withdrawal",
        "aporte_capital": "capital_contribution",
        "capital": "capital_contribution",
        "equipo": "asset_equipment",
        "activo_equipo": "asset_equipment",
        "vehiculo": "asset_vehicle",
        "activo_vehiculo": "asset_vehicle",
        "vivienda": "asset_real_estate",
        "bien_inmueble": "asset_real_estate",
        "credito_prendario": "loan_pledge_new",
        "nuevo_credito_prendario": "loan_pledge_new",
        "abono_prendario": "loan_pledge_payment",
        "credito_personal": "loan_personal_new",
        "nuevo_credito_personal": "loan_personal_new",
        "abono_personal": "loan_personal_payment",
        "credito_comercial": "loan_commercial_new",
        "nuevo_credito_comercial": "loan_commercial_new",
        "abono_comercial": "loan_commercial_payment",
        "credito_hipotecario": "loan_mortgage_new",
        "abono_hipotecario": "loan_mortgage_payment",
        "tarjeta": "credit_card_new",
        "abono_tarjeta": "credit_card_payment",
        "proveedor": "supplier_new",
        "pago_proveedor": "supplier_payment",
    }
    allowed = {
        "owner_withdrawal", "capital_contribution",
        "asset_real_estate", "asset_equipment", "asset_vehicle",
        "asset_real_estate_sale", "asset_equipment_sale", "asset_vehicle_sale",
        "loan_mortgage_new", "loan_mortgage_payment",
        "loan_consumo_new", "loan_consumo_payment",
        "loan_personal_new", "loan_personal_payment",
        "loan_pledge_new", "loan_pledge_payment",
        "loan_commercial_new", "loan_commercial_payment",
        "credit_card_new", "credit_card_payment",
        "supplier_new", "supplier_payment",
        "taxes_new", "taxes_payment",
        "accrued_new", "accrued_payment",
    }
    key = aliases.get(key, key)
    return key if key in allowed else ""


def _row_by_label(df: pd.DataFrame, label: str) -> pd.Series:
    col = df.columns[0]
    matches = df[df[col].astype(str).str.strip().str.lower() == label.lower()]
    if matches.empty:
        return pd.Series(dtype=object)
    return matches.iloc[0]


def _df_preview(df: pd.DataFrame, *, max_rows: int) -> Dict[str, Any]:
    subset = df.head(max_rows).copy()
    subset.columns = [_format_col(c) for c in subset.columns]
    return {
        "columns": list(subset.columns),
        "rows": _json_safe(subset.to_dict(orient="records")),
    }


def _format_col(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        return _month_key(value)
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.notna(ts) and not isinstance(value, str):
            return _month_key(pd.Timestamp(ts))
    except Exception:
        pass
    return str(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _pct(value: Any, default_pct: float) -> float:
    raw = _to_float(value, default_pct)
    if raw > 1:
        raw = raw / 100.0
    return max(0.0, min(1.0, raw))


def _round(value: Any) -> float:
    return float(round(_to_float(value, 0.0), 0))


def _month_key(month: pd.Timestamp) -> str:
    return pd.Timestamp(month).strftime("%Y-%m")


def _match_key(value: Any, candidates: Iterable[str]) -> Optional[str]:
    target = _plain(value)
    for candidate in candidates:
        if _plain(candidate) == target:
            return candidate
    return None


def _plain(value: Any) -> str:
    import unicodedata

    s = str(value or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return " ".join(s.replace("_", " ").split())


def _coerce_date(value: Any) -> Optional[pd.Timestamp]:
    try:
        ts = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(ts) else pd.Timestamp(ts)
    except Exception:
        return None


def _last_name(full_name: str) -> str:
    parts = str(full_name or "").strip().split()
    return parts[-1] if parts else ""

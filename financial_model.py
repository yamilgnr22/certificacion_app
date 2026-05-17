from __future__ import annotations

import calendar
import random
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

import pandas as pd

from accounting_model import build_accounting
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

LEDGER_ACCOUNTS: Dict[str, Dict[str, str]] = {
    "cash": {"label": "Efectivo y Equivalentes de Efectivo", "kind": "asset", "state": "cash"},
    "accounts_receivable": {"label": "Cuentas por Cobrar Clientes", "kind": "asset", "state": "accounts_receivable"},
    "inventory": {"label": "Inventarios", "kind": "asset", "state": "inventory"},
    "ppe_real_estate": {"label": "Bienes Inmuebles", "kind": "asset", "state": "ppe_real_estate"},
    "ppe_equipment": {"label": "Mobiliario y Equipos", "kind": "asset", "state": "ppe_equipment"},
    "ppe_vehicles": {"label": "Vehiculos", "kind": "asset", "state": "ppe_vehicles"},
    "accum_depreciation": {"label": "Depreciacion Acumulada", "kind": "contra_asset", "state": "accum_depreciation"},
    "credit_cards": {"label": "Tarjetas de Credito", "kind": "liability", "state": "credit_cards"},
    "suppliers": {"label": "Proveedores", "kind": "liability", "state": "suppliers"},
    "taxes_payable": {"label": "Impuestos por Pagar", "kind": "liability", "state": "taxes_payable"},
    "accrued_expenses": {"label": "Gastos Acumulados por pagar", "kind": "liability", "state": "accrued_expenses"},
    "loans_mortgage": {"label": "Creditos Hipotecarios", "kind": "liability", "state": "loans_mortgage"},
    "loans_consumo": {"label": "Creditos Consumo", "kind": "liability", "state": "loans_consumo"},
    "loans_personal": {"label": "Creditos Personales", "kind": "liability", "state": "loans_personal"},
    "loans_pledge": {"label": "Creditos Prendarios", "kind": "liability", "state": "loans_pledge"},
    "loans_commercial": {"label": "Creditos Comerciales", "kind": "liability", "state": "loans_commercial"},
    "capital": {"label": "Capital", "kind": "equity", "state": ""},
    "retained_earnings": {"label": "Resultados Acumulados", "kind": "equity", "state": "retained_earnings"},
    "current_earnings": {"label": "Resultados del Ejercicio", "kind": "equity", "state": "result_accum"},
}


@dataclass
class FinancialModelResult:
    df_certificacion: pd.DataFrame
    df_er: pd.DataFrame
    df_movimientos: pd.DataFrame
    df_flujo_caja: pd.DataFrame
    df_movimiento_cuentas: pd.DataFrame
    df_esf_mensual: pd.DataFrame
    df_datos: pd.DataFrame
    validations: Dict[str, Any]
    summary: Dict[str, Any]
    metadata: Dict[str, Any]
    accounting: Dict[str, Any]
    statement_blocks: List[Dict[str, Any]]
    df_er_full: pd.DataFrame
    df_movimientos_full: pd.DataFrame
    df_flujo_caja_full: pd.DataFrame
    df_movimiento_cuentas_full: pd.DataFrame
    df_esf_mensual_full: pd.DataFrame


def build_financial_model(payload: Mapping[str, Any]) -> FinancialModelResult:
    """Build the financial statements that used to be prepared in Excel."""
    payload = dict(payload or {})
    period = dict(payload.get("period") or {})
    income = dict(payload.get("income") or {})
    expenses_payload = dict(payload.get("expenses") or {})
    balances_payload = dict(payload.get("balances") or {})
    movement_payload = dict(payload.get("movements") or {})

    months = _build_months(
        start_month=str(period.get("start_month") or period.get("mes_inicio") or ""),
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
    income_overrides = _index_income_overrides(income.get("monthly_overrides") or income.get("overrides") or [])
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
    journal_entries = _index_journal_entries(movement_payload.get("journal_entries") or [], months, exchange_rate)

    revenue_factors: List[float] = []
    cost_rates: List[float] = []
    purchase_factors: List[float] = []
    for month in months:
        month_override = income_overrides.get(_month_key(month), {})
        month_cost_pct = _override_pct(month_override, "cost_pct", cost_pct)
        month_cost_variability = _override_pct(month_override, "cost_variability_pct", cost_variability)
        revenue_factors.append(1.0 + rng.uniform(-income_variability, income_variability))
        cost_rates.append(max(0.0, min(1.0, month_cost_pct + rng.uniform(-month_cost_variability, month_cost_variability))))
        purchase_factors.append(1.0 + rng.uniform(-purchase_variability, purchase_variability))

    monthly: List[Dict[str, float]] = []
    state = dict(balances)
    result_accum = 0.0
    result_accum_adjustment = 0.0
    opening_capital = _opening_capital(balances)
    previous_capital = opening_capital

    for idx, month in enumerate(months):
        key = _month_key(month)
        month_events = events.get(key, {})
        month_journal_entries = journal_entries.get(key, [])

        beginning_state = dict(state)
        beginning_result_accum = result_accum + result_accum_adjustment
        capital_beginning = previous_capital
        cash_beginning = state["cash"]
        revenue = _round(base_income_usd * exchange_rate * revenue_factors[idx])
        cash_sales = _round(revenue * cash_sales_pct)
        credit_sales = _round(revenue * credit_sales_pct)
        cogs = _round(revenue * cost_rates[idx])
        gross_profit = revenue - cogs
        base_purchases = _round(purchase_base_usd * exchange_rate * purchase_factors[idx])
        purchase_adjustment = _round(month_events.get("purchase_adjustment", 0.0))
        purchases = _round(max(0.0, base_purchases + purchase_adjustment))
        effective_purchase_adjustment = _round(purchases - base_purchases)

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
        supplier_financing_requested = _round(max(0.0, month_events.get("supplier_financing", 0.0)))
        supplier_financing = _round(min(supplier_financing_requested, purchases))
        cash_purchases = _round(max(0.0, purchases - supplier_financing))
        supplier_new = month_events.get("supplier_new", 0.0) + supplier_financing
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
        retained_earnings_distribution = _round(max(0.0, month_events.get("retained_earnings_distribution", 0.0)))
        capital_reclassification = _round(month_events.get("capital_reclassification", 0.0))
        sale_ppe = (
            month_events.get("asset_real_estate_sale", 0.0)
            + month_events.get("asset_equipment_sale", 0.0)
            + month_events.get("asset_vehicle_sale", 0.0)
        )
        state["retained_earnings"] = _round(
            state["retained_earnings"]
            + capital_reclassification
            - retained_earnings_distribution
        )
        retained_earnings_increase = capital_reclassification if capital_reclassification > 0 else 0.0
        retained_earnings_decrease = (
            retained_earnings_distribution
            + (abs(capital_reclassification) if capital_reclassification < 0 else 0.0)
        )

        cash_inflows = cash_sales + collections + new_credit_cash + sale_ppe + capital_contribution
        cash_outflows = (
            cash_operating_expenses
            + cash_purchases
            + credit_card_payment
            + supplier_payment
            + taxes_payment
            + accrued_payment
            + principal_payments
            + asset_purchases
            + owner_withdrawal
            + retained_earnings_distribution
        )

        state["cash"] = _round(
            cash_beginning
            + cash_sales
            + collections
            + new_credit_cash
            + sale_ppe
            + capital_contribution
            - cash_operating_expenses
            - cash_purchases
            - credit_card_payment
            - supplier_payment
            - taxes_payment
            - accrued_payment
            - principal_payments
            - asset_purchases
            - owner_withdrawal
            - retained_earnings_distribution
        )

        journal_effects = _apply_journal_entries_to_state(state, month_journal_entries)
        result_accum_adjustment += journal_effects.get("result_accum_delta", 0.0)
        journal_cash_increase = journal_effects.get("cash_journal_increase", 0.0)
        journal_cash_decrease = journal_effects.get("cash_journal_decrease", 0.0)
        if journal_cash_increase:
            cash_inflows = _round(cash_inflows + journal_cash_increase)
        if journal_cash_decrease:
            cash_outflows = _round(cash_outflows + journal_cash_decrease)
        current_earnings_journal_increase = journal_effects.get("result_accum_journal_increase", 0.0)
        current_earnings_journal_decrease = journal_effects.get("result_accum_journal_decrease", 0.0)

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
        retained = state["retained_earnings"]
        displayed_result_accum = result_accum + result_accum_adjustment
        capital = total_assets - total_liabilities - retained - displayed_result_accum
        capital_change = capital - capital_beginning
        capital_increase = capital_change if capital_change > 0 else 0.0
        capital_decrease = abs(capital_change) if capital_change < 0 else 0.0
        total_equity = capital + retained + displayed_result_accum

        month_data = {
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
            "base_purchases": base_purchases,
            "purchase_adjustment": effective_purchase_adjustment,
            "purchases": purchases,
            "supplier_financing": supplier_financing,
            "cash_purchases": cash_purchases,
            "collections": collections,
            "depreciation": depreciation,
            "financial_expense": financial_expense,
            "cash_beginning": cash_beginning,
            "cash_inflows": cash_inflows,
            "cash_outflows": cash_outflows,
            "cash_operating_expenses": cash_operating_expenses,
            "principal_payments": principal_payments,
            "new_credit_cash": new_credit_cash,
            "asset_purchases": asset_purchases,
            "owner_withdrawal": owner_withdrawal,
            "capital_contribution": capital_contribution,
            "retained_earnings_distribution": retained_earnings_distribution,
            "capital_reclassification": capital_reclassification,
            "retained_earnings_increase": retained_earnings_increase,
            "retained_earnings_decrease": retained_earnings_decrease,
            "result_accum_journal_increase": current_earnings_journal_increase,
            "result_accum_journal_decrease": current_earnings_journal_decrease,
            "cash_journal_increase": journal_cash_increase,
            "cash_journal_decrease": journal_cash_decrease,
            "journal_entries": month_journal_entries,
            "sale_ppe": sale_ppe,
            "credit_card_payment": credit_card_payment,
            "supplier_payment": supplier_payment,
            "taxes_payment": taxes_payment,
            "accrued_payment": accrued_payment,
            "additions_real_estate": additions_real_estate,
            "additions_equipment": additions_equipment,
            "additions_vehicles": additions_vehicles,
            "sale_real_estate": month_events.get("asset_real_estate_sale", 0.0),
            "sale_equipment": month_events.get("asset_equipment_sale", 0.0),
            "sale_vehicles": month_events.get("asset_vehicle_sale", 0.0),
            "credit_card_new": credit_card_new,
            "supplier_new": supplier_new,
            "taxes_new": taxes_new,
            "accrued_new": accrued_new,
            "loan_mortgage_new": new_loans["loans_mortgage"],
            "loan_consumo_new": new_loans["loans_consumo"],
            "loan_personal_new": new_loans["loans_personal"],
            "loan_pledge_new": new_loans["loans_pledge"],
            "loan_commercial_new": new_loans["loans_commercial"],
            "loan_mortgage_payment": loan_payments["loans_mortgage"],
            "loan_consumo_payment": loan_payments["loans_consumo"],
            "loan_personal_payment": loan_payments["loans_personal"],
            "loan_pledge_payment": loan_payments["loans_pledge"],
            "loan_commercial_payment": loan_payments["loans_commercial"],
            "capital_beginning": capital_beginning,
            "capital_increase": capital_increase,
            "capital_decrease": capital_decrease,
            "result_accum_beginning": beginning_result_accum,
            "result_accum_increase": net_income if net_income > 0 else 0.0,
            "result_accum_decrease": abs(net_income) if net_income < 0 else 0.0,
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
            "result_accum": displayed_result_accum,
            "total_equity": total_equity,
            "total_liabilities_equity": total_liabilities + total_equity,
            "balance_check": total_assets - (total_liabilities + total_equity),
        }
        for effect_key, effect_value in journal_effects.items():
            if effect_key != "result_accum_delta":
                month_data[effect_key] = effect_value
        for account_key, beginning_value in beginning_state.items():
            month_data[f"{account_key}_beginning"] = beginning_value
        monthly.append(month_data)
        previous_capital = capital

    full_df_er = _build_er_dataframe(monthly, months, base_income_usd, exchange_rate, cash_sales_pct)
    full_df_mov = _build_mov_dataframe(monthly, months, balances)
    full_df_cash = _build_cash_flow_dataframe(monthly, months)
    full_df_account_mov = _build_account_movement_dataframe(monthly, months)
    full_df_esf = _build_esf_dataframe(monthly, months)
    accounting = build_accounting(payload, monthly, months, balances)
    df_datos = _build_datos_dataframe(payload)

    v_er = validate_er(full_df_er, tolerance=1.0)
    v_esf = validate_esf(full_df_esf, tolerance=1.0, mode="mensual")
    balance_errors = [
        {"month": _month_key(item["month"]), "difference": item["balance_check"]}
        for item in monthly
        if abs(item["balance_check"]) > 1.0
    ]
    negative_cash = [
        {"month": _month_key(item["month"]), "cash": _round(item["cash"])}
        for item in monthly
        if item["cash"] < 0
    ]
    validations = {
        "er": v_er,
        "esf": v_esf,
        "balance": {"ok": not balance_errors, "errors": balance_errors},
        "cash": {"ok": not negative_cash, "warnings": negative_cash},
    }
    full_summary = _build_summary(monthly, months, seed, v_er, v_esf, balance_errors, negative_cash)
    period_blocks = _build_period_blocks(months)
    statement_blocks = []
    for block in period_blocks:
        block_keys = set(block["months"])
        block_monthly = [item for item in monthly if _month_key(item["month"]) in block_keys]
        block_months = [m for m in months if _month_key(m) in block_keys]
        block_balances = _block_opening_balances(block_monthly[0], balances)
        block_df_er = _build_er_dataframe(block_monthly, block_months, base_income_usd, exchange_rate, cash_sales_pct)
        block_df_mov = _build_mov_dataframe(block_monthly, block_months, block_balances)
        block_df_cash = _build_cash_flow_dataframe(block_monthly, block_months)
        block_df_account_mov = _build_account_movement_dataframe(block_monthly, block_months)
        block_df_esf = _build_esf_dataframe(block_monthly, block_months)
        block_df_cert = _build_cert_dataframe(payload, block_months, block_df_er, seed)
        block_negative_cash = [
            {"month": _month_key(item["month"]), "cash": _round(item["cash"])}
            for item in block_monthly
            if item["cash"] < 0
        ]
        block_balance_errors = [
            {"month": _month_key(item["month"]), "difference": item["balance_check"]}
            for item in block_monthly
            if abs(item["balance_check"]) > 1.0
        ]
        statement_blocks.append({
            "meta": block,
            "df_certificacion": block_df_cert,
            "df_er": block_df_er,
            "df_movimientos": block_df_mov,
            "df_flujo_caja": block_df_cash,
            "df_movimiento_cuentas": block_df_account_mov,
            "df_esf_mensual": block_df_esf,
            "summary": _build_summary(
                block_monthly,
                block_months,
                seed,
                validate_er(block_df_er, tolerance=1.0),
                validate_esf(block_df_esf, tolerance=1.0, mode="mensual"),
                block_balance_errors,
                block_negative_cash,
            ),
        })

    latest_block = statement_blocks[0]
    df_er = latest_block["df_er"]
    df_mov = latest_block["df_movimientos"]
    df_cash = latest_block["df_flujo_caja"]
    df_account_mov = latest_block["df_movimiento_cuentas"]
    df_esf = latest_block["df_esf_mensual"]
    df_cert = latest_block["df_certificacion"]
    summary = dict(latest_block["summary"])
    summary["all_months"] = full_summary["months"]
    metadata = {
        "exchange_rate": exchange_rate,
        "seed": seed,
        "start_month": _month_key(months[0]),
        "end_month": _month_key(months[-1]),
        "full_summary": full_summary,
        "period_blocks": period_blocks,
        "income_variability_pct": income_variability * 100,
        "cost_variability_pct": cost_variability * 100,
        "income_monthly_overrides": income_overrides,
        "revenue_factors": revenue_factors,
        "cost_rates": cost_rates,
        "purchase_factors": purchase_factors,
    }
    return FinancialModelResult(
        df_cert,
        df_er,
        df_mov,
        df_cash,
        df_account_mov,
        df_esf,
        df_datos,
        validations,
        summary,
        metadata,
        accounting,
        statement_blocks,
        full_df_er,
        full_df_mov,
        full_df_cash,
        full_df_account_mov,
        full_df_esf,
    )


def result_to_json(result: FinancialModelResult) -> Dict[str, Any]:
    blocks_preview = {}
    for block in result.statement_blocks:
        meta = block["meta"]
        blocks_preview[meta["id"]] = {
            "er": _df_preview(block["df_er"], max_rows=40),
            "esf_mensual": _df_preview(block["df_esf_mensual"], max_rows=45),
            "movimientos": _df_preview(block["df_movimientos"], max_rows=55),
            "flujo_caja": _df_preview(block["df_flujo_caja"], max_rows=30),
            "movimiento_cuentas": _df_preview(block["df_movimiento_cuentas"], max_rows=90),
            "certificacion": _df_preview(block["df_certificacion"], max_rows=30),
            "summary": _json_safe(block["summary"]),
        }
    return {
        "ok": bool(
            result.validations["er"].get("ok")
            and result.validations["esf"].get("ok")
            and result.validations["balance"].get("ok")
        ),
        "summary": _json_safe(result.summary),
        "full_summary": _json_safe(result.metadata.get("full_summary") or result.summary),
        "period_blocks": _json_safe(result.metadata.get("period_blocks") or []),
        "validations": _json_safe(result.validations),
        "accounting": _json_safe(result.accounting),
        "preview": {
            "er": _df_preview(result.df_er, max_rows=40),
            "esf_mensual": _df_preview(result.df_esf_mensual, max_rows=45),
            "movimientos": _df_preview(result.df_movimientos, max_rows=55),
            "flujo_caja": _df_preview(result.df_flujo_caja, max_rows=30),
            "movimiento_cuentas": _df_preview(result.df_movimiento_cuentas, max_rows=90),
            "certificacion": _df_preview(result.df_certificacion, max_rows=30),
            "blocks": blocks_preview,
        },
        "metadata": _json_safe(result.metadata),
    }


def _build_months(*, start_month: str = "", end_month: str, count: int) -> List[pd.Timestamp]:
    if not end_month:
        end = pd.Timestamp.today().to_period("M").to_timestamp(how="end")
    else:
        end = pd.to_datetime(end_month, errors="coerce")
        if pd.isna(end):
            raise ValueError("Mes final no valido")
        end = pd.Timestamp(end).to_period("M").to_timestamp(how="end")

    if start_month:
        start = pd.to_datetime(start_month, errors="coerce")
        if pd.isna(start):
            raise ValueError("Mes inicio no valido")
        start_period = pd.Timestamp(start).to_period("M")
        end_period = end.to_period("M")
        if start_period > end_period:
            raise ValueError("Mes inicio no puede ser posterior al mes final")
        period_count = (end_period.year - start_period.year) * 12 + (end_period.month - start_period.month) + 1
        if period_count > 36:
            raise ValueError("El rango no puede exceder 36 meses")
        return [p.to_timestamp(how="end") for p in pd.period_range(start_period, end_period, freq="M")]

    count = max(1, min(int(count or 1), 36))
    start_period = end.to_period("M") - (count - 1)
    return [p.to_timestamp(how="end") for p in pd.period_range(start_period, end.to_period("M"), freq="M")]


def _default_seed(months: List[pd.Timestamp], payload: Mapping[str, Any]) -> str:
    client = dict(payload.get("client") or {})
    name = str(client.get("nombre_completo") or client.get("name") or "").strip().lower()
    return f"{name or 'certificacion'}-{_month_key(months[-1])}-{len(months)}"


def _build_summary(
    monthly: List[Dict[str, Any]],
    months: List[pd.Timestamp],
    seed: str,
    v_er: Mapping[str, Any],
    v_esf: Mapping[str, Any],
    balance_errors: List[Dict[str, Any]],
    negative_cash: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
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
        "cash_ok": not negative_cash,
        "negative_cash_months": negative_cash,
    }


def _build_period_blocks(months: List[pd.Timestamp]) -> List[Dict[str, Any]]:
    if len(months) <= 12:
        return [_period_block("full_range", months, is_latest=True)]

    blocks: List[Dict[str, Any]] = []
    years = sorted({m.year for m in months}, reverse=True)
    for idx, year in enumerate(years):
        year_months = [m for m in months if m.year == year]
        blocks.append(_period_block(f"year_{year}", year_months, year=year, is_latest=(idx == 0)))
    return blocks


def _period_block(
    block_id: str,
    months: List[pd.Timestamp],
    *,
    year: Optional[int] = None,
    is_latest: bool,
) -> Dict[str, Any]:
    return {
        "id": block_id,
        "label": _period_label(months[0], months[-1]),
        "start_month": _month_key(months[0]),
        "end_month": _month_key(months[-1]),
        "months": [_month_key(m) for m in months],
        "month_count": len(months),
        "year": year,
        "is_latest": is_latest,
    }


def _period_label(start: pd.Timestamp, end: pd.Timestamp) -> str:
    if start.year == end.year:
        if start.month == end.month:
            return f"{_month_name(start.month)} {start.year}"
        return f"{_month_name(start.month)}-{_month_name(end.month)} {start.year}"
    return f"{_month_name(start.month)} {start.year}-{_month_name(end.month)} {end.year}"


def _month_name(month: int) -> str:
    names = {
        1: "Enero",
        2: "Febrero",
        3: "Marzo",
        4: "Abril",
        5: "Mayo",
        6: "Junio",
        7: "Julio",
        8: "Agosto",
        9: "Septiembre",
        10: "Octubre",
        11: "Noviembre",
        12: "Diciembre",
    }
    return names.get(int(month), str(month))


def _block_opening_balances(first_month_data: Mapping[str, Any], default_balances: Mapping[str, float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for key in DEFAULT_BALANCES_NIO:
        out[key] = _round(first_month_data.get(f"{key}_beginning", default_balances.get(key, 0.0)))
    return out


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
        row("Compras financiadas por proveedores", "", "supplier_financing"),
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


def _build_cash_flow_dataframe(monthly: List[Dict[str, Any]], months: List[pd.Timestamp]) -> pd.DataFrame:
    columns = ["Concepto", *months]

    def row(label: str, field: str, *, sign: int = 1) -> List[Any]:
        return [label, *[_round(item.get(field, 0.0) * sign) for item in monthly]]

    rows = [
        row("Saldo inicial de caja", "cash_beginning"),
        row("Ventas de contado", "cash_sales"),
        row("Recuperacion de cartera", "collections"),
        row("Nuevos creditos recibidos", "new_credit_cash"),
        row("Venta de activos", "sale_ppe"),
        row("Aportes de capital", "capital_contribution"),
        row("Entradas por partidas contables", "cash_journal_increase"),
        row("Total entradas de efectivo", "cash_inflows"),
        row("Gastos desembolsables", "cash_operating_expenses", sign=-1),
        row("Compras de inventario pagadas", "cash_purchases", sign=-1),
        row("Abonos de tarjetas", "credit_card_payment", sign=-1),
        row("Pagos a proveedores", "supplier_payment", sign=-1),
        row("Pago de impuestos", "taxes_payment", sign=-1),
        row("Pago de gastos acumulados", "accrued_payment", sign=-1),
        row("Abonos de creditos", "principal_payments", sign=-1),
        row("Compra de activos", "asset_purchases", sign=-1),
        row("Retiros de patrimonio", "owner_withdrawal", sign=-1),
        row("Distribucion de resultados acumulados", "retained_earnings_distribution", sign=-1),
        row("Salidas por partidas contables", "cash_journal_decrease", sign=-1),
        row("Total salidas de efectivo", "cash_outflows", sign=-1),
        row("Saldo final de caja", "cash"),
    ]
    return pd.DataFrame(rows, columns=columns)


def _build_account_movement_dataframe(monthly: List[Dict[str, Any]], months: List[pd.Timestamp]) -> pd.DataFrame:
    columns = ["Cuenta", "Movimiento", *months]
    rows: List[List[Any]] = []

    def add_account(
        account: str,
        beginning_field: str,
        increase_field: str,
        decrease_field: str,
        ending_field: str,
    ) -> None:
        rows.append([account, "Saldo inicial", *[_round(item.get(beginning_field, 0.0)) for item in monthly]])
        rows.append([
            account,
            "Aumentos",
            *[
                _round(item.get(increase_field, 0.0) + item.get(f"{ending_field}_journal_increase", 0.0))
                for item in monthly
            ],
        ])
        rows.append([
            account,
            "Disminuciones",
            *[
                -_round(item.get(decrease_field, 0.0) + item.get(f"{ending_field}_journal_decrease", 0.0))
                for item in monthly
            ],
        ])
        rows.append([account, "Saldo final", *[_round(item.get(ending_field, 0.0)) for item in monthly]])

    def add_cash_account() -> None:
        account = "Efectivo y Equivalentes de Efectivo"

        def add(label: str, field: str, *, sign: int = 1) -> None:
            rows.append([account, label, *[_round(item.get(field, 0.0) * sign) for item in monthly]])

        add("Saldo inicial", "cash_beginning")
        add("Entrada - ventas de contado", "cash_sales")
        add("Entrada - recuperacion de cartera", "collections")
        add("Entrada - nuevos creditos recibidos", "new_credit_cash")
        add("Entrada - venta de activos", "sale_ppe")
        add("Entrada - aportes de capital", "capital_contribution")
        add("Entrada - partidas contables", "cash_journal_increase")
        add("Aumentos", "cash_inflows")
        add("Salida - gastos desembolsables", "cash_operating_expenses", sign=-1)
        add("Salida - compras de inventario pagadas", "cash_purchases", sign=-1)
        add("Salida - abonos de tarjetas", "credit_card_payment", sign=-1)
        add("Salida - pagos a proveedores", "supplier_payment", sign=-1)
        add("Salida - pago de impuestos", "taxes_payment", sign=-1)
        add("Salida - pago de gastos acumulados", "accrued_payment", sign=-1)
        add("Salida - abonos de creditos", "principal_payments", sign=-1)
        add("Salida - compra de activos", "asset_purchases", sign=-1)
        add("Salida - retiros de patrimonio", "owner_withdrawal", sign=-1)
        add("Salida - distribucion de resultados acumulados", "retained_earnings_distribution", sign=-1)
        add("Salida - partidas contables", "cash_journal_decrease", sign=-1)
        add("Disminuciones", "cash_outflows", sign=-1)
        add("Saldo final", "cash")

    add_cash_account()
    add_account("Cuentas por Cobrar Clientes", "accounts_receivable_beginning", "credit_sales", "collections", "accounts_receivable")
    add_account("Inventarios", "inventory_beginning", "purchases", "cogs", "inventory")
    add_account("Bienes Inmuebles", "ppe_real_estate_beginning", "additions_real_estate", "sale_real_estate", "ppe_real_estate")
    add_account("Mobiliario y Equipos", "ppe_equipment_beginning", "additions_equipment", "sale_equipment", "ppe_equipment")
    add_account("Vehiculos", "ppe_vehicles_beginning", "additions_vehicles", "sale_vehicles", "ppe_vehicles")
    add_account("Depreciacion Acumulada", "accum_depreciation_beginning", "_zero", "depreciation", "accum_depreciation")
    add_account("Tarjetas de Credito", "credit_cards_beginning", "credit_card_new", "credit_card_payment", "credit_cards")
    add_account("Proveedores", "suppliers_beginning", "supplier_new", "supplier_payment", "suppliers")
    add_account("Impuestos por Pagar", "taxes_payable_beginning", "taxes_new", "taxes_payment", "taxes_payable")
    add_account("Gastos Acumulados por pagar", "accrued_expenses_beginning", "accrued_new", "accrued_payment", "accrued_expenses")
    add_account("Creditos Hipotecarios", "loans_mortgage_beginning", "loan_mortgage_new", "loan_mortgage_payment", "loans_mortgage")
    add_account("Creditos Consumo", "loans_consumo_beginning", "loan_consumo_new", "loan_consumo_payment", "loans_consumo")
    add_account("Creditos Personales", "loans_personal_beginning", "loan_personal_new", "loan_personal_payment", "loans_personal")
    add_account("Creditos Prendarios", "loans_pledge_beginning", "loan_pledge_new", "loan_pledge_payment", "loans_pledge")
    add_account("Creditos Comerciales", "loans_commercial_beginning", "loan_commercial_new", "loan_commercial_payment", "loans_commercial")
    add_account("Capital", "capital_beginning", "capital_increase", "capital_decrease", "capital")
    add_account("Resultados Acumulados", "retained_earnings_beginning", "retained_earnings_increase", "retained_earnings_decrease", "retained_earnings")
    add_account("Resultados del Ejercicio", "result_accum_beginning", "result_accum_increase", "result_accum_decrease", "result_accum")
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


def _opening_capital(balances: Mapping[str, float]) -> float:
    current_assets = (
        _to_float(balances.get("cash"), 0.0)
        + _to_float(balances.get("accounts_receivable"), 0.0)
        + _to_float(balances.get("inventory"), 0.0)
    )
    non_current_assets = (
        _to_float(balances.get("ppe_real_estate"), 0.0)
        + _to_float(balances.get("ppe_equipment"), 0.0)
        + _to_float(balances.get("ppe_vehicles"), 0.0)
        + _to_float(balances.get("accum_depreciation"), 0.0)
    )
    liabilities = (
        _to_float(balances.get("credit_cards"), 0.0)
        + _to_float(balances.get("suppliers"), 0.0)
        + _to_float(balances.get("taxes_payable"), 0.0)
        + _to_float(balances.get("accrued_expenses"), 0.0)
        + _to_float(balances.get("loans_mortgage"), 0.0)
        + _to_float(balances.get("loans_consumo"), 0.0)
        + _to_float(balances.get("loans_personal"), 0.0)
        + _to_float(balances.get("loans_pledge"), 0.0)
        + _to_float(balances.get("loans_commercial"), 0.0)
    )
    retained = _to_float(balances.get("retained_earnings"), 0.0)
    return _round(current_assets + non_current_assets - liabilities - retained)


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


def _index_journal_entries(raw_entries: Any, months: List[pd.Timestamp], exchange_rate: float) -> Dict[str, List[Dict[str, Any]]]:
    allowed_months = {_month_key(m) for m in months}
    out: Dict[str, List[Dict[str, Any]]] = {month: [] for month in allowed_months}
    if not isinstance(raw_entries, list):
        return out
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            continue
        month_key = str(entry.get("month") or entry.get("mes") or "").strip()[:7]
        if month_key not in allowed_months:
            continue
        debit_account = _normalize_ledger_account(entry.get("debit_account") or entry.get("debe") or "")
        credit_account = _normalize_ledger_account(entry.get("credit_account") or entry.get("haber") or "")
        if not debit_account or not credit_account or debit_account == credit_account:
            continue
        amount_nio = entry.get("amount_nio")
        if amount_nio is None:
            amount = _to_float(entry.get("amount") or entry.get("monto") or 0.0, 0.0)
            currency = str(entry.get("currency") or entry.get("moneda") or "nio").strip().lower()
            amount_nio = amount * exchange_rate if currency in {"usd", "dolar", "dolares"} else amount
        amount_nio = _round(_to_float(amount_nio, 0.0))
        if amount_nio <= 0:
            continue
        normalized = dict(entry)
        normalized["month"] = month_key
        normalized["debit_account"] = debit_account
        normalized["credit_account"] = credit_account
        normalized["amount_nio"] = amount_nio
        out[month_key].append(normalized)
    return out


def _apply_journal_entries_to_state(state: Dict[str, float], entries: List[Mapping[str, Any]]) -> Dict[str, float]:
    effects: Dict[str, float] = {"result_accum_delta": 0.0}
    for entry in entries:
        amount = _round(_to_float(entry.get("amount_nio") or entry.get("amount") or 0.0, 0.0))
        if amount <= 0:
            continue
        for account, side in (
            (str(entry.get("debit_account") or ""), "debit"),
            (str(entry.get("credit_account") or ""), "credit"),
        ):
            _apply_journal_side(state, effects, account, side, amount)
    return {key: _round(value) for key, value in effects.items()}


def _apply_journal_side(
    state: Dict[str, float],
    effects: Dict[str, float],
    account: str,
    side: str,
    amount: float,
) -> None:
    spec = LEDGER_ACCOUNTS.get(account)
    if not spec:
        return
    kind = spec["kind"]
    state_key = spec.get("state") or ""
    if account == "capital":
        return
    if account == "current_earnings":
        delta = amount if side == "credit" else -amount
        effects["result_accum_delta"] = effects.get("result_accum_delta", 0.0) + delta
        movement_key = "result_accum_journal_increase" if delta > 0 else "result_accum_journal_decrease"
        effects[movement_key] = effects.get(movement_key, 0.0) + abs(delta)
        return

    if kind == "asset":
        delta = amount if side == "debit" else -amount
    elif kind == "contra_asset":
        delta = amount if side == "debit" else -amount
    else:
        delta = amount if side == "credit" else -amount

    if state_key:
        state[state_key] = _round(state.get(state_key, 0.0) + delta)
        movement_key = f"{state_key}_journal_increase" if delta > 0 else f"{state_key}_journal_decrease"
        effects[movement_key] = effects.get(movement_key, 0.0) + abs(delta)
        if state_key == "cash":
            cash_key = "cash_journal_increase" if delta > 0 else "cash_journal_decrease"
            effects[cash_key] = effects.get(cash_key, 0.0) + abs(delta)


def _normalize_ledger_account(value: Any) -> str:
    canonical = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if canonical in LEDGER_ACCOUNTS:
        return canonical
    key = _plain(value)
    aliases = {
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
    if key in LEDGER_ACCOUNTS:
        return key
    return aliases.get(key, "")


def _normalize_account(value: Any) -> str:
    key = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "retiro_patrimonio": "owner_withdrawal",
        "retiro": "owner_withdrawal",
        "aporte_capital": "capital_contribution",
        "capital": "capital_contribution",
        "ajuste_compras": "purchase_adjustment",
        "ajuste_de_compras": "purchase_adjustment",
        "purchase": "purchase_adjustment",
        "compras": "purchase_adjustment",
        "financiamiento_proveedor": "supplier_financing",
        "financiamiento_proveedores": "supplier_financing",
        "compras_financiadas": "supplier_financing",
        "credito_proveedor": "supplier_financing",
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
        "distribucion_resultados": "retained_earnings_distribution",
        "retiro_resultados": "retained_earnings_distribution",
        "resultados_acumulados": "retained_earnings_distribution",
        "retained_earnings_distribution": "retained_earnings_distribution",
        "reclasificacion_capital": "capital_reclassification",
        "capital_reclassification": "capital_reclassification",
    }
    allowed = {
        "owner_withdrawal", "capital_contribution",
        "purchase_adjustment", "supplier_financing",
        "retained_earnings_distribution", "capital_reclassification",
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


def _override_pct(override: Mapping[str, Any], key: str, default_decimal: float) -> float:
    if key not in override or override.get(key) in {None, ""}:
        return default_decimal
    return _pct(override.get(key), default_decimal * 100.0)


def _index_income_overrides(raw: Any) -> Dict[str, Dict[str, float]]:
    if isinstance(raw, Mapping):
        items = []
        for month, values in raw.items():
            record = dict(values or {}) if isinstance(values, Mapping) else {}
            record["month"] = month
            items.append(record)
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    out: Dict[str, Dict[str, float]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        month = _normalize_month_key(item.get("month") or item.get("mes"))
        if not month:
            continue
        values: Dict[str, float] = {}
        if item.get("cost_pct") not in {None, ""}:
            values["cost_pct"] = _to_float(item.get("cost_pct"), 70.0)
        if item.get("porcentaje_costo") not in {None, ""}:
            values["cost_pct"] = _to_float(item.get("porcentaje_costo"), 70.0)
        if item.get("cost_variability_pct") not in {None, ""}:
            values["cost_variability_pct"] = _to_float(item.get("cost_variability_pct"), 5.0)
        if item.get("variabilidad_costo_pct") not in {None, ""}:
            values["cost_variability_pct"] = _to_float(item.get("variabilidad_costo_pct"), 5.0)
        if values:
            out[month] = values
    return out


def _normalize_month_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) >= 7 and text[:7].count("-") == 1 and text[:7].replace("-", "").isdigit():
        return text[:7]
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return ""
    return pd.Timestamp(parsed).strftime("%Y-%m")


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

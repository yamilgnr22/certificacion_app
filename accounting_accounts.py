from __future__ import annotations

import unicodedata


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
    "revenue": "Ingresos",
    "cogs": "Costo de Venta",
    "exp_salaries": "Sueldos y Salarios",
    "exp_services": "Servicios Publicos",
    "depreciation_expense": "Gasto por Depreciacion",
    "financial_expenses": "Gastos Financieros",
    "exp_alcaldia_dgi": "Alcaldia y DGI",
    "exp_fuel": "Combustible",
    "exp_advertising": "Publicidad",
    "exp_maintenance": "Mantenimientos",
    "exp_rent": "Renta",
    "exp_insurance": "Seguros",
    "exp_other": "Otros Gastos",
}


def plain_account_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower().strip()


def normalize_account(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw in LEDGER_ACCOUNT_LABELS:
        return raw
    lowered = plain_account_text(raw)
    if lowered in LEDGER_ACCOUNT_ALIASES:
        return LEDGER_ACCOUNT_ALIASES[lowered]
    for key, label in LEDGER_ACCOUNT_LABELS.items():
        if plain_account_text(label) == lowered:
            return key
    return raw


def account_label(account: str) -> str:
    return LEDGER_ACCOUNT_LABELS.get(account, account or "")

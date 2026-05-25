from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, Mapping


ACCOUNT_TYPES = {
    "Efectivo y Equivalentes de Efectivo": "asset",
    "Cuentas por Cobrar Clientes": "asset",
    "Inventarios": "asset",
    "Bienes Inmuebles": "asset",
    "Mobiliario y Equipos": "asset",
    "Vehiculos": "asset",
    "Depreciacion Acumulada": "contra_asset",
    "Tarjetas de Credito": "liability",
    "Proveedores": "liability",
    "Impuestos por Pagar": "liability",
    "Gastos Acumulados por pagar": "liability",
    "Creditos Hipotecarios": "liability",
    "Creditos Consumo": "liability",
    "Creditos Personales": "liability",
    "Creditos Prendarios": "liability",
    "Creditos Comerciales": "liability",
    "Capital": "equity",
    "Resultados Acumulados": "equity",
    "Resultados del Ejercicio": "equity",
    "Ingresos": "revenue",
    "Costo de Venta": "expense",
    "Gastos Operativos": "expense",
    "Gastos Financieros": "expense",
    "Gasto por Depreciacion": "expense",
}


BALANCE_ACCOUNTS = {
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
    "retained_earnings": "Resultados Acumulados",
}


LOAN_ACCOUNTS = {
    "loan_mortgage": "Creditos Hipotecarios",
    "loan_consumo": "Creditos Consumo",
    "loan_personal": "Creditos Personales",
    "loan_pledge": "Creditos Prendarios",
    "loan_commercial": "Creditos Comerciales",
}


def build_accounting(
    payload: Mapping[str, Any],
    monthly: list[Mapping[str, Any]],
    months: list[Any],
    opening_balances: Mapping[str, float],
) -> Dict[str, Any]:
    account_types = _account_types(payload)
    system_vouchers = _build_system_vouchers(payload, monthly, months, opening_balances, account_types)
    saved_vouchers = _saved_vouchers(payload)
    vouchers = [*system_vouchers, *saved_vouchers]
    ledger = _build_ledger(vouchers, account_types)
    traces = _build_traces(ledger)
    return {
        "vouchers": vouchers,
        "saved_vouchers": saved_vouchers,
        "ledger": ledger,
        "trace": traces,
        "accounts": sorted({line["account"] for line in ledger}),
        "summary": {
            "voucher_count": len(vouchers),
            "ledger_line_count": len(ledger),
            "saved_voucher_count": len(saved_vouchers),
        },
    }


def get_account_ledger(accounting: Mapping[str, Any], account: str) -> list[Dict[str, Any]]:
    account = str(account or "")
    return [line for line in accounting.get("ledger", []) if line.get("account") == account]


def get_trace(accounting: Mapping[str, Any], account: str, month: str) -> Dict[str, Any]:
    key = f"{account}|{str(month or '')[:7]}"
    return dict((accounting.get("trace") or {}).get(key) or {
        "account": account,
        "month": str(month or "")[:7],
        "opening_balance": 0,
        "debits": 0,
        "credits": 0,
        "closing_balance": 0,
        "entries": [],
    })


def reverse_voucher(voucher: Mapping[str, Any], *, voucher_id: str | None = None) -> Dict[str, Any]:
    original_id = str(voucher.get("voucher_id") or "")
    month = str(voucher.get("month") or "")[:7]
    lines = []
    for line in voucher.get("lines") or []:
        debit = _round(line.get("debit"))
        credit = _round(line.get("credit"))
        lines.append({
            "account": line.get("account"),
            "debit": credit,
            "credit": debit,
            "currency": line.get("currency") or "nio",
            "reference": f"Reverso de {original_id}",
        })
    return _voucher(
        voucher_id or f"REV-{original_id}",
        month,
        "reversal",
        f"Reverso de {original_id}",
        lines,
        source="system",
        reference_voucher_id=original_id,
    )


def _build_system_vouchers(
    payload: Mapping[str, Any],
    monthly: list[Mapping[str, Any]],
    months: list[Any],
    opening_balances: Mapping[str, float],
    account_types: Mapping[str, str],
) -> list[Dict[str, Any]]:
    vouchers: list[Dict[str, Any]] = []
    counters: Dict[str, int] = defaultdict(int)
    if months:
        opening_month = _month_key(months[0])
        opening = _opening_voucher(opening_month, opening_balances, account_types)
        if opening:
            opening["voucher_id"] = _next_voucher_id(counters, opening_month)
            vouchers.append(opening)

    for item in monthly:
        month = _month_key(item["month"])
        for voucher in _monthly_vouchers(item, month):
            voucher["voucher_id"] = _next_voucher_id(counters, month)
            vouchers.append(voucher)

    return vouchers


def _opening_voucher(month: str, balances: Mapping[str, float], account_types: Mapping[str, str]) -> Dict[str, Any]:
    lines: list[Dict[str, Any]] = []
    for key, account in BALANCE_ACCOUNTS.items():
        amount = _round(balances.get(key))
        if abs(amount) < 0.5:
            continue
        if account == "Depreciacion Acumulada":
            lines.append(_line(account, credit=abs(amount), reference="Saldo inicial"))
        elif account_types.get(account) == "asset":
            lines.append(_line(account, debit=amount, reference="Saldo inicial"))
        else:
            lines.append(_line(account, credit=amount, reference="Saldo inicial"))

    debit_total = sum(line["debit"] for line in lines)
    credit_total = sum(line["credit"] for line in lines)
    diff = _round(debit_total - credit_total)
    if diff > 0:
        lines.append(_line("Capital", credit=diff, reference="Capital de apertura"))
    elif diff < 0:
        lines.append(_line("Capital", debit=abs(diff), reference="Capital de apertura"))
    return _voucher("", month, "opening", "Comprobante de apertura de saldos iniciales", lines)


def _monthly_vouchers(item: Mapping[str, Any], month: str) -> Iterable[Dict[str, Any]]:
    cash_sales = _round(item.get("cash_sales"))
    credit_sales = _round(item.get("credit_sales"))
    revenue = _round(item.get("revenue"))
    if revenue:
        yield _voucher("", month, "sales", "Ventas mensuales contado y credito", [
            _line("Efectivo y Equivalentes de Efectivo", debit=cash_sales, reference="Ventas contado"),
            _line("Cuentas por Cobrar Clientes", debit=credit_sales, reference="Ventas credito"),
            _line("Ingresos", credit=revenue, reference="Ingresos del mes"),
        ])

    collections = _round(item.get("collections"))
    if collections:
        yield _voucher("", month, "collections", "Recuperacion de cartera", [
            _line("Efectivo y Equivalentes de Efectivo", debit=collections),
            _line("Cuentas por Cobrar Clientes", credit=collections),
        ])

    cogs = _round(item.get("cogs"))
    if cogs:
        yield _voucher("", month, "cogs", "Reconocimiento de costo de venta", [
            _line("Costo de Venta", debit=cogs),
            _line("Inventarios", credit=cogs),
        ])

    purchases = _round(item.get("purchases"))
    if purchases:
        supplier_financing = _round(item.get("supplier_financing"))
        cash_purchases = _round(item.get("cash_purchases"))
        lines = [_line("Inventarios", debit=purchases, reference="Compras del mes")]
        if cash_purchases:
            lines.append(_line("Efectivo y Equivalentes de Efectivo", credit=cash_purchases, reference="Compras pagadas"))
        if supplier_financing:
            lines.append(_line("Proveedores", credit=supplier_financing, reference="Compras financiadas"))
        yield _voucher("", month, "purchases", "Compras de inventario", lines)

    cash_expenses = _round(item.get("cash_operating_expenses"))
    if cash_expenses:
        yield _voucher("", month, "expenses", "Gastos operativos desembolsables", [
            _line("Gastos Operativos", debit=cash_expenses),
            _line("Efectivo y Equivalentes de Efectivo", credit=cash_expenses),
        ])

    depreciation = _round(item.get("depreciation"))
    if depreciation:
        yield _voucher("", month, "depreciation", "Depreciacion mensual", [
            _line("Gasto por Depreciacion", debit=depreciation),
            _line("Depreciacion Acumulada", credit=depreciation),
        ])

    for prefix, account in LOAN_ACCOUNTS.items():
        new_amount = _round(item.get(f"{prefix}_new"))
        payment = _round(item.get(f"{prefix}_payment"))
        if new_amount:
            yield _voucher("", month, "loan", f"Nuevo {account.lower()}", [
                _line("Efectivo y Equivalentes de Efectivo", debit=new_amount),
                _line(account, credit=new_amount),
            ])
        if payment:
            yield _voucher("", month, "loan", f"Abono {account.lower()}", [
                _line(account, debit=payment),
                _line("Efectivo y Equivalentes de Efectivo", credit=payment),
            ])

    liability_payments = [
        ("Tarjetas de Credito", item.get("credit_card_payment")),
        ("Proveedores", item.get("supplier_payment")),
        ("Impuestos por Pagar", item.get("taxes_payment")),
        ("Gastos Acumulados por pagar", item.get("accrued_payment")),
    ]
    for account, amount in liability_payments:
        amount = _round(amount)
        if amount:
            yield _voucher("", month, "payments", f"Pago de {account.lower()}", [
                _line(account, debit=amount),
                _line("Efectivo y Equivalentes de Efectivo", credit=amount),
            ])

    asset_lines = [
        ("Bienes Inmuebles", item.get("additions_real_estate")),
        ("Mobiliario y Equipos", item.get("additions_equipment")),
        ("Vehiculos", item.get("additions_vehicles")),
    ]
    for account, amount in asset_lines:
        amount = _round(amount)
        if amount:
            yield _voucher("", month, "asset_purchase", f"Compra de {account.lower()}", [
                _line(account, debit=amount),
                _line("Efectivo y Equivalentes de Efectivo", credit=amount),
            ])

    yield from _equity_vouchers(item, month)
    yield from _journal_entry_vouchers(item, month)
    closing = _income_close_voucher(item, month)
    if closing:
        yield closing


def _equity_vouchers(item: Mapping[str, Any], month: str) -> Iterable[Dict[str, Any]]:
    amount = _round(item.get("capital_contribution"))
    if amount:
        yield _voucher("", month, "equity", "Aporte de capital", [
            _line("Efectivo y Equivalentes de Efectivo", debit=amount),
            _line("Capital", credit=amount),
        ])
    amount = _round(item.get("owner_withdrawal"))
    if amount:
        yield _voucher("", month, "equity", "Retiro de patrimonio contra capital", [
            _line("Capital", debit=amount),
            _line("Efectivo y Equivalentes de Efectivo", credit=amount),
        ])
    amount = _round(item.get("retained_earnings_distribution"))
    if amount:
        yield _voucher("", month, "equity", "Distribucion de resultados acumulados", [
            _line("Resultados Acumulados", debit=amount),
            _line("Efectivo y Equivalentes de Efectivo", credit=amount),
        ])
    amount = _round(item.get("capital_reclassification"))
    if amount > 0:
        yield _voucher("", month, "equity", "Reclasificacion de capital a resultados acumulados", [
            _line("Capital", debit=amount),
            _line("Resultados Acumulados", credit=amount),
        ])
    elif amount < 0:
        amount = abs(amount)
        yield _voucher("", month, "equity", "Reclasificacion de resultados acumulados a capital", [
            _line("Resultados Acumulados", debit=amount),
            _line("Capital", credit=amount),
        ])


def _journal_entry_vouchers(item: Mapping[str, Any], month: str) -> Iterable[Dict[str, Any]]:
    for entry in item.get("journal_entries") or []:
        entry_lines = entry.get("lines")
        if isinstance(entry_lines, list) and entry_lines:
            lines = [
                _line(
                    _account_label(line.get("account")),
                    debit=_round(line.get("debit")),
                    credit=_round(line.get("credit")),
                    reference=str(line.get("reference") or ""),
                )
                for line in entry_lines
                if isinstance(line, Mapping)
            ]
        else:
            amount = _round(entry.get("amount_nio") or entry.get("amount"))
            if not amount:
                continue
            debit = _account_label(entry.get("debit_account"))
            credit = _account_label(entry.get("credit_account"))
            lines = [_line(debit, debit=amount), _line(credit, credit=amount)]
        if not lines:
            continue
        entry_type = str(entry.get("entry_type") or "chat_adjustment")
        voucher_type = "year_close" if entry_type == "year_close_transfer" else "chat_adjustment"
        yield _voucher(
            "",
            month,
            voucher_type,
            str(entry.get("message") or entry.get("description") or "Partida del chat financiero"),
            lines,
            source=str(entry.get("source") or "chat_financiero"),
            instruction_id=str(entry.get("instruction_id") or ""),
        )


def _income_close_voucher(item: Mapping[str, Any], month: str) -> Dict[str, Any] | None:
    revenue = _round(item.get("revenue"))
    cogs = _round(item.get("cogs"))
    total_expenses = _round(item.get("total_expenses"))
    net_income = _round(item.get("net_income"))
    if not any([revenue, cogs, total_expenses, net_income]):
        return None
    lines = [
        _line("Ingresos", debit=revenue, reference="Cierre mensual de ingresos"),
        _line("Costo de Venta", credit=cogs, reference="Cierre mensual de costos"),
        _line("Gastos Operativos", credit=max(total_expenses, 0), reference="Cierre mensual de gastos"),
    ]
    if net_income >= 0:
        lines.append(_line("Resultados del Ejercicio", credit=net_income, reference="Utilidad del mes"))
    else:
        lines.append(_line("Resultados del Ejercicio", debit=abs(net_income), reference="Perdida del mes"))
    return _voucher("", month, "year_close", "Cierre mensual de resultado", lines)


def _build_ledger(vouchers: list[Mapping[str, Any]], account_types: Mapping[str, str]) -> list[Dict[str, Any]]:
    lines: list[Dict[str, Any]] = []
    running: Dict[str, float] = defaultdict(float)
    for voucher in vouchers:
        if voucher.get("status") == "reversed":
            continue
        for idx, line in enumerate(voucher.get("lines") or [], start=1):
            account = str(line.get("account") or "")
            debit = _round(line.get("debit"))
            credit = _round(line.get("credit"))
            delta = _normal_delta(account, debit, credit, account_types)
            running[account] = _round(running[account] + delta)
            lines.append({
                "voucher_id": voucher.get("voucher_id"),
                "month": voucher.get("month"),
                "date": voucher.get("date"),
                "type": voucher.get("type"),
                "source": voucher.get("source"),
                "description": voucher.get("description"),
                "account": account,
                "line_no": idx,
                "debit": debit,
                "credit": credit,
                "signed_amount": delta,
                "running_balance": running[account],
                "reference": line.get("reference") or "",
            })
    return lines


def _build_traces(ledger: list[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    traces: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    accounts = sorted({str(line.get("account") or "") for line in ledger})
    months = sorted({str(line.get("month") or "")[:7] for line in ledger})
    for line in ledger:
        grouped[(str(line.get("account") or ""), str(line.get("month") or "")[:7])].append(line)
    for account in accounts:
        opening = 0.0
        for month in months:
            entries = list(grouped.get((account, month), []))
            debits = _round(sum(float(line.get("debit") or 0) for line in entries))
            credits = _round(sum(float(line.get("credit") or 0) for line in entries))
            closing = entries[-1].get("running_balance") if entries else opening
            trace = {
                "account": account,
                "month": month,
                "opening_balance": _round(opening),
                "debits": debits,
                "credits": credits,
                "closing_balance": _round(closing),
                "entries": [dict(line) for line in entries],
            }
            traces[f"{account}|{month}"] = trace
            opening = _round(closing)
    return traces


def _saved_vouchers(payload: Mapping[str, Any]) -> list[Dict[str, Any]]:
    accounting = dict((payload or {}).get("accounting") or {})
    return [dict(voucher) for voucher in accounting.get("vouchers") or [] if isinstance(voucher, Mapping)]


def _voucher(
    voucher_id: str,
    month: str,
    voucher_type: str,
    description: str,
    lines: list[Mapping[str, Any]],
    *,
    source: str = "system",
    instruction_id: str = "",
    reference_voucher_id: str = "",
) -> Dict[str, Any]:
    clean_lines = [dict(line) for line in lines if _round(line.get("debit")) or _round(line.get("credit"))]
    debit_total = _round(sum(line["debit"] for line in clean_lines))
    credit_total = _round(sum(line["credit"] for line in clean_lines))
    return {
        "voucher_id": voucher_id,
        "month": month,
        "date": f"{month}-01" if month else "",
        "type": voucher_type,
        "source": source,
        "instruction_id": instruction_id,
        "description": description,
        "status": "applied",
        "reference_voucher_id": reference_voucher_id,
        "debit_total": debit_total,
        "credit_total": credit_total,
        "balanced": abs(debit_total - credit_total) <= 1,
        "lines": clean_lines,
    }


def _line(account: str, *, debit: Any = 0, credit: Any = 0, currency: str = "nio", reference: str = "") -> Dict[str, Any]:
    return {
        "account": account,
        "debit": _round(debit),
        "credit": _round(credit),
        "currency": currency,
        "reference": reference,
    }


def _next_voucher_id(counters: Dict[str, int], month: str) -> str:
    year = str(month or "0000")[:4]
    counters[year] += 1
    return f"CD-{year}-{counters[year]:04d}"


def _normal_delta(account: str, debit: float, credit: float, account_types: Mapping[str, str] | None = None) -> float:
    account_type = (account_types or ACCOUNT_TYPES).get(account, "asset")
    if account_type in {"asset", "expense"}:
        return _round(debit - credit)
    return _round(credit - debit)


def _account_types(payload: Mapping[str, Any]) -> dict[str, str]:
    account_types = dict(ACCOUNT_TYPES)
    accounting = dict((payload or {}).get("accounting") or {})
    for account in accounting.get("dynamic_accounts") or []:
        if not isinstance(account, Mapping):
            continue
        code = str(account.get("code") or "").strip()
        name = str(account.get("name") or account.get("label") or account.get("code") or "").strip()
        if not name and not code:
            continue
        runtime_type = _runtime_account_type(account.get("account_type"))
        if name:
            account_types[name] = runtime_type
        if code:
            account_types[code] = runtime_type
    return account_types


def _runtime_account_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"activo", "asset"}:
        return "asset"
    if raw in {"pasivo", "liability"}:
        return "liability"
    if raw in {"patrimonio", "equity"}:
        return "equity"
    if raw in {"ingreso", "revenue"}:
        return "revenue"
    if raw in {"costo", "gasto", "expense"}:
        return "expense"
    return "asset"


def _account_label(account_key: Any) -> str:
    mapping = {
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
    return mapping.get(str(account_key or ""), str(account_key or ""))


def _month_key(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m")
    return str(value or "")[:7]


def _round(value: Any) -> float:
    try:
        return float(round(float(value or 0), 0))
    except (TypeError, ValueError):
        return 0.0

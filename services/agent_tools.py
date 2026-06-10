from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from accounting_accounts import account_label, normalize_account
from accounting_model import get_account_ledger, get_trace
from model_cache import cached_build_financial_model as build_financial_model


ToolHandler = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    version: str
    mutates: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: ToolHandler | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("AgentTool requiere name.")
        if not self.version:
            raise ValueError(f"AgentTool {self.name} requiere version.")


class AgentToolRegistry:
    """Registry versionado de herramientas disponibles para el asistente.

    Las tools mutantes se declaran aqui para versionado/auditoria, pero su
    ejecucion sigue pasando por propuestas en AgentCommandService.
    """

    def __init__(self) -> None:
        self.tools = {
            "explain_balance": AgentTool(
                "explain_balance",
                "1.1.0",
                False,
                {"account": "string", "month": "YYYY-MM"},
                {"response_type": "answer", "data": "balance_trace"},
                self._explain_balance,
            ),
            "show_ledger": AgentTool(
                "show_ledger",
                "1.1.0",
                False,
                {"account": "string", "start_month": "YYYY-MM?", "end_month": "YYYY-MM?"},
                {"response_type": "answer", "data": "ledger_rows"},
                self._show_ledger,
            ),
            "show_voucher": AgentTool(
                "show_voucher",
                "1.1.0",
                False,
                {"voucher_id": "string"},
                {"response_type": "answer", "data": "voucher"},
                self._show_voucher,
            ),
            "get_account_balance": AgentTool(
                "get_account_balance",
                "1.0.0",
                False,
                {"account": "string", "month": "YYYY-MM"},
                {"response_type": "answer", "data": "account_balance"},
                self._get_account_balance,
            ),
            "get_ledger": AgentTool(
                "get_ledger",
                "1.0.0",
                False,
                {"account": "string", "start_month": "YYYY-MM?", "end_month": "YYYY-MM?"},
                {"response_type": "answer", "data": "ledger_rows"},
                self._show_ledger,
            ),
            "get_period_summary": AgentTool(
                "get_period_summary",
                "1.0.0",
                False,
                {"month": "YYYY-MM?"},
                {"response_type": "answer", "data": "period_summary"},
                self._get_period_summary,
            ),
            "convert_currency": AgentTool(
                "convert_currency",
                "1.0.0",
                False,
                {"amount": "number", "from_currency": "USD|NIO", "to_currency": "USD|NIO"},
                {"response_type": "answer", "data": "currency_conversion"},
                self._convert_currency,
            ),
            "compute_target_delta": AgentTool(
                "compute_target_delta",
                "1.0.0",
                False,
                {"account": "string", "month": "YYYY-MM", "target_amount": "number", "currency": "USD|NIO"},
                {"response_type": "answer", "data": "target_delta"},
                self._compute_target_delta,
            ),
            "compute_target_distribution": AgentTool(
                "compute_target_distribution",
                "1.1.0",
                False,
                {"months": "YYYY-MM[]", "average": "number", "overrides": "object", "variability_pct": "number"},
                {"response_type": "answer", "data": "target_distribution"},
                self._compute_target_distribution,
            ),
            "navigate": AgentTool(
                "navigate",
                "1.0.0",
                False,
                {"target": "string"},
                {"response_type": "navigation"},
                self._navigate,
            ),
            "recalcular_preview": AgentTool(
                "recalcular_preview",
                "1.0.0",
                False,
                {},
                {"response_type": "answer"},
                self._recalcular_preview,
            ),
            "reverse_voucher": self._mutating_tool("reverse_voucher"),
            "correct_voucher": self._mutating_tool("correct_voucher"),
            "journal_entry": self._mutating_tool("journal_entry"),
            "account_transfer": self._mutating_tool("account_transfer"),
            "year_close_transfer": self._mutating_tool("year_close_transfer"),
            "assumption_change": self._mutating_tool("assumption_change"),
            "monthly_override": self._mutating_tool("monthly_override"),
            "create_account": self._mutating_tool("create_account"),
            "compound_plan": self._mutating_tool("compound_plan"),
            "target_balance_adjustment": self._mutating_tool("target_balance_adjustment"),
            "guardar_payload": self._mutating_tool("guardar_payload"),
            "finalizar_periodo": self._mutating_tool("finalizar_periodo"),
            "generar_documento": self._mutating_tool("generar_documento"),
            "plan_multi_target_balance": self._mutating_tool("plan_multi_target_balance"),
            "plan_non_negative_account": self._mutating_tool("plan_non_negative_account"),
            "plan_target_utility": self._mutating_tool("plan_target_utility"),
            "plan_multi_account_target_balance": self._mutating_tool("plan_multi_account_target_balance"),
        }

    @staticmethod
    def _mutating_tool(name: str) -> AgentTool:
        return AgentTool(
            name=name,
            version="1.0.0",
            mutates=True,
            input_schema={},
            output_schema={"response_type": "proposal_or_system"},
            handler=None,
        )

    def versions(self) -> dict[str, str]:
        return {name: tool.version for name, tool in self.tools.items()}

    def version_for(self, name: str) -> str | None:
        tool = self.tools.get(str(name or ""))
        return tool.version if tool else None

    def versions_used(self, names: list[str] | tuple[str, ...] | set[str]) -> dict[str, str]:
        return {name: self.tools[name].version for name in names if name in self.tools}

    def normalize_account(self, value: str | None) -> str:
        return normalize_account(value)

    def run(self, intent: str, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        intent = str(intent or "").strip()
        tool = self.tools.get(intent)
        if not tool or not tool.handler:
            return {
                "response_type": "question",
                "assistant_message": "Puedo ayudarte, pero necesito que aclares si quieres consultar saldo, ver mayor, ver comprobante o navegar.",
                "ui_actions": [],
                "data": {},
            }
        result = tool.handler(payload, args)
        result.setdefault("tool_name", tool.name)
        result.setdefault("tool_version", tool.version)
        return result

    def _explain_balance(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        account = _readonly_account(self.normalize_account(str(args.get("account") or "")))
        month = str(args.get("month") or "").strip()[:7]
        result = build_financial_model(payload)
        if not month:
            months = result.summary.get("all_months") or result.summary.get("months") or []
            month = str(months[-1]) if months else ""
        account_name = account_label(account)
        trace = get_trace(result.accounting, account_name or account, month)
        if not trace:
            return {
                "response_type": "question",
                "assistant_message": f"No encontre movimientos para {account_name or 'esa cuenta'} en {month or 'ese mes'}.",
                "ui_actions": [],
                "data": {"account": account, "account_label": account_name, "month": month, "entries": []},
            }
        entries = [_ledger_row(entry) for entry in (trace.get("entries") or [])]
        top_lines = [
            f"{entry['voucher_id']}: {entry['description']} | Debe {_money(entry['debit'])} | Haber {_money(entry['credit'])}"
            for entry in entries[:6]
        ]
        message = (
            f"{account_name} en {month}: saldo inicial {_money(trace.get('opening_balance'))}, "
            f"debe {_money(trace.get('debits'))}, haber {_money(trace.get('credits'))}, "
            f"saldo final {_money(trace.get('closing_balance'))}."
        )
        if top_lines:
            message += "\n" + "\n".join(top_lines)
            message += f"\nMostre {min(len(entries), 6)} de {len(entries)} comprobante(s)."
        return {
            "response_type": "answer",
            "assistant_message": message,
            "ui_actions": [{"type": "select_account", "account": account}, {"type": "scroll_to", "target": "accounting"}],
            "data": {
                "kind": "balance_explanation",
                "account": account,
                "account_label": account_name,
                "month": month,
                "opening_balance": trace.get("opening_balance") or 0,
                "debits": trace.get("debits") or 0,
                "credits": trace.get("credits") or 0,
                "closing_balance": trace.get("closing_balance") or 0,
                "entries": entries,
            },
        }

    def _show_ledger(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        account = _readonly_account(self.normalize_account(str(args.get("account") or "")))
        account_name = account_label(account)
        start_month = str(args.get("start_month") or "").strip()[:7]
        end_month = str(args.get("end_month") or "").strip()[:7]
        result = build_financial_model(payload)
        rows = [
            _ledger_row(row)
            for row in get_account_ledger(result.accounting, account_name or account)
            if (not start_month or str(row.get("month") or "")[:7] >= start_month)
            and (not end_month or str(row.get("month") or "")[:7] <= end_month)
        ]
        rows.sort(key=lambda row: (row["date"], row["voucher_id"], row["line_no"]))
        range_label = ""
        if start_month or end_month:
            range_label = f" ({start_month or 'inicio'} a {end_month or 'fin'})"
        return {
            "response_type": "answer",
            "assistant_message": f"Mayor de {account_name}{range_label}: {len(rows)} movimiento(s).",
            "ui_actions": [{"type": "select_account", "account": account}, {"type": "scroll_to", "target": "ledger"}],
            "data": {
                "kind": "ledger",
                "account": account,
                "account_label": account_name,
                "start_month": start_month,
                "end_month": end_month,
                "rows": rows,
            },
        }

    def _show_voucher(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        voucher_id = str(args.get("voucher_id") or "").strip().upper()
        result = build_financial_model(payload)
        voucher = next(
            (dict(v) for v in result.accounting.get("vouchers", []) if str(v.get("voucher_id") or "").upper() == voucher_id),
            None,
        )
        if not voucher:
            return {
                "response_type": "question",
                "assistant_message": f"No encontre el comprobante {voucher_id}.",
                "ui_actions": [],
                "data": {"kind": "voucher", "voucher_id": voucher_id, "found": False},
            }
        lines = [_voucher_line(line) for line in (voucher.get("lines") or [])]
        line_text = [f"{line['account']}: Debe {_money(line['debit'])}, Haber {_money(line['credit'])}" for line in lines]
        persisted_vouchers = _saved_vouchers(payload)
        reference_voucher_id = str(voucher.get("reference_voucher_id") or "")
        reversed_by = ""
        if str(voucher.get("type") or "").lower() != "reversal":
            reversed_by = _find_reversal_for(persisted_vouchers, voucher_id)
        reference_text = ""
        if reference_voucher_id:
            reference_text = f"\nReversa a {reference_voucher_id}."
        elif reversed_by:
            reference_text = f"\nReversado por {reversed_by}."
        return {
            "response_type": "answer",
            "assistant_message": f"{voucher_id} ({voucher.get('month')}): {voucher.get('description')}\n" + "\n".join(line_text) + reference_text,
            "ui_actions": [{"type": "select_voucher", "voucher_id": voucher_id}, {"type": "scroll_to", "target": "accounting"}],
            "data": {
                "kind": "voucher",
                "found": True,
                "voucher": {
                    "voucher_id": voucher.get("voucher_id"),
                    "month": voucher.get("month"),
                    "date": voucher.get("date"),
                    "type": voucher.get("type"),
                    "source": voucher.get("source"),
                    "description": voucher.get("description"),
                    "reference_voucher_id": reference_voucher_id,
                    "reversed_by": reversed_by,
                    "debit_total": voucher.get("debit_total") or 0,
                    "credit_total": voucher.get("credit_total") or 0,
                    "balanced": bool(voucher.get("balanced")),
                    "lines": lines,
                },
            },
        }

    def _get_account_balance(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        account = _readonly_account(self.normalize_account(str(args.get("account") or "")))
        month = str(args.get("month") or "").strip()[:7]
        result = build_financial_model(payload)
        if not month:
            months = result.summary.get("all_months") or result.summary.get("months") or []
            month = str(months[-1]) if months else ""
        account_name = account_label(account)
        trace = get_trace(result.accounting, account_name or account, month) or {}
        balance = trace.get("closing_balance") if trace else _statement_value_from_result(result, account_name, month)
        return {
            "response_type": "answer",
            "assistant_message": f"{account_name} en {month}: saldo final {_money(balance)}.",
            "ui_actions": [{"type": "select_account", "account": account}, {"type": "scroll_to", "target": "ledger"}],
            "data": {
                "kind": "account_balance",
                "account": account,
                "account_label": account_name,
                "month": month,
                "closing_balance": balance or 0,
            },
        }

    def _get_period_summary(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        month = str(args.get("month") or "").strip()[:7]
        result = build_financial_model(payload)
        months = result.summary.get("all_months") or result.summary.get("months") or []
        if not month:
            month = str(months[-1]) if months else ""
        summary = {
            "month": month,
            "cash": _statement_value_from_result(result, "Efectivo y Equivalentes de Efectivo", month),
            "inventory": _statement_value_from_result(result, "Inventarios", month),
            "accounts_receivable": _statement_value_from_result(result, "Cuentas por Cobrar Clientes", month),
            "suppliers": _statement_value_from_result(result, "Proveedores", month),
        }
        return {
            "response_type": "answer",
            "assistant_message": (
                f"Resumen {month}: caja {_money(summary['cash'])}, inventario {_money(summary['inventory'])}, "
                f"CxC {_money(summary['accounts_receivable'])}, proveedores {_money(summary['suppliers'])}."
            ),
            "ui_actions": [{"type": "scroll_to", "target": "accounting"}],
            "data": {"kind": "period_summary", **summary},
        }

    def _convert_currency(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        amount = _number(args.get("amount"))
        from_currency = str(args.get("from_currency") or args.get("from") or "nio").upper()
        to_currency = str(args.get("to_currency") or args.get("to") or "nio").upper()
        rate = _exchange_rate(payload)
        converted = amount
        if from_currency in {"USD", "DOLAR", "DOLARES"} and to_currency in {"NIO", "CORDOBA", "CORDOBAS", "C$"}:
            converted = amount * rate
        elif from_currency in {"NIO", "CORDOBA", "CORDOBAS", "C$"} and to_currency in {"USD", "DOLAR", "DOLARES"}:
            converted = amount / rate if rate else 0.0
        return {
            "response_type": "answer",
            "assistant_message": f"{_money(amount)} {from_currency} equivalen a {_money(converted)} {to_currency} con tasa {rate}.",
            "ui_actions": [],
            "data": {"kind": "currency_conversion", "amount": amount, "from_currency": from_currency, "to_currency": to_currency, "converted_amount": converted, "exchange_rate": rate},
        }

    def _compute_target_delta(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        account = _readonly_account(self.normalize_account(str(args.get("account") or args.get("target_account") or "")))
        month = str(args.get("month") or args.get("target_month") or "").strip()[:7]
        target_amount = _number(args.get("target_amount") or args.get("amount"))
        currency = str(args.get("currency") or "nio").upper()
        rate = _exchange_rate(payload)
        result = build_financial_model(payload)
        account_name = account_label(account)
        current = _statement_value_from_result(result, account_name, month)
        target_nio = target_amount * rate if currency in {"USD", "DOLAR", "DOLARES"} else target_amount
        delta = target_nio - current
        return {
            "response_type": "answer",
            "assistant_message": (
                f"{account_name} en {month}: saldo actual {_money(current)}, objetivo {_money(target_nio)}, "
                f"diferencia {_money(delta)}."
            ),
            "ui_actions": [{"type": "select_account", "account": account}],
            "data": {
                "kind": "target_delta",
                "account": account,
                "account_label": account_name,
                "month": month,
                "current_balance_nio": current,
                "target_amount_original": target_amount,
                "target_currency": currency,
                "target_amount_nio": target_nio,
                "delta_nio": delta,
                "exchange_rate": rate,
            },
        }

    def _compute_target_distribution(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        months = [str(m).strip()[:7] for m in (args.get("months") or []) if str(m or "").strip()]
        # No usar `or` para average porque 0 es valido (llevar cuenta a cero)
        raw_avg: Any = None
        for key in ("average", "target_average", "promedio"):
            if key in args and args[key] is not None:
                raw_avg = args[key]
                break
        average = _number(raw_avg) if raw_avg is not None else 0.0
        overrides = args.get("overrides") if isinstance(args.get("overrides"), Mapping) else {}
        variability_pct = _number(args.get("variability_pct") or args.get("variabilidad_pct") or 0)
        if not months or average < 0:
            return {
                "response_type": "question",
                "assistant_message": "Necesito meses y promedio no negativo para distribuir objetivos.",
                "ui_actions": [],
                "data": {},
            }
        # Clamp variabilidad razonable (0-50%); valores fuera de eso son casi seguro un error
        variability_pct = max(0.0, min(50.0, variability_pct))
        variability_frac = variability_pct / 100.0

        override_values = {str(k).strip()[:7]: _number(v) for k, v in overrides.items()}
        fixed_total = sum(v for v in override_values.values() if v > 0)
        free_months = [m for m in months if override_values.get(m, 0) <= 0]
        free_avg = (average * len(months) - fixed_total) / len(free_months) if free_months else average

        free_values: dict[str, float] = {}
        if free_months:
            if variability_frac > 0:
                # Seed deterministico: misma combinacion (meses + promedio + variabilidad) => mismos valores
                seed_source = f"{','.join(months)}|{average}|{variability_pct}|{','.join(sorted(override_values))}"
                rng = random.Random(hashlib.sha256(seed_source.encode("utf-8")).hexdigest())
                raw = [free_avg * (1.0 + rng.uniform(-variability_frac, variability_frac)) for _ in free_months]
                # Normalizar para que el promedio efectivo de los meses libres caiga exacto en free_avg
                actual_avg = sum(raw) / len(raw)
                scale = (free_avg / actual_avg) if actual_avg else 1.0
                free_values = {m: max(0.0, raw[i] * scale) for i, m in enumerate(free_months)}
            else:
                free_values = {m: free_avg for m in free_months}

        targets = [
            {
                "month": month,
                "target_amount": round(override_values[month], 2) if override_values.get(month, 0) > 0 else round(free_values.get(month, free_avg), 2),
            }
            for month in months
        ]
        return {
            "response_type": "answer",
            "assistant_message": "Distribucion calculada.",
            "ui_actions": [],
            "data": {"kind": "target_distribution", "targets": targets, "variability_pct": variability_pct},
        }

    def _navigate(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        target = str(args.get("target") or "accounting").strip()
        return {
            "response_type": "navigation",
            "assistant_message": "Listo, te llevo a la seccion solicitada.",
            "ui_actions": [{"type": "scroll_to", "target": target}],
            "data": {"kind": "navigation", "target": target},
        }

    def _recalcular_preview(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        result = build_financial_model(payload)
        months = result.summary.get("all_months") or result.summary.get("months") or []
        validations = result.validations or {}
        errors = validations.get("errors") or []
        warnings = validations.get("warnings") or []
        lines = [f"Recalculo listo. {len(months)} mes(es) en el modelo."]
        if errors:
            lines.append(f"Errores: {len(errors)}.")
        if warnings:
            lines.append(f"Advertencias: {len(warnings)}.")
        if not errors and not warnings:
            lines.append("Sin errores ni advertencias.")
        return {
            "response_type": "answer",
            "assistant_message": " ".join(lines),
            "ui_actions": [{"type": "scroll_to", "target": "summary"}],
            "data": {"kind": "preview_summary", "months": months, "errors": errors, "warnings": warnings},
        }


def _find_trace(trace_map: Mapping[str, Any], *, account: str, account_name: str, month: str) -> dict[str, Any]:
    return dict(
        trace_map.get(f"{account}|{month}")
        or trace_map.get(f"{account_name}|{month}")
        or {}
    )


def _readonly_account(account: str) -> str:
    aliases = {
        "61": "Gastos Operativos",
        "611": "Sueldos",
        "612": "Servicios",
        "613": "Depreciaciones",
    }
    raw = str(account or "").strip()
    return aliases.get(raw, raw)


def _same_account(value: Any, account: str, account_name: str) -> bool:
    raw = str(value or "").strip()
    return raw == account or raw == account_name or normalize_account(raw) == account


def _saved_vouchers(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    accounting = dict((payload or {}).get("accounting") or {})
    return [dict(voucher) for voucher in accounting.get("vouchers") or [] if isinstance(voucher, Mapping)]


def _find_reversal_for(vouchers: list[Mapping[str, Any]], original_voucher_id: str) -> str:
    original = str(original_voucher_id or "").upper()
    for voucher in vouchers:
        if str(voucher.get("type") or "").lower() != "reversal":
            continue
        if str(voucher.get("reference_voucher_id") or "").upper() == original:
            return str(voucher.get("voucher_id") or "")
    return ""


def _ledger_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "voucher_id": str(row.get("voucher_id") or ""),
        "month": str(row.get("month") or "")[:7],
        "date": str(row.get("date") or ""),
        "type": str(row.get("type") or ""),
        "source": str(row.get("source") or ""),
        "description": str(row.get("description") or ""),
        "account": str(row.get("account") or ""),
        "line_no": int(row.get("line_no") or 0),
        "debit": float(row.get("debit") or 0),
        "credit": float(row.get("credit") or 0),
        "signed_amount": float(row.get("signed_amount") or 0),
        "running_balance": float(row.get("running_balance") or 0),
        "reference": str(row.get("reference") or ""),
    }


def _voucher_line(line: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "account": str(line.get("account") or ""),
        "debit": float(line.get("debit") or 0),
        "credit": float(line.get("credit") or 0),
        "currency": str(line.get("currency") or "nio"),
        "reference": str(line.get("reference") or ""),
    }


def _money(value: Any) -> str:
    try:
        return f"{float(value or 0):,.0f}"
    except Exception:
        return "0"


def _number(value: Any) -> float:
    try:
        if isinstance(value, str):
            cleaned = value.replace(",", "").replace("USD", "").replace("C$", "").strip()
            if cleaned.lower().endswith("k"):
                return float(cleaned[:-1]) * 1000
            return float(cleaned)
        return float(value or 0)
    except Exception:
        return 0.0


def _exchange_rate(payload: Mapping[str, Any]) -> float:
    period = dict((payload or {}).get("period") or {})
    value = period.get("exchange_rate") or period.get("tasa_cambio") or (payload or {}).get("tasa_cambio")
    return _number(value) or 1.0


def _statement_value_from_result(result: Any, description: str, month: str) -> float:
    df = getattr(result, "df_esf_mensual_full", None)
    if df is None or not month:
        return 0.0
    try:
        col = month if month in df.columns else next((item for item in df.columns if str(item).startswith(month)), None)
        if col is None:
            return 0.0
        rows = df[df["Descripcion"] == description]
        if rows.empty:
            return 0.0
        return float(rows.iloc[0][col] or 0)
    except Exception:
        return 0.0

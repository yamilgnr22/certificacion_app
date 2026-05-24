from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from accounting_accounts import account_label, normalize_account
from financial_model import build_financial_model


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
            "journal_entry": self._mutating_tool("journal_entry"),
            "account_transfer": self._mutating_tool("account_transfer"),
            "year_close_transfer": self._mutating_tool("year_close_transfer"),
            "assumption_change": self._mutating_tool("assumption_change"),
            "create_account": self._mutating_tool("create_account"),
            "guardar_payload": self._mutating_tool("guardar_payload"),
            "finalizar_periodo": self._mutating_tool("finalizar_periodo"),
            "generar_documento": self._mutating_tool("generar_documento"),
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
        account = self.normalize_account(str(args.get("account") or ""))
        month = str(args.get("month") or "").strip()[:7]
        result = build_financial_model(payload)
        if not month:
            months = result.summary.get("all_months") or result.summary.get("months") or []
            month = str(months[-1]) if months else ""
        account_name = account_label(account)
        trace = _find_trace(result.accounting.get("trace") or {}, account=account, account_name=account_name, month=month)
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
        account = self.normalize_account(str(args.get("account") or ""))
        account_name = account_label(account)
        start_month = str(args.get("start_month") or "").strip()[:7]
        end_month = str(args.get("end_month") or "").strip()[:7]
        result = build_financial_model(payload)
        rows = [
            _ledger_row(row)
            for row in (result.accounting.get("ledger") or [])
            if _same_account(row.get("account"), account, account_name)
            and (not start_month or str(row.get("month") or "")[:7] >= start_month)
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
        return {
            "response_type": "answer",
            "assistant_message": f"{voucher_id} ({voucher.get('month')}): {voucher.get('description')}\n" + "\n".join(line_text),
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
                    "debit_total": voucher.get("debit_total") or 0,
                    "credit_total": voucher.get("credit_total") or 0,
                    "balanced": bool(voucher.get("balanced")),
                    "lines": lines,
                },
            },
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


def _same_account(value: Any, account: str, account_name: str) -> bool:
    raw = str(value or "").strip()
    return raw == account or raw == account_name or normalize_account(raw) == account


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

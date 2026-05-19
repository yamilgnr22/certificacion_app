from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from financial_model import build_financial_model
from model_chat import LEDGER_ACCOUNT_ALIASES, LEDGER_ACCOUNT_LABELS


@dataclass(frozen=True)
class AgentTool:
    name: str
    version: str


class AgentToolRegistry:
    tools = {
        "explain_balance": AgentTool("explain_balance", "1.0.0"),
        "show_ledger": AgentTool("show_ledger", "1.0.0"),
        "show_voucher": AgentTool("show_voucher", "1.0.0"),
        "navigate": AgentTool("navigate", "1.0.0"),
        "reverse_voucher": AgentTool("reverse_voucher", "1.0.0"),
        "journal_entry": AgentTool("journal_entry", "1.0.0"),
        "account_transfer": AgentTool("account_transfer", "1.0.0"),
        "year_close_transfer": AgentTool("year_close_transfer", "1.0.0"),
        "assumption_change": AgentTool("assumption_change", "1.0.0"),
        "create_account": AgentTool("create_account", "1.0.0"),
        "recalcular_preview": AgentTool("recalcular_preview", "1.0.0"),
        "guardar_payload": AgentTool("guardar_payload", "1.0.0"),
        "finalizar_periodo": AgentTool("finalizar_periodo", "1.0.0"),
        "generar_documento": AgentTool("generar_documento", "1.0.0"),
    }

    def versions(self) -> dict[str, str]:
        return {name: tool.version for name, tool in self.tools.items()}

    def normalize_account(self, value: str | None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        if raw in LEDGER_ACCOUNT_LABELS:
            return raw
        lowered = _plain(raw)
        if lowered in LEDGER_ACCOUNT_ALIASES:
            return LEDGER_ACCOUNT_ALIASES[lowered]
        for key, label in LEDGER_ACCOUNT_LABELS.items():
            if _plain(label) == lowered:
                return key
        return raw

    def run(self, intent: str, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        intent = str(intent or "").strip()
        if intent == "explain_balance":
            return self._explain_balance(payload, args)
        if intent == "show_ledger":
            return self._show_ledger(payload, args)
        if intent == "show_voucher":
            return self._show_voucher(payload, args)
        if intent == "navigate":
            return self._navigate(args)
        if intent == "recalcular_preview":
            return self._recalcular_preview(payload, args)
        return {
            "response_type": "question",
            "assistant_message": "Puedo ayudarte, pero necesito que aclares si quieres consultar saldo, ver mayor, ver comprobante o navegar.",
            "ui_actions": [],
        }

    def _explain_balance(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        account = self.normalize_account(str(args.get("account") or ""))
        month = str(args.get("month") or "").strip()[:7]
        result = build_financial_model(payload)
        if not month:
            months = result.summary.get("all_months") or result.summary.get("months") or []
            month = str(months[-1]) if months else ""
        account_label = LEDGER_ACCOUNT_LABELS.get(account, account)
        trace_map = result.accounting.get("trace") or {}
        trace = dict(
            trace_map.get(f"{account}|{month}")
            or trace_map.get(f"{account_label}|{month}")
            or {}
        )
        if not trace:
            return {
                "response_type": "question",
                "assistant_message": f"No encontre movimientos para {account_label or 'esa cuenta'} en {month or 'ese mes'}.",
                "ui_actions": [],
            }
        entries = trace.get("entries") or []
        lines = [
            f"{entry.get('voucher_id')}: {entry.get('description')} | Debe {_money(entry.get('debit'))} | Haber {_money(entry.get('credit'))}"
            for entry in entries[:6]
        ]
        extra = f"\nMostre {min(len(entries), 6)} de {len(entries)} comprobante(s)." if entries else ""
        return {
            "response_type": "answer",
            "assistant_message": (
                f"{account_label} en {month}: "
                f"saldo inicial {_money(trace.get('opening_balance'))}, "
                f"debe {_money(trace.get('debits'))}, haber {_money(trace.get('credits'))}, "
                f"saldo final {_money(trace.get('closing_balance'))}."
                + ("\n" + "\n".join(lines) if lines else "")
                + extra
            ),
            "ui_actions": [{"type": "select_account", "account": account}, {"type": "scroll_to", "target": "accounting"}],
        }

    def _show_ledger(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        account = self.normalize_account(str(args.get("account") or ""))
        return {
            "response_type": "navigation",
            "assistant_message": f"Te muestro el mayor de {LEDGER_ACCOUNT_LABELS.get(account, account)}.",
            "ui_actions": [{"type": "select_account", "account": account}, {"type": "scroll_to", "target": "ledger"}],
        }

    def _show_voucher(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        voucher_id = str(args.get("voucher_id") or "").strip().upper()
        result = build_financial_model(payload)
        voucher = next(
            (dict(v) for v in result.accounting.get("vouchers", []) if str(v.get("voucher_id") or "").upper() == voucher_id),
            None,
        )
        if not voucher:
            return {"response_type": "question", "assistant_message": f"No encontre el comprobante {voucher_id}.", "ui_actions": []}
        rows = [
            f"{line.get('account')}: Debe {_money(line.get('debit'))}, Haber {_money(line.get('credit'))}"
            for line in voucher.get("lines") or []
        ]
        return {
            "response_type": "answer",
            "assistant_message": f"{voucher_id} ({voucher.get('month')}): {voucher.get('description')}\n" + "\n".join(rows),
            "ui_actions": [{"type": "select_voucher", "voucher_id": voucher_id}, {"type": "scroll_to", "target": "accounting"}],
        }

    def _navigate(self, args: Mapping[str, Any]) -> dict[str, Any]:
        target = str(args.get("target") or "accounting").strip()
        return {
            "response_type": "navigation",
            "assistant_message": "Listo, te llevo a la seccion solicitada.",
            "ui_actions": [{"type": "scroll_to", "target": target}],
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
        }


def _money(value: Any) -> str:
    try:
        return f"{float(value or 0):,.0f}"
    except Exception:
        return "0"


def _plain(value: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower().strip()

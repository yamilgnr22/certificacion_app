from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional
from uuid import uuid4

from financial_model import build_financial_model, result_to_json
from model_chat import (
    CHAT_SOURCE,
    LEDGER_ACCOUNT_ALIASES,
    LEDGER_ACCOUNT_LABELS,
    ModelChatError,
    preview_chat_adjustment,
)


RESPONSE_ANSWER = "answer"
RESPONSE_PROPOSAL = "proposal"
RESPONSE_UI_ACTION = "ui_action"
RESPONSE_WORKFLOW = "workflow"
RESPONSE_CLARIFICATION = "clarification"
RESPONSE_ERROR = "error"


LABEL_TO_ACCOUNT = {label: key for key, label in LEDGER_ACCOUNT_LABELS.items()}
MONTHS_ES = {
    "enero": "01",
    "ene": "01",
    "febrero": "02",
    "feb": "02",
    "marzo": "03",
    "mar": "03",
    "abril": "04",
    "abr": "04",
    "mayo": "05",
    "may": "05",
    "junio": "06",
    "jun": "06",
    "julio": "07",
    "jul": "07",
    "agosto": "08",
    "ago": "08",
    "septiembre": "09",
    "setiembre": "09",
    "sept": "09",
    "sep": "09",
    "octubre": "10",
    "oct": "10",
    "noviembre": "11",
    "nov": "11",
    "diciembre": "12",
    "dic": "12",
}


class ChatCommandError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def handle_chat_command(
    payload: Mapping[str, Any],
    message: str,
    *,
    ui_context: Optional[Mapping[str, Any]] = None,
    scope: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    message = str(message or "").strip()
    if not message:
        return _clarification("Escriba la instruccion que quiere ejecutar.")

    command_id = _command_id()
    text = _plain(message)
    ui_context = dict(ui_context or {})
    scope = scope or ui_context.get("scope") or {}

    if _looks_like_save_draft(text):
        return _workflow(
            command_id,
            message,
            "save_draft",
            "Puedo guardar este modelo como borrador. Lo dejo listo para confirmar.",
            "Guardar borrador",
        )
    if _looks_like_save_final(text):
        return _workflow(
            command_id,
            message,
            "save_final",
            "Puedo guardar este modelo como version final. Esto genera el DOCX y deja el historico cerrado.",
            "Guardar final",
        )
    if _looks_like_generate_doc(text):
        return _workflow(
            command_id,
            message,
            "generate_document",
            "Puedo generar el documento con el modelo actual. Lo dejo listo para confirmar.",
            "Generar documento",
        )
    if _looks_like_load_saved(text):
        return _ui_action(
            command_id,
            message,
            "Abro el panel de modelos guardados para que cargue el borrador o historico correcto.",
            [{"type": "open_saved_models", "filter": _extract_client_hint(message)}],
        )
    if _looks_like_extract_documents(text):
        return _ui_action(
            command_id,
            message,
            "Vaya al bloque de documentos del cliente, adjunte cedula/matricula y ejecuto la extraccion desde ahi.",
            [{"type": "scroll_to", "target": "client_documents"}],
        )

    result = build_financial_model(payload)

    voucher_id = _extract_voucher_id(message) or _context_voucher_id(text, ui_context)
    if voucher_id and _looks_like_reverse(text):
        return _reverse_voucher_command(payload, result, message, command_id, voucher_id)
    if voucher_id and (_looks_like_show(text) or _is_bare_voucher_reference(text, voucher_id)):
        return _show_voucher_command(result, message, command_id, voucher_id)

    account = _extract_account(text, ui_context)
    month = _extract_month(text, result, ui_context)
    if account and _looks_like_explain(text):
        return _explain_account_command(result, message, command_id, account, month)
    if account and _looks_like_ledger(text):
        return _show_ledger_command(message, command_id, account)
    if account and _looks_like_filter(text):
        return _ui_action(
            command_id,
            message,
            f"Filtro el libro diario por {account}.",
            [{"type": "select_account", "account": account}, {"type": "scroll_to", "target": "accounting"}],
        )

    period = _extract_period_range(text)
    if period and _looks_like_period_change(text):
        return _period_change_command(payload, result, message, command_id, period)

    try:
        legacy = preview_chat_adjustment(payload, message, scope=scope)
    except ModelChatError as exc:
        return _error(command_id, message, str(exc))

    if not legacy.get("ok"):
        reason = legacy.get("error") or legacy.get("message") or legacy.get("clarification") or "Necesito un poco mas de detalle para preparar la propuesta."
        if "monto objetivo de caja" in reason.lower() or "palanca" in reason.lower():
            reason = (
                "Te sigo, pero necesito saber que accion quieres que haga. "
                "Por ejemplo: ver el comprobante, reversarlo, explicar una cuenta, cambiar un supuesto o registrar una partida."
            )
        return _clarification(reason, command_id=command_id, message=message, intent=str(legacy.get("intent") or "clarification_needed"))

    adjusted_payload = _payload_with_chat_audit(legacy.get("adjusted_payload") or payload, command_id, message, legacy)
    assistant_message = _friendly_proposal_message(legacy)
    legacy["adjusted_payload"] = adjusted_payload
    return {
        "ok": True,
        "assistant_message": assistant_message,
        "intent": str(legacy.get("action", {}).get("intent") or legacy.get("interpreted_action", {}).get("intent") or "financial_adjustment"),
        "response_type": RESPONSE_PROPOSAL,
        "requires_confirmation": True,
        "proposal": legacy.get("proposal") or {},
        "adjusted_payload": adjusted_payload,
        "ui_actions": [],
        "audit": _audit(command_id, message),
        **legacy,
    }


def _reverse_voucher_command(
    payload: Mapping[str, Any],
    result: Any,
    message: str,
    command_id: str,
    voucher_id: str,
) -> Dict[str, Any]:
    voucher = _find_voucher(result, voucher_id)
    if not voucher:
        return _error(command_id, message, f"No encontre el comprobante {voucher_id}.")
    if voucher.get("source") != CHAT_SOURCE:
        return _clarification(
            f"{voucher_id} es un comprobante automatico del sistema. Para cambiarlo, ajuste el supuesto o el evento que lo origina; no lo reverso directamente.",
            command_id=command_id,
            message=message,
            intent="reverse_voucher",
        )

    reverse_entry = _reverse_entry_from_voucher(voucher, command_id, message)
    if not reverse_entry:
        return _clarification(
            f"{voucher_id} no es una partida simple de dos cuentas. Puedo mostrarlo, pero necesito una instruccion mas especifica para reversarlo.",
            command_id=command_id,
            message=message,
            intent="reverse_voucher",
        )

    adjusted_payload = deepcopy(dict(payload or {}))
    movements = dict(adjusted_payload.get("movements") or {})
    journal_entries = list(movements.get("journal_entries") or [])
    journal_entries.append(reverse_entry)
    movements["journal_entries"] = journal_entries
    adjusted_payload["movements"] = movements
    proposal = _journal_proposal(
        reverse_entry,
        kind="voucher_reversal",
        title=f"Reverso de {voucher_id}",
        explanation=f"Reversa {voucher_id} creando una nueva partida contraria. El comprobante original queda visible para auditoria.",
    )
    adjusted_result = build_financial_model(adjusted_payload)
    proposal["impact"] = _impact(result, adjusted_result, reverse_entry["month"])
    adjusted_payload = _payload_with_chat_audit(adjusted_payload, command_id, message, {"proposal": proposal})
    return {
        "ok": True,
        "assistant_message": (
            f"Puedo reversar {voucher_id}. Registro contable propuesto:\n"
            f"Debe: {proposal['debit_label']} {_money(proposal['amount'])}\n"
            f"Haber: {proposal['credit_label']} {_money(proposal['amount'])}"
        ),
        "intent": "reverse_voucher",
        "response_type": RESPONSE_PROPOSAL,
        "requires_confirmation": True,
        "proposal": proposal,
        "adjusted_payload": adjusted_payload,
        "new_journal_entries": [reverse_entry],
        "ui_actions": [{"type": "select_voucher", "voucher_id": voucher_id}, {"type": "scroll_to", "target": "accounting"}],
        "audit": _audit(command_id, message),
    }


def _show_voucher_command(result: Any, message: str, command_id: str, voucher_id: str) -> Dict[str, Any]:
    voucher = _find_voucher(result, voucher_id)
    if not voucher:
        return _error(command_id, message, f"No encontre el comprobante {voucher_id}.")
    lines = []
    for line in voucher.get("lines") or []:
        debit = _money(line.get("debit"))
        credit = _money(line.get("credit"))
        lines.append(f"{line.get('account')}: Debe {debit}, Haber {credit}")
    details = "\n".join(lines) or "Sin lineas."
    return _ui_action(
        command_id,
        message,
        (
            f"Encontré {voucher_id} ({voucher.get('month')}): {voucher.get('description')}\n"
            f"{details}\n\n"
            "Si quieres anularlo, dime: revierte este comprobante."
        ),
        [{"type": "select_voucher", "voucher_id": voucher_id}, {"type": "scroll_to", "target": "accounting"}],
        intent="show_voucher",
    )


def _explain_account_command(result: Any, message: str, command_id: str, account: str, month: str) -> Dict[str, Any]:
    trace = dict((result.accounting.get("trace") or {}).get(f"{account}|{month}") or {})
    if not trace:
        return _answer(
            command_id,
            message,
            f"No encontre movimientos para {account} en {month}. Selecciono la cuenta para que la revise en el mayor.",
            intent="explain_balance",
            ui_actions=[{"type": "select_account", "account": account}, {"type": "scroll_to", "target": "accounting"}],
        )
    entries = trace.get("entries") or []
    duplicate_hint = _duplicate_hint(entries)
    lines = [
        f"{account} en {month}: saldo inicial {_money(trace.get('opening_balance'))}, debe {_money(trace.get('debits'))}, haber {_money(trace.get('credits'))}, saldo final {_money(trace.get('closing_balance'))}."
    ]
    if entries:
        lines.append("Movimientos principales:")
        for entry in entries[:6]:
            lines.append(
                f"- {entry.get('voucher_id')}: {entry.get('description')} | Debe {_money(entry.get('debit'))} | Haber {_money(entry.get('credit'))} | Saldo {_money(entry.get('running_balance'))}"
            )
        if len(entries) > 6:
            lines.append(f"- Hay {len(entries) - 6} movimiento(s) adicional(es) en el mayor.")
    if duplicate_hint:
        lines.append(duplicate_hint)
    return _answer(
        command_id,
        message,
        "\n".join(lines),
        intent="explain_balance",
        ui_actions=[{"type": "select_account", "account": account}, {"type": "scroll_to", "target": "accounting"}],
    )


def _show_ledger_command(message: str, command_id: str, account: str) -> Dict[str, Any]:
    return _ui_action(
        command_id,
        message,
        f"Listo. Selecciono {account} en el mayor y en la trazabilidad.",
        [{"type": "select_account", "account": account}, {"type": "scroll_to", "target": "accounting"}],
        intent="show_account_ledger",
    )


def _period_change_command(
    payload: Mapping[str, Any],
    result: Any,
    message: str,
    command_id: str,
    period: Mapping[str, str],
) -> Dict[str, Any]:
    adjusted_payload = deepcopy(dict(payload or {}))
    current_period = dict(adjusted_payload.get("period") or {})
    current_period["start_month"] = period["start_month"]
    current_period["end_month"] = period["end_month"]
    adjusted_payload["period"] = current_period
    adjusted_result = build_financial_model(adjusted_payload)
    adjusted_json = result_to_json(adjusted_result)
    proposal = {
        "kind": "period_change",
        "target_month": f"{period['start_month']} a {period['end_month']}",
        "explanation": f"Cambia el rango del modelo a {period['start_month']} - {period['end_month']} y recalcula los bloques.",
        "impact": _impact(result, adjusted_result, period["end_month"]),
    }
    adjusted_payload = _payload_with_chat_audit(adjusted_payload, command_id, message, {"proposal": proposal})
    return {
        "ok": True,
        "assistant_message": "Puedo cambiar el periodo y recalcular el modelo. Te dejo la propuesta antes de aplicarla.",
        "intent": "change_period",
        "response_type": RESPONSE_PROPOSAL,
        "requires_confirmation": True,
        "proposal": proposal,
        "adjusted_payload": adjusted_payload,
        "summary": adjusted_json.get("summary"),
        "preview": adjusted_json.get("preview"),
        "ui_actions": [],
        "audit": _audit(command_id, message),
    }


def _workflow(command_id: str, message: str, action: str, text: str, confirm_label: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "assistant_message": text,
        "intent": action,
        "response_type": RESPONSE_WORKFLOW,
        "requires_confirmation": True,
        "workflow": {"action": action, "confirm_label": confirm_label},
        "proposal": {
            "kind": "workflow",
            "workflow_action": action,
            "explanation": text,
            "confirm_label": confirm_label,
        },
        "ui_actions": [],
        "audit": _audit(command_id, message),
    }


def _answer(command_id: str, message: str, text: str, *, intent: str, ui_actions: Optional[list[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "ok": True,
        "assistant_message": text,
        "intent": intent,
        "response_type": RESPONSE_ANSWER,
        "requires_confirmation": False,
        "ui_actions": ui_actions or [],
        "audit": _audit(command_id, message),
    }


def _ui_action(command_id: str, message: str, text: str, ui_actions: list[Dict[str, Any]], *, intent: str = "ui_action") -> Dict[str, Any]:
    return {
        "ok": True,
        "assistant_message": text,
        "intent": intent,
        "response_type": RESPONSE_UI_ACTION,
        "requires_confirmation": False,
        "ui_actions": ui_actions,
        "audit": _audit(command_id, message),
    }


def _clarification(text: str, *, command_id: Optional[str] = None, message: str = "", intent: str = "clarification_needed") -> Dict[str, Any]:
    return {
        "ok": False,
        "assistant_message": text,
        "intent": intent,
        "response_type": RESPONSE_CLARIFICATION,
        "needs_clarification": True,
        "requires_confirmation": False,
        "ui_actions": [],
        "audit": _audit(command_id or _command_id(), message),
    }


def _error(command_id: str, message: str, text: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "assistant_message": text,
        "error": text,
        "intent": "error",
        "response_type": RESPONSE_ERROR,
        "requires_confirmation": False,
        "ui_actions": [],
        "audit": _audit(command_id, message),
    }


def _friendly_proposal_message(data: Mapping[str, Any]) -> str:
    proposal = dict(data.get("proposal") or {})
    kind = str(proposal.get("kind") or "")
    if kind == "assumption_change":
        return proposal.get("explanation") or "Tengo listo el cambio de supuesto; revisalo antes de aplicarlo."
    if kind == "journal_entry":
        rows = proposal.get("journal_rows") or []
        if rows:
            return "Registro contable propuesto:\n" + "\n".join(
                f"{row.get('account')}: Debe {_money(row.get('debit'))}, Haber {_money(row.get('credit'))}"
                for row in rows
            )
        return proposal.get("explanation") or "Tengo lista la partida doble; revisala antes de aplicarla."
    if kind == "compound_events":
        return proposal.get("explanation") or "Tengo listo el movimiento compuesto; revisalo antes de aplicarlo."
    return proposal.get("explanation") or "Tengo una propuesta calculada. Revisala y, si esta correcta, aplicala."


def _journal_proposal(entry: Mapping[str, Any], *, kind: str, title: str, explanation: str) -> Dict[str, Any]:
    amount = float(entry.get("amount") or entry.get("amount_nio") or 0)
    debit = str(entry.get("debit_account") or "")
    credit = str(entry.get("credit_account") or "")
    return {
        "kind": kind,
        "entry_type": entry.get("entry_type") or kind,
        "target_month": entry.get("month") or "",
        "amount": amount,
        "debit_account": debit,
        "credit_account": credit,
        "debit_label": _account_label(debit),
        "credit_label": _account_label(credit),
        "title": title,
        "journal_entry": dict(entry),
        "journal_rows": [
            {"account": _account_label(debit), "debit": amount, "credit": 0},
            {"account": _account_label(credit), "debit": 0, "credit": amount},
        ],
        "explanation": explanation,
        "impact": {},
    }


def _reverse_entry_from_voucher(voucher: Mapping[str, Any], command_id: str, message: str) -> Optional[Dict[str, Any]]:
    debit_line = None
    credit_line = None
    for line in voucher.get("lines") or []:
        debit = float(line.get("debit") or 0)
        credit = float(line.get("credit") or 0)
        if debit > 0:
            debit_line = line
        if credit > 0:
            credit_line = line
    if not debit_line or not credit_line:
        return None
    original_debit = _account_key(debit_line.get("account"))
    original_credit = _account_key(credit_line.get("account"))
    amount = float(debit_line.get("debit") or credit_line.get("credit") or 0)
    if not original_debit or not original_credit or amount <= 0:
        return None
    return {
        "month": str(voucher.get("month") or "")[:7],
        "debit_account": original_credit,
        "credit_account": original_debit,
        "amount": amount,
        "currency": "nio",
        "entry_type": "reversal",
        "source": CHAT_SOURCE,
        "instruction_id": command_id,
        "locked": True,
        "created_at": _utc_now(),
        "message": f"Reverso de {voucher.get('voucher_id')}: {message}",
        "reference_voucher_id": voucher.get("voucher_id") or "",
    }


def _find_voucher(result: Any, voucher_id: str) -> Optional[Dict[str, Any]]:
    wanted = str(voucher_id or "").strip().upper()
    for voucher in result.accounting.get("vouchers", []) or []:
        if str(voucher.get("voucher_id") or "").strip().upper() == wanted:
            return dict(voucher)
    return None


def _impact(before: Any, after: Any, month: str) -> Dict[str, float]:
    month = str(month or "")[:7]
    if not month:
        return {}
    return {
        "cash": _statement_value(after, "Efectivo y Equivalentes de Efectivo", month) - _statement_value(before, "Efectivo y Equivalentes de Efectivo", month),
        "assets": _statement_value(after, "Total Activos", month) - _statement_value(before, "Total Activos", month),
        "liabilities": _statement_value(after, "Total Pasivos", month) - _statement_value(before, "Total Pasivos", month),
        "equity": _statement_value(after, "Total Patrimonio", month) - _statement_value(before, "Total Patrimonio", month),
    }


def _statement_value(result: Any, label: str, month: str) -> float:
    df = getattr(result, "df_esf_mensual_full", None)
    if df is None:
        df = result.df_esf_mensual
    if month not in df.columns:
        return 0.0
    rows = df[df["Descripcion"].astype(str) == label]
    if rows.empty:
        return 0.0
    return round(float(rows.iloc[0][month] or 0))


def _payload_with_chat_audit(payload: Mapping[str, Any], command_id: str, message: str, data: Mapping[str, Any]) -> Dict[str, Any]:
    adjusted = deepcopy(dict(payload or {}))
    chat = dict(adjusted.get("chat") or {})
    commands = list(chat.get("commands") or [])
    commands.append({
        "command_id": command_id,
        "message": message,
        "intent": data.get("intent") or data.get("action", {}).get("intent") or data.get("proposal", {}).get("kind") or "",
        "created_at": _utc_now(),
        "source": CHAT_SOURCE,
        "status": "proposed",
    })
    chat["commands"] = commands
    adjusted["chat"] = chat
    return adjusted


def _duplicate_hint(entries: list[Mapping[str, Any]]) -> str:
    seen: dict[tuple[str, float, float], int] = {}
    for entry in entries:
        key = (str(entry.get("description") or ""), float(entry.get("debit") or 0), float(entry.get("credit") or 0))
        seen[key] = seen.get(key, 0) + 1
    duplicates = [key for key, count in seen.items() if count > 1 and (key[1] or key[2])]
    if duplicates:
        return "Nota: veo movimientos con descripcion y monto repetidos. Conviene revisar si alguno debe reversarse."
    return ""


def _extract_voucher_id(message: str) -> str:
    match = re.search(r"\b(?:CD|REV)-\d{4}-\d{4}\b", message, flags=re.IGNORECASE)
    return match.group(0).upper() if match else ""


def _context_voucher_id(text: str, ui_context: Mapping[str, Any]) -> str:
    selected = str(ui_context.get("selected_voucher") or "").strip().upper()
    if selected and any(token in text for token in ["este comprobante", "ese comprobante", "comprobante seleccionado"]):
        return selected
    return ""


def _is_bare_voucher_reference(text: str, voucher_id: str) -> bool:
    cleaned = text.replace(str(voucher_id or "").lower(), "")
    cleaned = re.sub(r"\b(de|del|en|el|la|mes|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre|ene|feb|mar|abr|may|jun|jul|ago|sept|sep|oct|nov|dic|20\d{2})\b", "", cleaned)
    cleaned = re.sub(r"[\s,.;:-]+", "", cleaned)
    return not cleaned


def _extract_account(text: str, ui_context: Mapping[str, Any]) -> str:
    selected = str(ui_context.get("selected_account") or "").strip()
    if any(token in text for token in ["esta cuenta", "cuenta seleccionada", "este saldo"]) and selected:
        return selected
    for alias, account_key in sorted(LEDGER_ACCOUNT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in text:
            return _account_label(account_key)
    for label in LABEL_TO_ACCOUNT:
        if _plain(label) in text:
            return label
    return selected


def _extract_month(text: str, result: Any, ui_context: Mapping[str, Any]) -> str:
    months = _model_months(result)
    selected_month = str(ui_context.get("selected_month") or "").strip()[:7]
    for month in months:
        if month in text:
            return month
    year_match = re.search(r"\b(20\d{2})\b", text)
    for name, number in MONTHS_ES.items():
        if name in text and year_match:
            candidate = f"{year_match.group(1)}-{number}"
            if candidate in months:
                return candidate
    return selected_month if selected_month in months else (months[-1] if months else "")


def _extract_period_range(text: str) -> Optional[Dict[str, str]]:
    matches = list(re.finditer(r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre|ene|feb|mar|abr|may|jun|jul|ago|sept|sep|oct|nov|dic)\s+(?:de\s+)?(20)?(\d{2})\b", text))
    if len(matches) < 2:
        return None
    start = _month_from_match(matches[0])
    end = _month_from_match(matches[-1])
    if start and end:
        return {"start_month": start, "end_month": end}
    return None


def _month_from_match(match: re.Match[str]) -> str:
    name = match.group(1)
    year = match.group(3)
    full_year = f"20{year}" if len(year) == 2 else year
    return f"{full_year}-{MONTHS_ES[name]}"


def _extract_client_hint(message: str) -> str:
    text = str(message or "").strip()
    for marker in [" de ", " para "]:
        if marker in text.lower():
            return text.lower().split(marker)[-1].strip()
    return ""


def _model_months(result: Any) -> list[str]:
    full_summary = (getattr(result, "metadata", {}) or {}).get("full_summary") or {}
    months = full_summary.get("months") or result.summary.get("all_months") or result.summary.get("months") or []
    return [str(month)[:7] for month in months]


def _account_key(value: Any) -> str:
    text = str(value or "").strip()
    if text in LABEL_TO_ACCOUNT:
        return LABEL_TO_ACCOUNT[text]
    plain = _plain(text)
    return LEDGER_ACCOUNT_ALIASES.get(plain) or (text if text in LEDGER_ACCOUNT_LABELS else "")


def _account_label(key_or_label: Any) -> str:
    text = str(key_or_label or "").strip()
    return LEDGER_ACCOUNT_LABELS.get(text) or text


def _plain(value: Any) -> str:
    text = str(value or "").lower()
    text = "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text).strip()


def _money(value: Any) -> str:
    number = round(float(value or 0))
    sign = "-" if number < 0 else ""
    return f"{sign}{abs(number):,.0f}"


def _command_id() -> str:
    return f"chat_{uuid4().hex[:12]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(command_id: str, message: str) -> Dict[str, Any]:
    return {
        "command_id": command_id,
        "message": message,
        "source": CHAT_SOURCE,
        "created_at": _utc_now(),
    }


def _looks_like_reverse(text: str) -> bool:
    return any(token in text for token in ["reversa", "reversar", "revierte", "revertir", "anula", "anular", "deshace", "deshacer"])


def _looks_like_show(text: str) -> bool:
    return any(token in text for token in ["muestra", "mostrar", "ver", "abre", "abrir", "detalle"])


def _looks_like_explain(text: str) -> bool:
    return any(token in text for token in ["explica", "explicame", "de donde sale", "rastrea", "traza", "trazabilidad", "que afecto", "por que"])


def _looks_like_ledger(text: str) -> bool:
    return any(token in text for token in ["mayor", "movimiento por cuenta", "movimientos de", "kardex"])


def _looks_like_filter(text: str) -> bool:
    return any(token in text for token in ["filtra", "filtrar", "selecciona", "seleccionar"])


def _looks_like_save_draft(text: str) -> bool:
    return any(token in text for token in ["guardar", "guarda", "guardame"]) and "borrador" in text


def _looks_like_save_final(text: str) -> bool:
    return ("guardar" in text or "guarda" in text) and "final" in text


def _looks_like_generate_doc(text: str) -> bool:
    return any(token in text for token in ["genera el documento", "generar documento", "genera docx", "generar docx"])


def _looks_like_load_saved(text: str) -> bool:
    return any(token in text for token in ["carga", "cargar", "levanta", "abrir borrador", "abre borrador", "ultimo borrador", "historico"])


def _looks_like_extract_documents(text: str) -> bool:
    return any(token in text for token in ["extrae", "extraer"]) and any(token in text for token in ["cedula", "matricula", "documento"])


def _looks_like_period_change(text: str) -> bool:
    return any(token in text for token in ["periodo", "rango", "mes inicio", "mes final", "cambia de", "desde"])

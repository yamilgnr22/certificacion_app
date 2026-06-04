"""Preparacion de propuestas del agente contable.

Mixin con la logica de construir cada tipo de propuesta a partir del
intent del LLM: reverso de comprobante, correccion, plan compuesto,
asiento contable, cambio de supuesto, valor exacto mensual, ajuste
por objetivo de saldo, creacion de cuenta y finalizacion de periodo.

El mixin asume que el host implementa `_account_label`,
`_normalize_account`, `_postable_account_error`,
`_valid_account_suggestions`, `_ensure_dynamic_accounts_in_payload`,
`_catalog_hash` y `_compute_assumption_impact`, y expone
`self.tools`, `self.planner`, `self.accounts`.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from accounting_accounts import LEDGER_ACCOUNT_LABELS
from accounting_model import reverse_voucher
from financial_model import build_financial_model
from services.agent_constants import TARGET_BALANCE_ACCOUNTS, TARGET_COUNTER_DEFAULTS
from services.agent_errors import AgentValidationError
from services.agent_helpers import (
    _account_catalog_payload,
    _account_code,
    _append_dynamic_account,
    _append_journal_entry,
    _append_saved_voucher,
    _apply_assumption_change,
    _dynamic_account_lookup,
    _extract_target_month,
    _find_reversal_for,
    _income_overrides_by_month,
    _income_overrides_list,
    _journal_entry_from_reversal_voucher,
    _journal_pair,
    _journal_title,
    _journal_totals,
    _last_payload_month,
    _normalize_account_section,
    _normalize_account_type,
    _normalize_assumption_field,
    _normalize_month_value,
    _payload_months,
    _period_exchange_rate,
    _previous_year_end,
    _proposal_payload,
    _resolve_correctable_voucher,
    _saved_vouchers,
    _statement_value,
    _target_amount_value,
    _to_float,
    _unique_reversal_id,
    _valid_type_section,
    _voucher_rows,
)


def _monthly_override_updates(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = args.get("updates") or args.get("months") or args.get("valores") or []
    if isinstance(raw, Mapping):
        raw = [raw]
    updates = [dict(item) for item in raw if isinstance(item, Mapping)]
    if not updates and (args.get("month") or args.get("mes")):
        updates = [dict(args)]
    return updates


def _monthly_override_removals(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = args.get("remove") or args.get("removals") or args.get("delete") or []
    if args.get("action") in {"remove", "delete", "quitar", "eliminar"} and (args.get("month") or args.get("mes")):
        raw = [dict(args)]
    if isinstance(raw, Mapping):
        raw = [raw]
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _first_present(data: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if data.get(key) not in {None, ""}:
            return data.get(key)
    return None


def has_monthly_override_data(item: Mapping[str, Any]) -> bool:
    return any(key != "month" and value not in {None, ""} for key, value in item.items())


def _is_past_month(month: str) -> bool:
    current = datetime.now(timezone.utc).strftime("%Y-%m")
    return bool(month and month < current)


class AgentProposalBuilderMixin:
    """Construye propuestas (payload proyectado + payload visible) por intent."""

    def _build_projected_payload(
        self,
        *,
        intent: str,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        ui_context: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if intent == "reverse_voucher":
            return self._prepare_reverse_voucher(payload, args, command_id, original_message)
        if intent == "correct_voucher":
            return self._prepare_correct_voucher(payload, args, command_id, original_message)
        if intent == "compound_plan":
            return self._prepare_compound_plan(payload, args, command_id, original_message)
        if intent in {"journal_entry", "account_transfer", "year_close_transfer"}:
            return self._prepare_journal_entry(intent, payload, args, command_id, original_message)
        if intent == "assumption_change":
            return self._prepare_assumption_change(payload, args, ui_context, command_id, original_message)
        if intent == "monthly_override":
            return self._prepare_monthly_override(payload, args, command_id, original_message)
        if intent == "target_balance_adjustment":
            return self._prepare_target_balance_adjustment(payload, args, command_id, original_message)
        if intent == "create_account":
            return self._prepare_create_account(payload, args, command_id, original_message)
        if intent == "finalizar_periodo":
            return self._prepare_finalizar_periodo(payload, args, command_id, original_message)
        raise AgentValidationError("Esa accion todavia no esta habilitada en el asistente.")

    def _prepare_reverse_voucher(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        voucher_id = str(args.get("voucher_id") or "").strip().upper()
        if not voucher_id:
            raise AgentValidationError("Indique el comprobante que desea reversar.")
        persisted_vouchers = _saved_vouchers(payload)
        voucher = next((dict(v) for v in persisted_vouchers if str(v.get("voucher_id") or "").upper() == voucher_id), None)
        if not voucher:
            result = build_financial_model(payload)
            synthetic = next((dict(v) for v in result.accounting.get("vouchers", []) if str(v.get("voucher_id") or "").upper() == voucher_id), None)
            if synthetic:
                raise AgentValidationError("Ese comprobante es generado automaticamente; en esta fase solo puedo reversar comprobantes guardados.")
            raise AgentValidationError(f"No encontre el comprobante {voucher_id}.")
        if str(voucher.get("type") or "").lower() == "reversal":
            raise AgentValidationError("No se puede reversar un comprobante de reverso.")
        existing_reversal = _find_reversal_for(persisted_vouchers, voucher_id)
        if existing_reversal:
            raise AgentValidationError(f"El comprobante {voucher_id} ya fue reversado por {existing_reversal}.")
        reversal_id = _unique_reversal_id(voucher_id, persisted_vouchers)
        reversal = reverse_voucher(voucher, voucher_id=reversal_id)
        reversal["type"] = "reversal"
        reversal["source"] = "chat_financiero"
        reversal["reference_voucher_id"] = voucher_id
        reversal["description"] = f"Reverso de {voucher_id}"
        reversal["instruction_id"] = command_id
        projected = _append_saved_voucher(payload, reversal)
        return projected, _proposal_payload(
            kind="voucher_reversal",
            title=f"Reversar comprobante {voucher_id}",
            assistant_message=f"Puedo reversar {voucher_id}. Te dejo el comprobante contrario antes de aplicarlo.",
            month=reversal.get("month"),
            rows=_voucher_rows(reversal),
            technical_records=[reversal],
            original_message=original_message,
            extra={"original_voucher_id": voucher_id, "reversal_voucher_id": reversal_id},
        )

    def _prepare_correct_voucher(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        voucher_id = str(args.get("voucher_id") or args.get("original_voucher_id") or "").strip().upper()
        if not voucher_id:
            raise AgentValidationError("Indique el comprobante que desea corregir.")
        persisted_vouchers = _saved_vouchers(payload)
        result = build_financial_model(payload)
        voucher, source_kind = _resolve_correctable_voucher(payload, result.accounting.get("vouchers") or [], persisted_vouchers, voucher_id)
        if str(voucher.get("type") or "").lower() == "reversal":
            raise AgentValidationError("No se puede corregir un comprobante de reverso.")
        existing_reversal = _find_reversal_for([*persisted_vouchers, *[dict(v) for v in result.accounting.get("vouchers") or [] if isinstance(v, Mapping)]], voucher_id)
        if existing_reversal:
            raise AgentValidationError(f"El comprobante {voucher_id} ya fue reversado por {existing_reversal}.")

        correction = args.get("correction") if isinstance(args.get("correction"), Mapping) else args
        target_month = str(correction.get("month") or correction.get("target_month") or voucher.get("month") or "")[:7]
        description = str(correction.get("description") or "").strip()
        raw_lines = correction.get("lines")
        lines = self._normalize_journal_lines(raw_lines, payload)
        self._validate_journal_entry(payload=payload, month=target_month, description=description, lines=lines)

        reversal_id = _unique_reversal_id(voucher_id, [*persisted_vouchers, *[dict(v) for v in result.accounting.get("vouchers") or [] if isinstance(v, Mapping)]])
        reversal = reverse_voucher(voucher, voucher_id=reversal_id)
        reversal["type"] = "reversal"
        reversal["source"] = "chat_financiero"
        reversal["reference_voucher_id"] = voucher_id
        reversal["description"] = f"Reverso de {voucher_id}"
        reversal["instruction_id"] = command_id
        correction_entry_id = f"JE-{uuid.uuid4().hex[:8].upper()}"
        correction_entry = {
            "entry_id": correction_entry_id,
            "month": target_month,
            "description": description[:200],
            "lines": lines,
            "currency": "nio",
            "entry_type": "voucher_correction",
            "source": "chat_financiero",
            "instruction_id": command_id,
            "locked": True,
            "message": original_message,
            "corrects_voucher_id": voucher_id,
            "reversal_voucher_id": reversal_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        projected = deepcopy(dict(payload or {}))
        if source_kind == "persisted":
            projected = _append_saved_voucher(projected, reversal)
        else:
            reversal_entry = _journal_entry_from_reversal_voucher(reversal, command_id=command_id, original_message=original_message)
            projected = _append_journal_entry(projected, reversal_entry)
        projected = _append_journal_entry(projected, correction_entry)
        projected = self._ensure_dynamic_accounts_in_payload(projected, [str(line.get("account") or "") for line in lines])
        build_financial_model(projected)

        correction_rows = [
            {"account": line["account_label"], "debit": line["debit"], "credit": line["credit"], "reference": line.get("reference") or ""}
            for line in lines
        ]
        execution_plan = [
            {"tool": "reverse_voucher", "args": {"voucher_id": voucher_id}, "mutates": True},
            {
                "tool": "journal_entry",
                "args": {"month": target_month, "description": description[:200], "lines": lines},
                "mutates": True,
            },
        ]
        user_visible_steps = [
            {"kind": "voucher_reversal", "title": f"Reverso de {voucher_id}", "rows": _voucher_rows(reversal)},
            {"kind": "journal_entry", "title": "Nuevo asiento corregido", "rows": correction_rows},
        ]
        return projected, _proposal_payload(
            kind="compound_agent_proposal",
            title=f"Corregir comprobante {voucher_id}",
            assistant_message=f"Prepare la correccion de {voucher_id}: reverso del comprobante original y nuevo asiento corregido.",
            month=target_month,
            rows=[],
            technical_records=[reversal, correction_entry],
            original_message=original_message,
            extra={
                "compound_type": "voucher_correction",
                "execution_plan": execution_plan,
                "user_visible_steps": user_visible_steps,
                "original_voucher_id": voucher_id,
                "reversal_voucher_id": reversal_id,
                "correction_entry_id": correction_entry_id,
                "description": description[:200],
                "source_kind": source_kind,
                "reversal_rows": _voucher_rows(reversal),
                "correction_rows": correction_rows,
                "totals": _journal_totals(lines),
            },
        )

    def _prepare_compound_plan(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        steps = self.planner.validate({"steps": args.get("steps") or []})
        create_steps = [step for step in steps if step.tool == "create_account"]
        journal_steps = [step for step in steps if step.tool == "journal_entry"]
        if len(create_steps) > 1 or len(journal_steps) != 1:
            raise AgentValidationError("El plan compuesto debe incluir un solo asiento y, como maximo, una cuenta nueva.")

        projected = deepcopy(dict(payload or {}))
        execution_plan: list[dict[str, Any]] = []
        user_visible_steps: list[dict[str, Any]] = []
        account_operations: list[dict[str, Any]] = []
        technical_records: list[dict[str, Any]] = []
        journal_rows: list[dict[str, Any]] = []

        for step in steps:
            execution_plan.append({"tool": step.tool, "args": step.args, "mutates": step.tool in {"create_account", "journal_entry"}})
            if step.tool in {"find_account", "validate_account"}:
                name = str(step.args.get("name") or step.args.get("account") or "").strip()
                if name:
                    found = self.accounts.find_by_text(name)
                    execution_plan[-1]["result"] = {"found": bool(found), "code": found.code if found else ""}
                continue
            if step.tool == "create_account":
                name = str(step.args.get("name") or step.args.get("account_name") or "").strip()
                existing = self.accounts.find_by_text(name) or self.accounts.get_by_name(name) or self.accounts.get_by_code(_account_code(name))
                if existing:
                    execution_plan[-1]["result"] = {"skipped": True, "reason": "account_exists", "code": existing.code}
                    projected = self._ensure_dynamic_accounts_in_payload(projected, [existing.code])
                    continue
                projected, create_payload = self._prepare_create_account(projected, step.args, command_id, original_message)
                account = dict(create_payload.get("account") or {})
                account_operations.append(account)
                technical_records.append(account)
                user_visible_steps.append({
                    "kind": "create_account",
                    "title": f"Crear cuenta {account.get('name')}",
                    "account": account,
                })
                execution_plan[-1]["result"] = {"proposed": True, "code": account.get("code")}
                continue
            if step.tool == "journal_entry":
                projected, journal_payload = self._prepare_journal_entry("journal_entry", projected, step.args, command_id, original_message)
                rows = list(journal_payload.get("journal_rows") or [])
                journal_rows = rows
                technical_records.extend(list(journal_payload.get("technical_records") or []))
                user_visible_steps.append({
                    "kind": "journal_entry",
                    "title": journal_payload.get("title") or "Asiento contable",
                    "description": journal_payload.get("description") or step.args.get("description") or "",
                    "month": journal_payload.get("month"),
                    "rows": rows,
                    "totals": journal_payload.get("totals") or {},
                })
                execution_plan[-1]["result"] = {"proposed": True, "month": journal_payload.get("month")}

        if not journal_rows:
            raise AgentValidationError("No pude preparar el asiento contable del plan.")
        build_financial_model(projected)
        return projected, _proposal_payload(
            kind="compound_agent_proposal",
            title="Propuesta compuesta del asistente",
            assistant_message="Prepare una propuesta compuesta con los pasos contables solicitados.",
            month=_extract_target_month({"user_visible_steps": user_visible_steps}, args),
            rows=journal_rows,
            technical_records=technical_records,
            original_message=original_message,
            extra={
                "compound_type": "planned_account_and_entry",
                "execution_plan": execution_plan,
                "user_visible_steps": user_visible_steps,
                "account_operations": account_operations,
                "catalog_before_hash": self._catalog_hash(),
            },
        )

    def _prepare_journal_entry(
        self,
        intent: str,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raw_month = args.get("month") or args.get("target_month") or _last_payload_month(payload)
        target_month = _normalize_month_value(raw_month) or str(raw_month or "")[:7]
        description = str(args.get("description") or args.get("message") or original_message or "").strip()
        raw_lines = args.get("lines")
        amount = _to_float(args.get("amount"))
        debit = self.tools.normalize_account(args.get("debit_account") or args.get("source_account"))
        credit = self.tools.normalize_account(args.get("credit_account") or args.get("destination_account"))
        source_month = str(args.get("source_month") or "")[:7]
        if intent == "year_close_transfer":
            debit = debit or "current_earnings"
            credit = credit or "retained_earnings"
            source_month = source_month or _previous_year_end(target_month)
            if amount is None:
                amount = _statement_value(build_financial_model(payload), "Resultados del Ejercicio", source_month)
        if intent == "account_transfer" and (not debit or not credit):
            debit = self.tools.normalize_account(args.get("source_account"))
            credit = self.tools.normalize_account(args.get("destination_account"))
        if raw_lines is None:
            raw_lines = [
                {"account": debit, "debit": amount or 0, "credit": 0},
                {"account": credit, "debit": 0, "credit": amount or 0},
            ]
        lines = self._normalize_journal_lines(raw_lines, payload)
        self._validate_journal_entry(
            payload=payload,
            month=target_month,
            description=description,
            lines=lines,
        )
        entry = {
            "month": target_month,
            "description": description[:200],
            "lines": lines,
            "currency": "nio",
            "entry_type": intent,
            "source": "chat_financiero",
            "instruction_id": command_id,
            "locked": True,
            "message": original_message,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        pair = _journal_pair(lines)
        if pair:
            entry.update(pair)
        if source_month:
            entry["source_month"] = source_month
        projected = _append_journal_entry(payload, entry)
        projected = self._ensure_dynamic_accounts_in_payload(projected, [str(line.get("account") or "") for line in lines])
        build_financial_model(projected)
        rows = [
            {"account": line["account_label"], "debit": line["debit"], "credit": line["credit"], "reference": line.get("reference") or ""}
            for line in lines
        ]
        totals = _journal_totals(lines)
        return projected, _proposal_payload(
            kind="journal_entry_proposal",
            title=_journal_title(intent),
            assistant_message=f"Registro contable propuesto: {description[:200]}.",
            month=target_month,
            rows=rows,
            technical_records=[entry],
            original_message=original_message,
            extra={"source_month": source_month or None, "description": description[:200], "totals": totals},
        )

    def _normalize_journal_lines(self, raw_lines: Any, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(raw_lines, list):
            raise AgentValidationError("La partida debe incluir una lista de lineas contables.")
        lines: list[dict[str, Any]] = []
        invalid_accounts: list[str] = []
        for raw in raw_lines:
            if not isinstance(raw, Mapping):
                continue
            raw_account = raw.get("account") or raw.get("cuenta") or raw.get("debit_account") or raw.get("credit_account")
            account = self._normalize_account(raw_account)
            label = self._account_label(account, payload)
            if not label:
                invalid_accounts.append(str(raw_account or "").strip() or "(sin cuenta)")
                continue
            postable_error = self._postable_account_error(account, label)
            if postable_error:
                raise AgentValidationError(postable_error)
            debit = round(float(_to_float(raw.get("debit") or raw.get("debe")) or 0), 2)
            credit = round(float(_to_float(raw.get("credit") or raw.get("haber")) or 0), 2)
            lines.append({
                "account": account,
                "account_label": label,
                "debit": debit,
                "credit": credit,
                "reference": str(raw.get("reference") or raw.get("referencia") or "").strip(),
            })
        if invalid_accounts:
            suggestions = ", ".join(self._valid_account_suggestions(payload)[:12])
            raise AgentValidationError(
                f"No reconozco la cuenta {invalid_accounts[0]}. Use una cuenta existente. Cuentas validas: {suggestions}."
            )
        return lines

    def _validate_journal_entry(
        self,
        *,
        payload: Mapping[str, Any],
        month: str,
        description: str,
        lines: list[Mapping[str, Any]],
    ) -> None:
        valid_months = set(_payload_months(payload))
        if not month or month not in valid_months:
            raise AgentValidationError(f"El mes {month or '(vacio)'} no esta dentro del periodo modelado.")
        if not description or len(description) > 200:
            raise AgentValidationError("La descripcion de la partida es requerida y debe tener 200 caracteres o menos.")
        if len(lines) < 2:
            raise AgentValidationError("La partida debe tener al menos dos lineas.")
        if len({str(line.get("account") or "") for line in lines}) < 2:
            raise AgentValidationError("La cuenta al debe y al haber deben ser diferentes.")
        seen: set[tuple[str, str]] = set()
        for line in lines:
            debit = float(line.get("debit") or 0)
            credit = float(line.get("credit") or 0)
            if (debit > 0 and credit > 0) or (debit <= 0 and credit <= 0):
                raise AgentValidationError("Cada linea debe tener exactamente un valor al debe o al haber.")
            side = "debit" if debit > 0 else "credit"
            key = (str(line.get("account") or ""), side)
            if key in seen:
                raise AgentValidationError("La partida tiene una linea duplicada para la misma cuenta y signo.")
            seen.add(key)
        totals = _journal_totals(lines)
        if not totals["balanced"]:
            raise AgentValidationError("La partida esta descuadrada: el debe y el haber deben ser iguales.")

    def _prepare_assumption_change(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        ui_context: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        field = _normalize_assumption_field(args.get("field") or args.get("assumption") or "cost_pct")
        value = args.get("new_value")
        if value is None:
            value = args.get("value")
        if value is None:
            value = args.get(field)
        projected, before_value, after_value = _apply_assumption_change(payload, field=field, value=value)
        build_financial_model(projected)
        rows: list[dict[str, Any]] = []
        return projected, _proposal_payload(
            kind="assumption_change_proposal",
            title="Cambiar supuesto del modelo",
            assistant_message=f"Propuesta: cambiar {field} de {before_value} a {after_value} para todo el periodo.",
            month=None,
            rows=rows,
            technical_records=[{"field": field, "before": before_value, "after": after_value, "scope": "period"}],
            original_message=original_message,
            extra={"field": field, "before": before_value, "after": after_value, "scope": "period"},
        )

    def _prepare_monthly_override(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        valid_months = set(_payload_months(payload))
        if not valid_months:
            raise AgentValidationError("El periodo no tiene meses validos para aplicar valores exactos.")
        period = dict(payload.get("period") or {})
        exchange_rate = float(_to_float(period.get("exchange_rate") or period.get("tasa_cambio")) or 0)
        if exchange_rate <= 0:
            raise AgentValidationError("El periodo necesita una tasa de cambio valida para convertir montos.")

        income = dict(payload.get("income") or {})
        overrides = _income_overrides_by_month(income.get("monthly_overrides") or income.get("overrides") or [])
        updates = _monthly_override_updates(args)
        removals = _monthly_override_removals(args)
        if not updates and not removals:
            raise AgentValidationError("Indique mes y valor exacto de ingreso o costo.")

        rows: list[dict[str, Any]] = []
        for update in updates:
            month = str(update.get("month") or update.get("mes") or "")[:7]
            if month not in valid_months:
                raise AgentValidationError(f"El mes {month or '(vacio)'} no esta dentro del periodo modelado.")
            current = dict(overrides.get(month) or {"month": month})
            before = {"revenue_usd": current.get("revenue_usd"), "cogs_usd": current.get("cogs_usd")}
            changed = False
            for target_key, aliases in {
                "revenue_usd": ("revenue_usd", "ingreso_usd", "revenue", "ingreso"),
                "cogs_usd": ("cogs_usd", "costo_usd", "cost_usd", "costo"),
            }.items():
                raw_value = _first_present(update, aliases)
                nio_value = _first_present(update, (target_key.replace("_usd", "_nio"), aliases[1].replace("_usd", "_nio")))
                if raw_value is None and nio_value is None:
                    continue
                value = (float(_to_float(nio_value) or 0.0) / exchange_rate) if nio_value is not None else float(_to_float(raw_value) or 0.0)
                if value < 0:
                    raise AgentValidationError("Los valores exactos de ingreso y costo no pueden ser negativos.")
                current[target_key] = round(float(value), 2)
                changed = True
            note = str(update.get("note") or update.get("nota") or "").strip()
            if note:
                current["note"] = note[:200]
            if not changed and not note:
                continue
            overrides[month] = current
            rows.append({
                "month": month,
                "before_revenue_usd": before.get("revenue_usd"),
                "after_revenue_usd": current.get("revenue_usd"),
                "before_cogs_usd": before.get("cogs_usd"),
                "after_cogs_usd": current.get("cogs_usd"),
                "note": current.get("note") or "",
            })

        for removal in removals:
            month = str(removal.get("month") or removal.get("mes") or "")[:7]
            if month not in valid_months:
                raise AgentValidationError(f"El mes {month or '(vacio)'} no esta dentro del periodo modelado.")
            current = dict(overrides.get(month) or {"month": month})
            before = {"revenue_usd": current.get("revenue_usd"), "cogs_usd": current.get("cogs_usd")}
            fields = removal.get("fields") or removal.get("campos") or ["revenue_usd", "cogs_usd", "note"]
            if isinstance(fields, str):
                fields = [fields]
            for field in fields:
                normalized = str(field or "").strip().lower()
                if normalized in {"revenue", "ingreso", "ingreso_usd"}:
                    normalized = "revenue_usd"
                if normalized in {"cogs", "cost", "costo", "costo_usd"}:
                    normalized = "cogs_usd"
                current.pop(normalized, None)
            if has_monthly_override_data(current):
                overrides[month] = current
            else:
                overrides.pop(month, None)
            rows.append({
                "month": month,
                "before_revenue_usd": before.get("revenue_usd"),
                "after_revenue_usd": current.get("revenue_usd"),
                "before_cogs_usd": before.get("cogs_usd"),
                "after_cogs_usd": current.get("cogs_usd"),
                "removed": True,
            })

        projected = deepcopy(dict(payload or {}))
        projected_income = dict(projected.get("income") or {})
        projected_income["monthly_overrides"] = _income_overrides_list(overrides)
        projected["income"] = projected_income
        build_financial_model(projected)
        return projected, _proposal_payload(
            kind="monthly_override_proposal",
            title="Valores exactos por mes",
            assistant_message="Prepare una propuesta para actualizar ingresos y costos exactos por mes.",
            month=rows[0]["month"] if rows else None,
            rows=rows,
            technical_records=rows,
            original_message=original_message,
            extra={
                "scope": "monthly_overrides",
                "override_rows": rows,
                "assumption_impact": self._compute_assumption_impact(payload, projected),
            },
        )

    def _prepare_target_balance_adjustment(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        target_account = self._normalize_account(args.get("account") or args.get("target_account") or args.get("cuenta"))
        raw_month = args.get("month") or args.get("target_month") or args.get("mes") or _last_payload_month(payload)
        target_month = _normalize_month_value(raw_month) or str(raw_month or "")[:7]
        target_amount = _target_amount_value(args)
        currency = str(args.get("currency") or args.get("moneda") or "nio").strip().upper()
        if currency in {"DOLAR", "DOLARES"}:
            currency = "USD"
        if currency in {"CORDOBA", "CORDOBAS", "C$"}:
            currency = "NIO"
        if not target_account or target_account not in TARGET_BALANCE_ACCOUNTS:
            raise AgentValidationError(
                "Entendi que queres ajustar una cuenta a un saldo objetivo, pero en esta fase solo puedo hacerlo para "
                "Inventario, Caja, Cuentas por Cobrar y Proveedores."
            )
        valid_months = set(_payload_months(payload))
        if target_month not in valid_months:
            raise AgentValidationError(f"El mes {target_month or '(vacio)'} no esta dentro del periodo modelado.")
        if target_amount is None or target_amount < 0:
            raise AgentValidationError("Indique un saldo objetivo valido y no negativo.")

        result = build_financial_model(payload)
        exchange_rate = _period_exchange_rate(payload)
        target_label = TARGET_BALANCE_ACCOUNTS[target_account]["label"]
        current_nio = _statement_value(result, target_label, target_month)
        target_nio = round(float(target_amount) * exchange_rate, 2) if currency == "USD" else round(float(target_amount), 2)
        delta_nio = round(target_nio - current_nio, 2)
        if abs(delta_nio) <= 1:
            raise AgentValidationError(f"{target_label} ya cierra cerca del objetivo en {target_month}; no hace falta asiento.")

        counter_account = self._target_counter_account(target_account, delta_nio, args)
        lines = self._target_adjustment_lines(
            target_account=target_account,
            counter_account=counter_account,
            delta_nio=delta_nio,
        )
        description = (
            f"Ajuste para que {target_label} cierre en {target_amount:,.2f} {currency} "
            f"en {target_month}"
        )
        self._validate_journal_entry(payload=payload, month=target_month, description=description, lines=lines)
        entry_id = f"JE-{uuid.uuid4().hex[:8].upper()}"
        entry = {
            "entry_id": entry_id,
            "month": target_month,
            "description": description[:200],
            "lines": lines,
            "currency": "nio",
            "entry_type": "target_balance_adjustment",
            "source": "chat_financiero",
            "instruction_id": command_id,
            "locked": True,
            "message": original_message,
            "target_account": target_account,
            "target_month": target_month,
            "target_amount_original": target_amount,
            "target_currency": currency,
            "target_amount_nio": target_nio,
            "current_balance_nio_before": current_nio,
            "delta_applied_nio": delta_nio,
            "counter_account": counter_account,
            "exchange_rate_used": exchange_rate,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        projected = _append_journal_entry(payload, entry)
        build_financial_model(projected)
        rows = [
            {"account": line["account_label"], "debit": line["debit"], "credit": line["credit"], "reference": line.get("reference") or ""}
            for line in lines
        ]
        warnings: list[str] = []
        if _is_past_month(target_month):
            warnings.append("Estas ajustando un mes pasado. Verifica que no este certificado antes de aplicar.")
        delta_sign = "+" if delta_nio >= 0 else "-"
        return projected, _proposal_payload(
            kind="target_balance_adjustment_proposal",
            title=f"Ajuste por objetivo: {target_label} {target_month} a {target_amount:,.2f} {currency}",
            assistant_message=(
                f"Interprete tu pedido como un unico ajuste: llevar {target_label} en {target_month} "
                f"a {target_amount:,.2f} {currency} (delta C${delta_sign}{abs(delta_nio):,.2f}). "
                f"Si tu instruccion incluia otros objetivos (promedio de varios meses, otros meses, "
                f"otras cuentas), procesalos por separado."
            ),
            month=target_month,
            rows=rows,
            technical_records=[entry],
            original_message=original_message,
            extra={
                "target": {
                    "account": target_account,
                    "account_label": target_label,
                    "month": target_month,
                    "target_amount_original": target_amount,
                    "target_currency": currency,
                    "target_amount_nio": target_nio,
                    "current_balance_nio_before": current_nio,
                    "delta_applied_nio": delta_nio,
                    "exchange_rate_used": exchange_rate,
                },
                "counter_account": counter_account,
                "counter_account_label": self._account_label(counter_account, payload),
                "journal_entry_id": entry_id,
                "description": description[:200],
                "totals": _journal_totals(lines),
                "calculation_steps": [
                    f"Saldo actual C$: {current_nio:,.2f}",
                    f"Objetivo C$: {target_nio:,.2f}",
                    f"Diferencia C$: {delta_nio:,.2f}",
                ],
                "warnings": warnings,
            },
        )

    def _target_counter_account(self, target_account: str, delta_nio: float, args: Mapping[str, Any]) -> str:
        explicit = args.get("counter_account") or args.get("contra_account") or args.get("contrapartida")
        if explicit:
            counter = self._normalize_account(explicit)
            if self._account_label(counter, {}):
                return counter
            raise AgentValidationError(
                f"No reconozco la contrapartida '{explicit}'. Probá con nombres como: "
                "capital, loans_personal, loans_pledge, loans_commercial, loans_mortgage, suppliers, inventory."
            )
        if target_account == "cash":
            raise AgentValidationError(
                "Para ajustar la caja necesito que me indiques la contrapartida explicitamente "
                "(ej: 'usa capital como contrapartida', 'usa loans_personal', 'usa loans_pledge'). "
                "No asumo una por defecto."
            )
        defaults = TARGET_COUNTER_DEFAULTS[target_account]["increase" if delta_nio > 0 else "decrease"]
        for account in defaults:
            label = self._account_label(account, {})
            if label and not self._postable_account_error(account, label):
                return account
        return defaults[0]

    def _target_adjustment_lines(self, *, target_account: str, counter_account: str, delta_nio: float) -> list[dict[str, Any]]:
        amount = round(abs(delta_nio), 2)
        target_spec = TARGET_BALANCE_ACCOUNTS[target_account]
        target_is_debit = target_spec["normal_balance"] == "debit"
        increase_target = delta_nio > 0
        target_side = "debit" if target_is_debit == increase_target else "credit"
        counter_side = "credit" if target_side == "debit" else "debit"
        raw_lines = [
            {"account": target_account, "debit": amount if target_side == "debit" else 0, "credit": amount if target_side == "credit" else 0},
            {"account": counter_account, "debit": amount if counter_side == "debit" else 0, "credit": amount if counter_side == "credit" else 0},
        ]
        return self._normalize_journal_lines(raw_lines, {})

    def _verify_target_balance_after_apply(self, result, proposal_data: Mapping[str, Any]) -> None:
        target = proposal_data.get("target") if isinstance(proposal_data.get("target"), Mapping) else {}
        account = str(target.get("account") or "")
        month = str(target.get("month") or "")[:7]
        target_nio = float(_to_float(target.get("target_amount_nio")) or 0.0)
        label = TARGET_BALANCE_ACCOUNTS.get(account, {}).get("label", "")
        if not label or not month:
            raise AgentValidationError("La propuesta de objetivo no tiene cuenta o mes verificable.")
        actual = _statement_value(result, label, month)
        if abs(actual - target_nio) > 1.0:
            raise AgentValidationError(
                f"El asiento no llevo {label} al objetivo. Objetivo C$ {target_nio:,.2f}, resultado C$ {actual:,.2f}."
            )

    def _prepare_create_account(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        name = str(args.get("name") or args.get("account_name") or "").strip()
        account_type = _normalize_account_type(args.get("account_type") or args.get("type"))
        section = _normalize_account_section(args.get("section"))
        if not name:
            raise AgentValidationError("Indique el nombre de la cuenta que desea crear.")
        if not account_type or not section:
            raise AgentValidationError("Indique tipo y seccion contable para la cuenta nueva.")
        if not _valid_type_section(account_type, section):
            raise AgentValidationError("La combinacion tipo-seccion no es valida para el catalogo contable.")

        code = _account_code(args.get("code") or name)
        if self.accounts.get_by_code(code) or self.accounts.get_by_name(name) or self.accounts.find_by_text(name):
            raise AgentValidationError(f"La cuenta {name} ya existe en el catalogo.")

        account_record = {
            "code": code,
            "name": name,
            "account_type": account_type,
            "section": section,
            "normal_balance": str(args.get("normal_balance") or ("debe" if account_type in {"activo", "gasto", "costo"} else "haber")),
            "parent_code": str(args.get("parent_code") or "").strip(),
            "aliases": [str(alias).strip() for alias in (args.get("aliases") or []) if str(alias).strip()] if isinstance(args.get("aliases"), list) else [],
            "source": "chat_financiero",
            "is_postable": bool(args.get("is_postable", True)),
            "instruction_id": command_id,
            "message": original_message,
        }
        projected = _append_dynamic_account(payload, account_record)
        rows = [{"account": name, "debit": 0, "credit": 0}]
        return projected, _proposal_payload(
            kind="create_account",
            title=f"Crear cuenta {name}",
            assistant_message=f'Voy a crear "{name}" como {account_type} en {section}. Confirmame si es correcto.',
            month=None,
            rows=rows,
            technical_records=[account_record],
            original_message=original_message,
            extra={"account": account_record},
        )

    def _prepare_finalizar_periodo(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        projected = deepcopy(dict(payload))
        return projected, _proposal_payload(
            kind="finalizar_periodo",
            title="Finalizar periodo",
            assistant_message="Esto marcara el periodo como finalizado. No podras editar el modelo despues de confirmar.",
            month=None,
            rows=[],
            technical_records=[],
            original_message=original_message,
        )

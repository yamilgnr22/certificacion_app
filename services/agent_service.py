from __future__ import annotations

import uuid
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy.orm import Session

from accounting_model import reverse_voucher
from financial_model import build_financial_model
from llm import LLMProvider, LLMProviderError, get_llm_provider
from model_chat import LEDGER_ACCOUNT_LABELS
from repositories import AccountRepository, AgentRepository, PeriodoRepository
from services.audit_service import AuditService, stable_hash
from services.agent_tools import AgentToolRegistry
from services.periodo_service import PeriodoService
from services.serializers import parse_json_object


PROMPT_VERSION = "agent-command-v1.0.0"


class AgentServiceError(ValueError):
    pass


class AgentConfigError(AgentServiceError):
    pass


class AgentValidationError(AgentServiceError):
    pass


class AgentNotFoundError(AgentServiceError):
    pass


class AgentProposalConflictError(AgentServiceError):
    pass


class AgentCommandService:
    """Orquestador del asistente contable nuevo.

    Fase 2A: solo consultas y navegacion. Las mutaciones financieras se mantienen
    fuera de este servicio hasta que existan propuestas auditables.
    """

    def __init__(self, session: Session, *, provider: LLMProvider | None = None):
        self.session = session
        self.periodos = PeriodoRepository(session)
        self.agent_repo = AgentRepository(session)
        self.accounts = AccountRepository(session)
        self.tools = AgentToolRegistry()
        self.provider = provider

    def handle_command(
        self,
        *,
        periodo_id: str,
        message: str,
        ui_context: Mapping[str, Any] | None = None,
        cpa_user: str = "system",
    ) -> dict[str, Any]:
        periodo_id = str(periodo_id or "").strip()
        message = str(message or "").strip()
        ui_context = dict(ui_context or {})
        if not periodo_id:
            raise AgentValidationError("Falta periodo_id para ejecutar el asistente.")
        if not message:
            raise AgentValidationError("Escriba una instruccion para el asistente.")

        periodo = self.periodos.get(periodo_id)
        if not periodo:
            raise AgentNotFoundError("Periodo no encontrado.")

        payload = parse_json_object(periodo.payload_json)
        command_id = f"cmd_{uuid.uuid4().hex[:12]}"
        try:
            provider = self.provider or self._provider_from_config()
            interpreted = provider.complete_json(
                system_prompt=_system_prompt(),
                user_prompt=_user_prompt(message=message, ui_context=ui_context),
                schema=AGENT_COMMAND_SCHEMA,
            )
        except LLMProviderError as exc:
            raise AgentConfigError(str(exc)) from exc
        except Exception as exc:
            raise AgentServiceError(f"No pude interpretar la instruccion: {type(exc).__name__}: {exc}") from exc

        intent = str(interpreted.get("intent") or "").strip()
        args = interpreted.get("args") if isinstance(interpreted.get("args"), dict) else {}
        if intent in {"", "clarification", "question"}:
            response = self._question_response(
                command_id=command_id,
                intent=intent or "question",
                message=str(interpreted.get("assistant_message") or interpreted.get("question") or "Necesito un poco mas de detalle para ayudarte."),
            )
        elif intent in MUTATION_INTENTS:
            response = self._proposal_response(
                command_id=command_id,
                intent=intent,
                periodo=periodo,
                payload=payload,
                args=args,
                ui_context=ui_context,
                original_message=message,
            )
        elif intent in self.tools.tools:
            tool_result = self.tools.run(intent, payload, args)
            response = self._tool_response(
                command_id=command_id,
                intent=intent,
                tool_result=tool_result,
            )
        else:
            response = self._question_response(
                command_id=command_id,
                intent=intent or "unknown",
                message=(
                    "Esa instruccion parece cambiar el modelo. En esta fase puedo consultar saldos, "
                    "mostrar mayores, abrir comprobantes y navegar. Para aplicar cambios usaremos propuestas auditables en la siguiente fase."
                ),
            )

        self.agent_repo.add_message(
            periodo_id=periodo.id,
            command_id=command_id,
            cpa_user=cpa_user,
            message=message,
            intent=response.get("intent"),
            response_type=response.get("response_type"),
            response=response,
        )
        self.session.commit()
        return response

    def apply_proposal(self, proposal_id: str, *, cpa_user: str = "system") -> dict[str, Any]:
        proposal_id = str(proposal_id or "").strip()
        if not proposal_id:
            raise AgentValidationError("Falta proposal_id.")
        proposal = self.agent_repo.get_proposal(proposal_id)
        if not proposal:
            raise AgentNotFoundError("Propuesta no encontrada.")
        if proposal.status != "pending":
            raise AgentProposalConflictError(f"La propuesta ya no esta pendiente. Estado actual: {proposal.status}.")

        now = datetime.now(timezone.utc)
        if _as_aware(proposal.expires_at) < now:
            proposal.status = "expired"
            self.session.commit()
            raise AgentProposalConflictError("La propuesta expiro, pedi de nuevo la instruccion.")

        periodo = self.periodos.get(proposal.periodo_id)
        if not periodo:
            raise AgentNotFoundError("Periodo de la propuesta no encontrado.")
        if periodo.estado != "borrador":
            raise AgentProposalConflictError("Solo se pueden aplicar propuestas en periodos borrador.")
        current_payload = parse_json_object(periodo.payload_json)
        current_hash = stable_hash(current_payload)
        if current_hash != proposal.payload_before_hash:
            proposal.status = "stale"
            self.session.commit()
            raise AgentProposalConflictError("El modelo cambio desde que se genero la propuesta. Pedi una propuesta nueva.")

        projected_payload = parse_json_object(proposal.projected_payload_json)
        if not projected_payload:
            raise AgentValidationError("La propuesta no tiene payload proyectado aplicable.")

        before = _periodo_snapshot(periodo)
        try:
            result = build_financial_model(projected_payload)
            periodo.payload_json = json.dumps(projected_payload, ensure_ascii=False, sort_keys=True, default=str)
            periodo.validation_json = json.dumps(result.validations, ensure_ascii=False, sort_keys=True, default=str)
            periodo.period_blocks_json = json.dumps(result.metadata.get("period_blocks") or [], ensure_ascii=False, sort_keys=True, default=str)
            _sync_period_fields(periodo, projected_payload)
            proposal.status = "applied"
            proposal.applied_at = now
            self.session.flush()

            proposal_payload = parse_json_object(proposal.proposal_json)
            if proposal_payload.get("kind") == "create_account":
                self._apply_account_creation(proposal_payload, command_id=proposal.command_id, cpa_user=cpa_user)
            after = _periodo_snapshot(periodo)
            AuditService(self.session).log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="agent_apply_proposal",
                summary=str(proposal_payload.get("title") or "Aplico propuesta del asistente contable"),
                before=before,
                after=after,
                metadata={
                    "command_id": proposal.command_id,
                    "proposal_id": proposal.id,
                    "prompt_version": PROMPT_VERSION,
                    "tool_versions": self.tools.versions(),
                    "proposal_kind": proposal_payload.get("kind"),
                },
            )
            self.session.commit()
            return {
                "ok": True,
                "proposal_id": proposal.id,
                "status": proposal.status,
                "assistant_message": "Listo, aplique la propuesta al periodo.",
                "periodo_id": periodo.id,
            }
        except Exception:
            self.session.rollback()
            raise

    def discard_proposal(self, proposal_id: str, *, cpa_user: str = "system") -> dict[str, Any]:
        proposal_id = str(proposal_id or "").strip()
        proposal = self.agent_repo.get_proposal(proposal_id)
        if not proposal:
            raise AgentNotFoundError("Propuesta no encontrada.")
        if proposal.status != "pending":
            raise AgentProposalConflictError(f"La propuesta ya no esta pendiente. Estado actual: {proposal.status}.")
        proposal.status = "discarded"
        proposal.discarded_at = datetime.now(timezone.utc)
        AuditService(self.session).log(
            cpa_user=cpa_user,
            entity_type="periodo",
            entity_id=proposal.periodo_id,
            action="agent_discard_proposal",
            summary="Descarto propuesta del asistente contable",
            metadata={"command_id": proposal.command_id, "proposal_id": proposal.id},
        )
        self.session.commit()
        return {"ok": True, "proposal_id": proposal.id, "status": proposal.status}

    @staticmethod
    def _provider_from_config() -> LLMProvider:
        return get_llm_provider()

    def _tool_response(self, *, command_id: str, intent: str, tool_result: Mapping[str, Any]) -> dict[str, Any]:
        response_type = str(tool_result.get("response_type") or "answer")
        return {
            "ok": True,
            "command_id": command_id,
            "intent": intent,
            "response_type": response_type,
            "assistant_message": str(tool_result.get("assistant_message") or ""),
            "ui_actions": list(tool_result.get("ui_actions") or []),
            "requires_confirmation": False,
            "audit": self._audit_metadata(command_id),
        }

    def _proposal_response(
        self,
        *,
        command_id: str,
        intent: str,
        periodo,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        ui_context: Mapping[str, Any],
        original_message: str,
    ) -> dict[str, Any]:
        projected_payload, proposal_payload = self._build_projected_payload(
            intent=intent,
            payload=payload,
            args=args,
            ui_context=ui_context,
            command_id=command_id,
            original_message=original_message,
        )
        payload_hash = stable_hash(payload)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        record = self.agent_repo.add_proposal(
            periodo_id=periodo.id,
            command_id=command_id,
            payload_before_hash=payload_hash,
            proposal_json=json.dumps(proposal_payload, ensure_ascii=False, sort_keys=True, default=str),
            projected_payload_json=json.dumps(projected_payload, ensure_ascii=False, sort_keys=True, default=str),
            expires_at=expires_at,
        )
        proposal_payload["id"] = record.id
        proposal_payload["expires_at"] = expires_at.isoformat()
        return {
            "ok": True,
            "command_id": command_id,
            "intent": intent,
            "response_type": "proposal",
            "assistant_message": proposal_payload.get("assistant_message") or proposal_payload.get("title") or "Prepare una propuesta para revisar.",
            "proposal": proposal_payload,
            "requires_confirmation": True,
            "ui_actions": [{"type": "show_proposal", "proposal_id": record.id}],
            "audit": self._audit_metadata(command_id),
        }

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
        if intent in {"journal_entry", "account_transfer", "year_close_transfer"}:
            return self._prepare_journal_entry(intent, payload, args, command_id, original_message)
        if intent == "assumption_change":
            return self._prepare_assumption_change(payload, args, ui_context, command_id, original_message)
        if intent == "create_account":
            return self._prepare_create_account(payload, args, command_id, original_message)
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
        result = build_financial_model(payload)
        voucher = next((dict(v) for v in result.accounting.get("vouchers", []) if str(v.get("voucher_id") or "").upper() == voucher_id), None)
        if not voucher:
            raise AgentValidationError(f"No encontre el comprobante {voucher_id}.")
        reversal = reverse_voucher(voucher, voucher_id=f"REV-{voucher_id}")
        reversal["source"] = "chat_financiero"
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
        )

    def _prepare_journal_entry(
        self,
        intent: str,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        target_month = str(args.get("month") or args.get("target_month") or _last_payload_month(payload))[:7]
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
        if not target_month:
            raise AgentValidationError("Indique el mes de la partida.")
        if not debit or not credit:
            raise AgentValidationError("Indique cuenta al debe y cuenta al haber.")
        if debit == credit:
            raise AgentValidationError("La cuenta al debe y al haber deben ser diferentes.")
        if amount is None or amount <= 0:
            raise AgentValidationError("Indique un monto positivo para la partida.")
        debit_label = self._account_label(debit, payload)
        credit_label = self._account_label(credit, payload)
        if not debit_label or not credit_label:
            raise AgentValidationError("Solo se permiten cuentas del catalogo contable actual.")

        entry = {
            "month": target_month,
            "debit_account": debit,
            "credit_account": credit,
            "amount": round(float(amount), 2),
            "currency": "nio",
            "entry_type": intent,
            "source": "chat_financiero",
            "instruction_id": command_id,
            "locked": True,
            "message": original_message,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if source_month:
            entry["source_month"] = source_month
        projected = _append_journal_entry(payload, entry)
        projected = self._ensure_dynamic_accounts_in_payload(projected, [debit, credit])
        build_financial_model(projected)
        rows = [
            {"account": debit_label, "debit": amount, "credit": 0},
            {"account": credit_label, "debit": 0, "credit": amount},
        ]
        return projected, _proposal_payload(
            kind="journal_entry",
            title=_journal_title(intent),
            assistant_message=f"Registro contable propuesto: debita {debit_label} y acredita {credit_label} por {amount:,.0f}.",
            month=target_month,
            rows=rows,
            technical_records=[entry],
            original_message=original_message,
            extra={"source_month": source_month or None},
        )

    def _account_label(self, account: str, payload: Mapping[str, Any]) -> str:
        if account in LEDGER_ACCOUNT_LABELS:
            return LEDGER_ACCOUNT_LABELS[account]
        dynamic = _dynamic_account_lookup(payload)
        if account in dynamic:
            return dynamic[account]["name"]
        found = self.accounts.get_by_code(account) or self.accounts.get_by_name(account)
        return found.name if found else ""

    def _ensure_dynamic_accounts_in_payload(self, payload: Mapping[str, Any], accounts: list[str]) -> dict[str, Any]:
        projected = deepcopy(dict(payload or {}))
        for account in accounts:
            if account in LEDGER_ACCOUNT_LABELS:
                continue
            if account in _dynamic_account_lookup(projected):
                continue
            found = self.accounts.get_by_code(account) or self.accounts.get_by_name(account)
            if found:
                projected = _append_dynamic_account(projected, _account_catalog_payload(found))
        return projected

    def _prepare_assumption_change(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        ui_context: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        assumption = str(args.get("assumption") or "cost_pct").strip()
        if assumption != "cost_pct":
            raise AgentValidationError("Por ahora solo puedo preparar cambios de costo de venta.")
        value = _to_float(args.get("value") or args.get("cost_pct"))
        variability = _to_float(args.get("cost_variability_pct") or args.get("variability_pct") or args.get("cash_variability_pct"))
        if value is None or value <= 0 or value >= 100:
            raise AgentValidationError("Indique un porcentaje de costo de venta valido.")
        scope = str(args.get("scope") or ui_context.get("scope_mode") or ui_context.get("scope") or "global").lower()
        result = build_financial_model(payload)
        months = result.summary.get("all_months") or result.summary.get("months") or []
        if scope in {"block", "bloque", "selected_block"}:
            block = ui_context.get("selected_block") if isinstance(ui_context.get("selected_block"), Mapping) else {}
            months = _months_between(str(block.get("start_month") or months[0]), str(block.get("end_month") or months[-1]))
        projected = _apply_cost_assumption(payload, months=months, cost_pct=value, cost_variability_pct=variability, global_scope=scope not in {"block", "bloque", "selected_block"})
        build_financial_model(projected)
        rows = [{"account": "Costo de venta", "debit": value, "credit": variability or 0}]
        return projected, _proposal_payload(
            kind="assumption_change",
            title="Cambiar supuesto de costo de venta",
            assistant_message=f"Propuesta: costo de venta {value:g}%{f' +/- {variability:g}%' if variability is not None else ''} para {'el bloque seleccionado' if scope in {'block', 'bloque', 'selected_block'} else 'todo el modelo'}.",
            month=",".join(months[:2] + (["..."] if len(months) > 2 else [])),
            rows=rows,
            technical_records=[{"assumption": assumption, "value": value, "cost_variability_pct": variability, "scope": scope, "months": months}],
            original_message=original_message,
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
        if self.accounts.get_by_code(code) or self.accounts.get_by_name(name):
            raise AgentValidationError(f"La cuenta {name} ya existe en el catalogo.")

        account_record = {
            "code": code,
            "name": name,
            "account_type": account_type,
            "section": section,
            "source": "chat_financiero",
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

    def _apply_account_creation(self, proposal_payload: Mapping[str, Any], *, command_id: str, cpa_user: str) -> None:
        account = dict(proposal_payload.get("account") or {})
        code = str(account.get("code") or "").strip()
        name = str(account.get("name") or "").strip()
        if not code or not name:
            raise AgentValidationError("La propuesta no contiene una cuenta valida para crear.")
        if self.accounts.get_by_code(code) or self.accounts.get_by_name(name):
            return
        account = self.accounts.create(
            id=code,
            code=code,
            name=name,
            account_type=str(account.get("account_type") or ""),
            section=str(account.get("section") or ""),
            source=str(account.get("source") or "chat_financiero"),
        )
        AuditService(self.session).log(
            cpa_user=cpa_user,
            entity_type="account_catalog",
            entity_id=account.id,
            action="agent_create_account",
            summary=f"Creo cuenta {account.name}",
            metadata={
                "command_id": command_id,
                "prompt_version": PROMPT_VERSION,
                "tool_versions": self.tools.versions(),
                "account": _account_catalog_payload(account),
            },
        )

    def _question_response(self, *, command_id: str, intent: str, message: str) -> dict[str, Any]:
        return {
            "ok": True,
            "command_id": command_id,
            "intent": intent,
            "response_type": "question",
            "assistant_message": message,
            "ui_actions": [],
            "requires_confirmation": False,
            "audit": self._audit_metadata(command_id),
        }

    def _audit_metadata(self, command_id: str) -> dict[str, Any]:
        return {
            "command_id": command_id,
            "source": "agent_contable",
            "prompt_version": PROMPT_VERSION,
            "tool_versions": self.tools.versions(),
        }


AGENT_COMMAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "explain_balance",
                "show_ledger",
                "show_voucher",
                "navigate",
                "reverse_voucher",
                "journal_entry",
                "account_transfer",
                "year_close_transfer",
                "assumption_change",
                "create_account",
                "question",
                "unsupported",
            ],
        },
        "args": {"type": "object"},
        "assistant_message": {"type": "string"},
    },
    "required": ["intent", "args"],
}


MUTATION_INTENTS = {"reverse_voucher", "journal_entry", "account_transfer", "year_close_transfer", "assumption_change", "create_account"}


def _system_prompt() -> str:
    return (
        "Sos un asistente contable dentro de una app de certificaciones. "
        "Interpretas instrucciones contables y devuelves una accion estructurada; no calcules saldos finales. "
        "Respondé exclusivamente JSON valido con intent y args. "
        "Intents permitidos: explain_balance(account, month), show_ledger(account), "
        "show_voucher(voucher_id), navigate(target), reverse_voucher(voucher_id), "
        "journal_entry(month, debit_account, credit_account, amount), "
        "account_transfer(month, source_account, destination_account, amount), "
        "year_close_transfer(target_month, source_month, amount opcional), "
        "assumption_change(assumption=cost_pct, value, cost_variability_pct, scope), "
        "create_account(name, account_type, section), question. "
        "Si falta cuenta, mes o monto, usa question. Si el usuario pide crear una cuenta nueva, usa create_account."
    )


def _user_prompt(*, message: str, ui_context: Mapping[str, Any]) -> str:
    return (
        "Mensaje del usuario:\n"
        f"{message}\n\n"
        "Contexto UI disponible:\n"
        f"{dict(ui_context or {})}\n\n"
        "Ejemplos de cuentas validas: Efectivo y Equivalentes de Efectivo, "
        "Resultados Acumulados, Resultados del Ejercicio, Inventarios, Proveedores."
    )


def _proposal_payload(
    *,
    kind: str,
    title: str,
    assistant_message: str,
    month: str | None,
    rows: list[dict[str, Any]],
    technical_records: list[dict[str, Any]],
    original_message: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = {
        "kind": kind,
        "title": title,
        "assistant_message": assistant_message,
        "month": month,
        "journal_rows": rows,
        "technical_records": technical_records,
        "original_message": original_message,
    }
    if extra:
        data.update(extra)
    return data


def _append_saved_voucher(payload: Mapping[str, Any], voucher: Mapping[str, Any]) -> dict[str, Any]:
    projected = deepcopy(dict(payload or {}))
    accounting = dict(projected.get("accounting") or {})
    vouchers = list(accounting.get("vouchers") or [])
    vouchers.append(dict(voucher))
    accounting["vouchers"] = vouchers
    projected["accounting"] = accounting
    return projected


def _append_journal_entry(payload: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    projected = deepcopy(dict(payload or {}))
    movements = dict(projected.get("movements") or {})
    entries = list(movements.get("journal_entries") or [])
    entries.append(dict(entry))
    movements["journal_entries"] = entries
    projected["movements"] = movements
    return projected


def _append_dynamic_account(payload: Mapping[str, Any], account: Mapping[str, Any]) -> dict[str, Any]:
    projected = deepcopy(dict(payload or {}))
    accounting = dict(projected.get("accounting") or {})
    accounts = list(accounting.get("dynamic_accounts") or [])
    clean = {
        "code": str(account.get("code") or "").strip(),
        "name": str(account.get("name") or "").strip(),
        "account_type": _normalize_account_type(account.get("account_type")),
        "section": _normalize_account_section(account.get("section")),
        "source": str(account.get("source") or "chat_financiero").strip(),
    }
    existing_codes = {str(item.get("code") or "").strip() for item in accounts if isinstance(item, Mapping)}
    existing_names = {str(item.get("name") or "").strip().lower() for item in accounts if isinstance(item, Mapping)}
    if clean["code"] and clean["name"].lower() not in existing_names and clean["code"] not in existing_codes:
        accounts.append(clean)
    accounting["dynamic_accounts"] = accounts
    projected["accounting"] = accounting
    return projected


def _dynamic_account_lookup(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    accounting = dict((payload or {}).get("accounting") or {})
    lookup: dict[str, dict[str, Any]] = {}
    for item in accounting.get("dynamic_accounts") or []:
        if not isinstance(item, Mapping):
            continue
        account = dict(item)
        for value in [account.get("code"), account.get("name")]:
            key = str(value or "").strip()
            if key:
                lookup[key] = account
    return lookup


def _account_catalog_payload(account: Any) -> dict[str, Any]:
    return {
        "code": account.code,
        "name": account.name,
        "account_type": account.account_type,
        "section": account.section,
        "source": account.source,
    }


def _voucher_rows(voucher: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"account": line.get("account"), "debit": line.get("debit") or 0, "credit": line.get("credit") or 0}
        for line in voucher.get("lines") or []
    ]


def _to_float(value: Any) -> float | None:
    try:
        if value in {None, ""}:
            return None
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def _account_code(value: Any) -> str:
    import re
    import unicodedata

    raw = unicodedata.normalize("NFKD", str(value or ""))
    plain = "".join(ch for ch in raw if not unicodedata.combining(ch)).lower()
    code = re.sub(r"[^a-z0-9]+", "_", plain).strip("_")
    return code[:100] or f"cuenta_{uuid.uuid4().hex[:8]}"


def _normalize_account_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "asset": "activo",
        "activo": "activo",
        "liability": "pasivo",
        "pasivo": "pasivo",
        "equity": "patrimonio",
        "patrimonio": "patrimonio",
        "revenue": "ingreso",
        "ingreso": "ingreso",
        "income": "ingreso",
        "expense": "gasto",
        "gasto": "gasto",
        "cost": "costo",
        "costo": "costo",
    }
    return aliases.get(raw, raw)


def _normalize_account_section(value: Any) -> str:
    raw = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "current": "corriente",
        "corriente": "corriente",
        "non_current": "no_corriente",
        "nocorriente": "no_corriente",
        "no_corriente": "no_corriente",
        "patrimonio": "patrimonio",
        "equity": "patrimonio",
        "ingresos": "ingresos",
        "revenue": "ingresos",
        "costo_ventas": "costo_ventas",
        "costo_de_ventas": "costo_ventas",
        "gastos_operativos": "gastos_operativos",
        "gasto_operativo": "gastos_operativos",
        "gastos_financieros": "gastos_financieros",
        "gasto_financiero": "gastos_financieros",
    }
    return aliases.get(raw, raw)


def _valid_type_section(account_type: str, section: str) -> bool:
    allowed = {
        "activo": {"corriente", "no_corriente"},
        "pasivo": {"corriente", "no_corriente"},
        "patrimonio": {"patrimonio"},
        "ingreso": {"ingresos"},
        "costo": {"costo_ventas"},
        "gasto": {"gastos_operativos", "gastos_financieros"},
    }
    return section in allowed.get(account_type, set())


def _last_payload_month(payload: Mapping[str, Any]) -> str:
    period = dict(payload.get("period") or {})
    return str(period.get("end_month") or "")[:7]


def _previous_year_end(month: str) -> str:
    year = int(str(month or "0000-01")[:4] or 0)
    return f"{year - 1}-12"


def _statement_value(result, description: str, month: str) -> float:
    df = result.df_esf_mensual_full
    if not month or month not in df.columns:
        return 0.0
    rows = df[df["Descripcion"] == description]
    if rows.empty:
        return 0.0
    try:
        return float(rows.iloc[0][month] or 0)
    except Exception:
        return 0.0


def _journal_title(intent: str) -> str:
    if intent == "year_close_transfer":
        return "Cierre de resultados a acumulados"
    if intent == "account_transfer":
        return "Reclasificacion contable"
    return "Partida doble"


def _apply_cost_assumption(
    payload: Mapping[str, Any],
    *,
    months: list[str],
    cost_pct: float,
    cost_variability_pct: float | None,
    global_scope: bool,
) -> dict[str, Any]:
    projected = deepcopy(dict(payload or {}))
    income = dict(projected.get("income") or {})
    if global_scope:
        income["cost_pct"] = cost_pct
        if cost_variability_pct is not None:
            income["cost_variability_pct"] = cost_variability_pct
        overrides = _income_overrides_by_month(income.get("monthly_overrides") or [])
        for month in months:
            if month in overrides:
                overrides[month].pop("cost_pct", None)
                overrides[month].pop("cost_variability_pct", None)
        income["monthly_overrides"] = _income_overrides_list(overrides)
    else:
        overrides = _income_overrides_by_month(income.get("monthly_overrides") or [])
        for month in months:
            record = dict(overrides.get(month) or {})
            record["month"] = month
            record["cost_pct"] = cost_pct
            if cost_variability_pct is not None:
                record["cost_variability_pct"] = cost_variability_pct
            overrides[month] = record
        income["monthly_overrides"] = _income_overrides_list(overrides)
    projected["income"] = income
    return projected


def _income_overrides_by_month(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, Mapping):
        raw = [dict({"month": k}, **dict(v or {})) for k, v in raw.items()]
    for item in raw or []:
        if isinstance(item, Mapping) and item.get("month"):
            out[str(item["month"])[:7]] = dict(item)
    return out


def _income_overrides_list(overrides: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for month in sorted(overrides):
        record = dict(overrides[month])
        record["month"] = month
        if any(k != "month" for k in record):
            out.append(record)
    return out


def _months_between(start: str, end: str) -> list[str]:
    import pandas as pd

    try:
        return [d.strftime("%Y-%m") for d in pd.period_range(start=start[:7], end=end[:7], freq="M")]
    except Exception:
        return []


def _sync_period_fields(periodo, payload: Mapping[str, Any]) -> None:
    period = dict(payload.get("period") or {})
    income = dict(payload.get("income") or {})
    if period.get("start_month"):
        periodo.mes_inicial = str(period["start_month"])[:7]
    if period.get("end_month"):
        periodo.mes_final = str(period["end_month"])[:7]
    if period.get("exchange_rate") is not None:
        periodo.tasa_cambio = float(period["exchange_rate"])
    periodo.seed = str(period.get("seed") or periodo.seed or "")
    for attr, key in [
        ("ingresos_base_usd", "base_income_usd"),
        ("variabilidad_ingresos_pct", "income_variability_pct"),
        ("cost_pct", "cost_pct"),
        ("variabilidad_costos_pct", "cost_variability_pct"),
        ("cash_sales_pct", "cash_sales_pct"),
    ]:
        if income.get(key) is not None:
            setattr(periodo, attr, float(income[key]))


def _periodo_snapshot(periodo) -> dict[str, Any]:
    return {
        "id": periodo.id,
        "estado": periodo.estado,
        "mes_inicial": periodo.mes_inicial,
        "mes_final": periodo.mes_final,
        "payload_hash": stable_hash(parse_json_object(periodo.payload_json)),
    }


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value

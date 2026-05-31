from __future__ import annotations

import uuid
import json
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy.orm import Session

from accounting_accounts import LEDGER_ACCOUNT_LABELS
from accounting_model import reverse_voucher
from financial_model import build_financial_model
from llm import LLMProvider, LLMProviderError, get_llm_provider
from repositories import AccountRepository, AgentRepository, PeriodoRepository
from services.audit_service import AuditService, stable_hash
from services.agent_errors import (
    AgentCatalogChangedError,
    AgentConfigError,
    AgentNotFoundError,
    AgentProposalConflictError,
    AgentServiceError,
    AgentValidationError,
)
from services.agent_helpers import (
    ASSUMPTION_FIELD_MAP,
    _account_catalog_payload,
    _account_code,
    _append_dynamic_account,
    _append_journal_entry,
    _append_saved_voucher,
    _apply_assumption_change,
    _apply_cost_assumption,
    _as_aware,
    _dynamic_account_lookup,
    _elapsed_seconds,
    _er_value,
    _extract_target_month,
    _find_reversal_for,
    _has_correction_payload,
    _impact_deltas,
    _income_overrides_by_month,
    _income_overrides_list,
    _journal_entry_from_reversal_voucher,
    _journal_pair,
    _journal_title,
    _journal_totals,
    _last_payload_month,
    _months_between,
    _normalize_account_section,
    _normalize_account_type,
    _normalize_assumption_field,
    _normalize_assumption_value,
    _payload_months,
    _periodo_snapshot,
    _previous_year_end,
    _proposal_payload,
    _resolve_correctable_voucher,
    _saved_vouchers,
    _statement_value,
    _sync_period_fields,
    _system_prompt,
    _to_float,
    _unique_reversal_id,
    _user_prompt,
    _valid_account_suggestions,
    _valid_type_section,
    _voucher_rows,
)
from services.agent_tools import AgentToolRegistry
from services.agent_planner import AgentPlanError, AgentPlanner
from services.periodo_service import PeriodoService
from services.rollforward_service import RollforwardService
from services.serializers import parse_json_object


PROMPT_VERSION = "agent-command-v1.0.0"
MAX_TOOL_CALLS_PER_TURN = 3
MAX_TURN_DURATION_S = 30.0


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
        self.planner = AgentPlanner()
        self.provider = provider

    def handle_command(
        self,
        *,
        periodo_id: str,
        message: str,
        ui_context: Mapping[str, Any] | None = None,
        current_payload: Mapping[str, Any] | None = None,
        is_dirty: bool = False,
        cpa_user: str = "system",
    ) -> dict[str, Any]:
        started_at = time.monotonic()
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

        persisted_payload = parse_json_object(periodo.payload_json)
        dirty_payload = dict(current_payload or {}) if isinstance(current_payload, Mapping) else {}
        used_dirty_payload = bool(is_dirty and dirty_payload)
        payload = dirty_payload if used_dirty_payload else persisted_payload
        command_id = str(ui_context.get("command_id") or "").strip() or f"cmd_{uuid.uuid4().hex[:12]}"
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

        if _elapsed_seconds(started_at) > MAX_TURN_DURATION_S:
            response = self._decorate_response(
                self._timeout_response(command_id=command_id),
                command_id=command_id,
                intent="timeout",
                used_dirty_payload=used_dirty_payload,
                started_at=started_at,
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

        intent = str(interpreted.get("intent") or "").strip()
        args = interpreted.get("args") if isinstance(interpreted.get("args"), dict) else {}
        args = self._apply_short_memory(intent, args, periodo_id=periodo.id, cpa_user=cpa_user)
        if intent in {"", "clarification", "question"}:
            response = self._question_response(
                command_id=command_id,
                intent=intent or "question",
                message=str(interpreted.get("assistant_message") or interpreted.get("question") or "Necesito un poco mas de detalle para ayudarte."),
            )
        elif intent in MUTATION_INTENTS:
            if used_dirty_payload:
                message_text = "Guarda los cambios antes de preparar una correccion contable." if intent == "correct_voucher" else "Guarda los cambios antes de preparar una propuesta contable."
                response = self._question_response(
                    command_id=command_id,
                    intent=intent,
                    message=message_text,
                )
            elif intent == "compound_plan":
                try:
                    self.planner.validate(interpreted)
                except AgentPlanError as exc:
                    response = self._question_response(command_id=command_id, intent=intent, message=str(exc))
                else:
                    response = self._proposal_response(
                        command_id=command_id,
                        intent=intent,
                        periodo=periodo,
                        payload=payload,
                        args={"steps": interpreted.get("steps") or []},
                        ui_context=ui_context,
                        original_message=message,
                    )
            elif intent == "correct_voucher" and not _has_correction_payload(args):
                response = self._question_response(
                    command_id=command_id,
                    intent=intent,
                    message="Indique el asiento corregido con mes, descripcion y lineas al debe y haber.",
                )
            else:
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
        elif intent in SYSTEM_INTENTS:
            response = self._handle_system_intent(
                command_id=command_id,
                intent=intent,
                periodo=periodo,
                payload=payload,
                args=args,
                cpa_user=cpa_user,
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

        response = self._decorate_response(
            response,
            command_id=command_id,
            intent=intent,
            used_dirty_payload=used_dirty_payload,
            started_at=started_at,
        )
        if used_dirty_payload and response.get("response_type") in {"answer", "navigation", "proposal"}:
            response["assistant_message"] = "Segun los cambios sin guardar en pantalla: " + str(response.get("assistant_message") or "")

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

        proposal_data = parse_json_object(proposal.proposal_json)
        if proposal_data.get("kind") == "compound_agent_proposal":
            stale_reason = self._compound_catalog_stale_reason(proposal_data)
            if stale_reason:
                proposal.status = "stale"
                self.session.commit()
                raise AgentProposalConflictError(stale_reason)
        if proposal_data.get("kind") == "finalizar_periodo":
            return self._apply_finalizar_periodo(proposal, periodo, proposal_data=proposal_data, cpa_user=cpa_user)

        projected_payload = parse_json_object(proposal.projected_payload_json)
        if not projected_payload:
            raise AgentValidationError("La propuesta no tiene payload proyectado aplicable.")

        before = _periodo_snapshot(periodo)
        try:
            if proposal_data.get("kind") == "compound_agent_proposal":
                for account in proposal_data.get("account_operations") or []:
                    if isinstance(account, Mapping):
                        self._apply_account_creation({"account": dict(account)}, command_id=proposal.command_id, cpa_user=cpa_user, strict=True)
            result = build_financial_model(projected_payload)
            periodo.payload_json = json.dumps(projected_payload, ensure_ascii=False, sort_keys=True, default=str)
            periodo.validation_json = json.dumps(result.validations, ensure_ascii=False, sort_keys=True, default=str)
            periodo.period_blocks_json = json.dumps(result.metadata.get("period_blocks") or [], ensure_ascii=False, sort_keys=True, default=str)
            _sync_period_fields(periodo, projected_payload)
            RollforwardService(self.session).cache_saldos_finales(periodo)
            proposal.status = "applied"
            proposal.applied_at = now
            self.session.flush()

            if proposal_data.get("kind") == "create_account":
                self._apply_account_creation(proposal_data, command_id=proposal.command_id, cpa_user=cpa_user)
            metadata = {
                "command_id": proposal.command_id,
                "proposal_id": proposal.id,
                "prompt_version": PROMPT_VERSION,
                "tool_versions": self.tools.versions(),
                "proposal_kind": proposal_data.get("kind"),
            }
            if proposal_data.get("kind") == "voucher_reversal":
                metadata["original_voucher_id"] = proposal_data.get("original_voucher_id")
                metadata["reversal_voucher_id"] = proposal_data.get("reversal_voucher_id")
            if proposal_data.get("kind") == "compound_voucher_correction":
                metadata["original_voucher_id"] = proposal_data.get("original_voucher_id")
                metadata["reversal_voucher_id"] = proposal_data.get("reversal_voucher_id")
                metadata["correction_entry_id"] = proposal_data.get("correction_entry_id")
            if proposal_data.get("kind") == "compound_agent_proposal":
                metadata["compound_type"] = proposal_data.get("compound_type")
                metadata["execution_plan"] = proposal_data.get("execution_plan")
                metadata["user_visible_steps"] = proposal_data.get("user_visible_steps")
                if proposal_data.get("original_voucher_id"):
                    metadata["original_voucher_id"] = proposal_data.get("original_voucher_id")
                if proposal_data.get("reversal_voucher_id"):
                    metadata["reversal_voucher_id"] = proposal_data.get("reversal_voucher_id")
                if proposal_data.get("correction_entry_id"):
                    metadata["correction_entry_id"] = proposal_data.get("correction_entry_id")
                metadata["created_account_codes"] = [
                    str(account.get("code") or "")
                    for account in proposal_data.get("account_operations") or []
                    if isinstance(account, Mapping)
                ]
            after = _periodo_snapshot(periodo)
            AuditService(self.session).log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="agent_apply_compound_proposal" if proposal_data.get("kind") == "compound_agent_proposal" else "agent_apply_proposal",
                summary=str(proposal_data.get("title") or "Aplico propuesta del asistente contable"),
                before=before,
                after=after,
                metadata=metadata,
            )
            self.session.commit()
            return {
                "ok": True,
                "proposal_id": proposal.id,
                "status": proposal.status,
                "assistant_message": "Listo, aplique la propuesta al periodo.",
                "periodo_id": periodo.id,
            }
        except AgentCatalogChangedError:
            self.session.rollback()
            if proposal_data.get("kind") == "compound_agent_proposal":
                self._mark_proposal_stale_after_rollback(proposal_id)
                raise AgentProposalConflictError("El catalogo cambio mientras aplicabas la propuesta. Pedi una propuesta nueva.")
            raise
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

    def _apply_short_memory(self, intent: str, args: Mapping[str, Any], *, periodo_id: str, cpa_user: str) -> dict[str, Any]:
        args = dict(args or {})
        if intent != "journal_entry" or not bool(args.get("repeat_last")):
            return args
        previous = self._last_applied_journal_entry(periodo_id=periodo_id)
        if not previous:
            raise AgentValidationError("No encontre un asiento aplicado reciente para repetir.")
        lines = deepcopy(list(previous.get("lines") or []))
        if not lines:
            raise AgentValidationError("El ultimo asiento no tiene lineas reutilizables.")
        amount = _to_float(args.get("amount") or args.get("monto"))
        if amount is not None:
            if len(lines) != 2:
                raise AgentValidationError("No puedo cambiar el monto de un asiento con mas de dos lineas. Pasame el asiento completo.")
            for line in lines:
                if float(line.get("debit") or 0) > 0:
                    line["debit"] = amount
                    line["credit"] = 0
                elif float(line.get("credit") or 0) > 0:
                    line["debit"] = 0
                    line["credit"] = amount
        args["lines"] = lines
        args["description"] = str(args.get("description") or previous.get("description") or "Asiento repetido")
        args["month"] = str(args.get("month") or args.get("target_month") or previous.get("month") or "")[:7]
        args["memory_source"] = {
            "proposal_id": previous.get("proposal_id"),
            "entry_id": previous.get("entry_id"),
            "month": previous.get("month"),
        }
        return args

    def _last_applied_journal_entry(self, *, periodo_id: str) -> dict[str, Any] | None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        for proposal in self.agent_repo.recent_applied_proposals(periodo_id=periodo_id, limit=10):
            applied_at = proposal.applied_at or proposal.created_at
            if applied_at and _as_aware(applied_at) < cutoff:
                continue
            proposal_data = parse_json_object(proposal.proposal_json)
            records = list(proposal_data.get("technical_records") or [])
            for record in reversed(records):
                if not isinstance(record, Mapping):
                    continue
                if record.get("lines") and str(record.get("entry_type") or "") not in {"voucher_reversal"}:
                    entry = dict(record)
                    entry["proposal_id"] = proposal.id
                    return entry
        return None

    def _tool_response(self, *, command_id: str, intent: str, tool_result: Mapping[str, Any]) -> dict[str, Any]:
        response_type = str(tool_result.get("response_type") or "answer")
        return {
            "ok": True,
            "command_id": command_id,
            "intent": intent,
            "response_type": response_type,
            "assistant_message": str(tool_result.get("assistant_message") or ""),
            "ui_actions": list(tool_result.get("ui_actions") or []),
            "data": dict(tool_result.get("data") or {}),
            "requires_confirmation": False,
            "audit": self._audit_metadata(command_id),
        }

    def _decorate_response(
        self,
        response: Mapping[str, Any],
        *,
        command_id: str,
        intent: str,
        used_dirty_payload: bool,
        started_at: float,
    ) -> dict[str, Any]:
        out = dict(response)
        tool_versions_used = self.tools.versions_used([intent])
        duration_ms = round(_elapsed_seconds(started_at) * 1000, 2)
        out["prompt_version"] = PROMPT_VERSION
        out["tool_versions_used"] = tool_versions_used
        out["used_dirty_payload"] = bool(used_dirty_payload)
        out["tool_call_count"] = 1 if tool_versions_used else 0
        out["max_tool_calls_per_turn"] = MAX_TOOL_CALLS_PER_TURN
        out["duration_ms"] = duration_ms
        audit = dict(out.get("audit") or {})
        audit.update({
            "command_id": command_id,
            "source": "agent_contable",
            "prompt_version": PROMPT_VERSION,
            "tool_versions_used": tool_versions_used,
            "used_dirty_payload": bool(used_dirty_payload),
            "tool_call_count": out["tool_call_count"],
            "duration_ms": duration_ms,
        })
        out["audit"] = audit
        return out

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
        if intent in {"reverse_voucher", "correct_voucher"} and getattr(periodo, "estado", "") != "borrador":
            raise AgentValidationError("Solo puedo preparar reversos y correcciones en periodos borrador.")
        projected_payload, proposal_payload = self._build_projected_payload(
            intent=intent,
            payload=payload,
            args=args,
            ui_context=ui_context,
            command_id=command_id,
            original_message=original_message,
        )
        proposal_payload["impact"] = self._compute_impact(
            payload, projected_payload, intent,
            target_month=_extract_target_month(proposal_payload, args),
        )
        if proposal_payload.get("kind") == "assumption_change_proposal":
            proposal_payload["assumption_impact"] = self._compute_assumption_impact(payload, projected_payload)
        elif proposal_payload.get("kind") == "journal_entry_proposal":
            proposal_payload["journal_impact"] = _impact_deltas(proposal_payload.get("impact") or {})
        payload_hash = stable_hash(payload)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.agent_repo.supersede_pending_for_command(command_id)
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
        if intent == "correct_voucher":
            return self._prepare_correct_voucher(payload, args, command_id, original_message)
        if intent == "compound_plan":
            return self._prepare_compound_plan(payload, args, command_id, original_message)
        if intent in {"journal_entry", "account_transfer", "year_close_transfer"}:
            return self._prepare_journal_entry(intent, payload, args, command_id, original_message)
        if intent == "assumption_change":
            return self._prepare_assumption_change(payload, args, ui_context, command_id, original_message)
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
        target_month = str(args.get("month") or args.get("target_month") or _last_payload_month(payload))[:7]
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

    def _account_label(self, account: str, payload: Mapping[str, Any]) -> str:
        if account in LEDGER_ACCOUNT_LABELS:
            return LEDGER_ACCOUNT_LABELS[account]
        dynamic = _dynamic_account_lookup(payload)
        if account in dynamic:
            return dynamic[account]["name"]
        found = self.accounts.get_by_code(account) or self.accounts.get_by_name(account)
        return found.name if found else ""

    def _postable_account_error(self, account: str, label: str) -> str:
        found = self.accounts.get_by_code(account) or self.accounts.get_by_name(account) or self.accounts.find_by_text(label)
        if not found or int(getattr(found, "is_postable", 0) or 0) == 1:
            return ""
        children = [
            f"{child.niif_code or child.code} {child.name}".strip()
            for child in self.accounts.list_children(found.code)
            if int(getattr(child, "is_postable", 0) or 0) == 1
        ]
        suffix = f" Subcuentas disponibles: {', '.join(children)}." if children else " No tiene subcuentas registrables activas."
        return f"{found.niif_code or found.code} {found.name} es un rubro; use una subcuenta registrable.{suffix}"

    def _normalize_account(self, raw_account: Any) -> str:
        normalized = self.tools.normalize_account(str(raw_account or ""))
        if normalized in LEDGER_ACCOUNT_LABELS:
            return normalized
        found = self.accounts.find_by_text(str(raw_account or "")) or self.accounts.find_by_text(normalized)
        return found.code if found else _account_code(normalized)

    def _valid_account_suggestions(self, payload: Mapping[str, Any]) -> list[str]:
        labels = _valid_account_suggestions(payload)
        for account in self.accounts.list_active():
            if int(getattr(account, "is_postable", 0) or 0) != 1:
                continue
            label = account.name.strip()
            if label and label not in labels:
                labels.append(label)
        return sorted(labels)

    def _ensure_dynamic_accounts_in_payload(self, payload: Mapping[str, Any], accounts: list[str]) -> dict[str, Any]:
        projected = deepcopy(dict(payload or {}))
        for account in accounts:
            if account in LEDGER_ACCOUNT_LABELS:
                continue
            if account in _dynamic_account_lookup(projected):
                continue
            found = self.accounts.get_by_code(account) or self.accounts.get_by_name(account) or self.accounts.find_by_text(account)
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

    def _apply_account_creation(self, proposal_payload: Mapping[str, Any], *, command_id: str, cpa_user: str, strict: bool = False) -> None:
        account = dict(proposal_payload.get("account") or {})
        code = str(account.get("code") or "").strip()
        name = str(account.get("name") or "").strip()
        if not code or not name:
            raise AgentValidationError("La propuesta no contiene una cuenta valida para crear.")
        if self.accounts.get_by_code(code) or self.accounts.get_by_name(name) or self.accounts.find_by_text(name):
            if strict:
                raise AgentCatalogChangedError("account exists")
            return
        account = self.accounts.create(
            id=code,
            code=code,
            name=name,
            account_type=str(account.get("account_type") or ""),
            section=str(account.get("section") or ""),
            normal_balance=str(account.get("normal_balance") or "") or None,
            parent_code=str(account.get("parent_code") or "") or None,
            aliases_json=json.dumps(list(account.get("aliases") or []), ensure_ascii=False),
            source=str(account.get("source") or "chat_financiero"),
            is_postable=1 if bool(account.get("is_postable", True)) else 0,
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

    def _catalog_hash(self) -> str:
        rows = []
        for account in self.accounts.list_active():
            rows.append({
                "code": account.code,
                "name": account.name,
                "aliases_json": account.aliases_json,
                "account_type": account.account_type,
                "section": account.section,
                "parent_code": account.parent_code,
                "active": account.active,
            })
        rows.sort(key=lambda row: str(row.get("code") or ""))
        return stable_hash(rows)

    def _compound_catalog_stale_reason(self, proposal_data: Mapping[str, Any]) -> str:
        if proposal_data.get("kind") != "compound_agent_proposal":
            return ""
        expected = str(proposal_data.get("catalog_before_hash") or "")
        if expected and expected != self._catalog_hash():
            return "El catalogo contable cambio desde que se genero la propuesta. Pedi una propuesta nueva."
        for account in proposal_data.get("account_operations") or []:
            if not isinstance(account, Mapping):
                continue
            code = str(account.get("code") or "").strip()
            name = str(account.get("name") or "").strip()
            if self.accounts.get_by_code(code) or self.accounts.get_by_name(name) or self.accounts.find_by_text(name):
                return f"La cuenta {name or code} ya existe en el catalogo. Pedi una propuesta nueva."
        return ""

    def _mark_proposal_stale_after_rollback(self, proposal_id: str) -> None:
        proposal = self.agent_repo.get_proposal(proposal_id)
        if proposal and proposal.status == "pending":
            proposal.status = "stale"
            self.session.commit()

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

    def _timeout_response(self, *, command_id: str) -> dict[str, Any]:
        return {
            "ok": False,
            "command_id": command_id,
            "intent": "timeout",
            "response_type": "error",
            "assistant_message": "La instruccion tomo demasiado tiempo. Intente de nuevo con una consulta mas especifica.",
            "ui_actions": [],
            "requires_confirmation": False,
            "audit": self._audit_metadata(command_id),
        }

    def _compute_impact(
        self,
        payload: Mapping[str, Any],
        projected_payload: Mapping[str, Any],
        intent: str,
        target_month: str | None = None,
    ) -> dict[str, Any]:
        if intent in {"finalizar_periodo", "create_account"}:
            return {"month": None, "items": []}
        try:
            before_result = build_financial_model(payload)
            after_result = build_financial_model(projected_payload)
        except Exception:
            return {"month": None, "items": []}
        months = (
            after_result.summary.get("all_months")
            or after_result.summary.get("months")
            or before_result.summary.get("all_months")
            or before_result.summary.get("months")
            or []
        )
        if not months:
            return {"month": None, "items": []}
        months_str = [str(m) for m in months]
        month = ""
        if target_month:
            candidate = str(target_month).strip()[:7]
            if candidate in months_str:
                month = candidate
        if not month:
            month = months_str[-1]
        rows = [
            ("caja", "Efectivo y equivalentes", "Efectivo y Equivalentes de Efectivo"),
            ("activos", "Total activos", "Total Activos"),
            ("pasivos", "Total pasivos", "Total Pasivos"),
            ("patrimonio", "Total patrimonio", "Total Patrimonio"),
            ("resultado", "Resultado del ejercicio", "Resultados del Ejercicio"),
        ]
        items: list[dict[str, Any]] = []
        for key, label, esf_desc in rows:
            before_val = _statement_value(before_result, esf_desc, month)
            after_val = _statement_value(after_result, esf_desc, month)
            items.append({
                "key": key,
                "label": label,
                "before": round(before_val, 2),
                "after": round(after_val, 2),
                "delta": round(after_val - before_val, 2),
            })
        return {"month": month, "items": items}

    def _compute_assumption_impact(self, payload: Mapping[str, Any], projected_payload: Mapping[str, Any]) -> dict[str, float]:
        try:
            before = build_financial_model(payload)
            after = build_financial_model(projected_payload)
        except Exception:
            return {}
        before_summary = before.summary or {}
        after_summary = after.summary or {}
        months = after_summary.get("all_months") or after_summary.get("months") or before_summary.get("all_months") or before_summary.get("months") or []
        last_month = str(months[-1]) if months else ""
        return {
            "revenue_total_delta": round(float(after_summary.get("income_total") or 0) - float(before_summary.get("income_total") or 0), 2),
            "cost_total_delta": round(_er_value(after, "(-) Costo de ventas") - _er_value(before, "(-) Costo de ventas"), 2),
            "net_income_delta": round(float(after_summary.get("net_income_total") or 0) - float(before_summary.get("net_income_total") or 0), 2),
            "cash_final_delta": round(_statement_value(after, "Efectivo y Equivalentes de Efectivo", last_month) - _statement_value(before, "Efectivo y Equivalentes de Efectivo", last_month), 2),
            "equity_final_delta": round(_statement_value(after, "Total Patrimonio", last_month) - _statement_value(before, "Total Patrimonio", last_month), 2),
        }

    def _audit_metadata(self, command_id: str) -> dict[str, Any]:
        return {
            "command_id": command_id,
            "source": "agent_contable",
            "prompt_version": PROMPT_VERSION,
            "tool_versions": self.tools.versions(),
        }

    def _apply_finalizar_periodo(
        self,
        proposal,
        periodo,
        *,
        proposal_data: dict[str, Any],
        cpa_user: str,
    ) -> dict[str, Any]:
        from services.periodo_service import PeriodoService
        try:
            proposal.status = "applied"
            proposal.applied_at = datetime.now(timezone.utc)
            self.session.flush()
            AuditService(self.session).log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="agent_apply_proposal",
                summary=str(proposal_data.get("title") or "Finalizar periodo"),
                metadata={
                    "command_id": proposal.command_id,
                    "proposal_id": proposal.id,
                    "prompt_version": PROMPT_VERSION,
                    "tool_versions": self.tools.versions(),
                    "proposal_kind": "finalizar_periodo",
                },
            )
            self.session.commit()
            PeriodoService(self.session).finalize(periodo.id, cpa_user=cpa_user)
            return {
                "ok": True,
                "proposal_id": proposal.id,
                "status": "applied",
                "assistant_message": "Listo, el periodo fue finalizado.",
                "periodo_id": periodo.id,
            }
        except Exception:
            self.session.rollback()
            raise

    def _handle_system_intent(
        self,
        *,
        command_id: str,
        intent: str,
        periodo,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        cpa_user: str,
    ) -> dict[str, Any]:
        if intent == "guardar_payload":
            return self._handle_guardar_payload(periodo=periodo, payload=payload, command_id=command_id)
        if intent == "generar_documento":
            return self._handle_generar_documento(periodo=periodo, command_id=command_id, cpa_user=cpa_user)
        raise AgentValidationError(f"Accion de sistema no reconocida: {intent}.")

    def _handle_guardar_payload(self, *, periodo, payload: Mapping[str, Any], command_id: str) -> dict[str, Any]:
        if periodo.estado != "borrador":
            raise AgentValidationError(f"Solo se puede guardar un periodo en estado borrador. Estado actual: '{periodo.estado}'.")
        result = build_financial_model(payload)
        periodo.validation_json = json.dumps(result.validations, ensure_ascii=False, sort_keys=True, default=str)
        periodo.period_blocks_json = json.dumps(result.metadata.get("period_blocks") or [], ensure_ascii=False, sort_keys=True, default=str)
        months = result.summary.get("all_months") or result.summary.get("months") or []
        return {
            "ok": True,
            "command_id": command_id,
            "intent": "guardar_payload",
            "response_type": "answer",
            "assistant_message": f"Borrador guardado y recalculado. {len(months)} mes(es) en el modelo.",
            "ui_actions": [{"type": "scroll_to", "target": "summary"}],
            "requires_confirmation": False,
            "audit": self._audit_metadata(command_id),
        }

    def _handle_generar_documento(self, *, periodo, command_id: str, cpa_user: str) -> dict[str, Any]:
        from services.periodo_service import PeriodoService
        if periodo.estado not in ("finalizado", "certificado"):
            raise AgentValidationError(
                f"El periodo debe estar finalizado antes de generar el documento. Estado actual: '{periodo.estado}'."
            )
        svc = PeriodoService(self.session)
        doc_result = svc.generate_document(periodo.id, cpa_user=cpa_user)
        periodo_data = doc_result.get("periodo") or {}
        raw_path = str(periodo_data.get("documento_path") or "")
        filename = raw_path.replace("\\", "/").split("/")[-1] or "documento.docx"
        return {
            "ok": True,
            "command_id": command_id,
            "intent": "generar_documento",
            "response_type": "answer",
            "assistant_message": f"Documento generado: {filename}. Podés descargarlo desde la seccion de documentos.",
            "ui_actions": [{"type": "scroll_to", "target": "documents"}],
            "requires_confirmation": False,
            "audit": self._audit_metadata(command_id),
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
                "correct_voucher",
                "journal_entry",
                "account_transfer",
                "year_close_transfer",
                "assumption_change",
                "create_account",
                "compound_plan",
                "recalcular_preview",
                "guardar_payload",
                "finalizar_periodo",
                "generar_documento",
                "question",
                "unsupported",
            ],
        },
        "args": {"type": "object"},
        "steps": {"type": "array"},
        "assistant_message": {"type": "string"},
    },
    "required": ["intent", "args"],
}


MUTATION_INTENTS = {"reverse_voucher", "correct_voucher", "journal_entry", "account_transfer", "year_close_transfer", "assumption_change", "create_account", "compound_plan", "finalizar_periodo"}

SYSTEM_INTENTS = {"guardar_payload", "generar_documento"}


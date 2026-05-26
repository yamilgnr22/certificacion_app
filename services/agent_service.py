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
from services.agent_tools import AgentToolRegistry
from services.periodo_service import PeriodoService
from services.rollforward_service import RollforwardService
from services.serializers import parse_json_object


PROMPT_VERSION = "agent-command-v1.0.0"
MAX_TOOL_CALLS_PER_TURN = 1
MAX_TURN_DURATION_S = 30.0


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
        if proposal_data.get("kind") == "finalizar_periodo":
            return self._apply_finalizar_periodo(proposal, periodo, proposal_data=proposal_data, cpa_user=cpa_user)

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
            after = _periodo_snapshot(periodo)
            AuditService(self.session).log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="agent_apply_proposal",
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
        return projected, _proposal_payload(
            kind="compound_voucher_correction",
            title=f"Corregir comprobante {voucher_id}",
            assistant_message=f"Prepare la correccion de {voucher_id}: reverso del comprobante original y nuevo asiento corregido.",
            month=target_month,
            rows=[],
            technical_records=[reversal, correction_entry],
            original_message=original_message,
            extra={
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

    def _normalize_account(self, raw_account: Any) -> str:
        normalized = self.tools.normalize_account(str(raw_account or ""))
        if normalized in LEDGER_ACCOUNT_LABELS:
            return normalized
        found = self.accounts.find_by_text(str(raw_account or "")) or self.accounts.find_by_text(normalized)
        return found.code if found else normalized

    def _valid_account_suggestions(self, payload: Mapping[str, Any]) -> list[str]:
        labels = _valid_account_suggestions(payload)
        for account in self.accounts.list_active():
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

    def _apply_account_creation(self, proposal_payload: Mapping[str, Any], *, command_id: str, cpa_user: str) -> None:
        account = dict(proposal_payload.get("account") or {})
        code = str(account.get("code") or "").strip()
        name = str(account.get("name") or "").strip()
        if not code or not name:
            raise AgentValidationError("La propuesta no contiene una cuenta valida para crear.")
        if self.accounts.get_by_code(code) or self.accounts.get_by_name(name) or self.accounts.find_by_text(name):
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
                "recalcular_preview",
                "guardar_payload",
                "finalizar_periodo",
                "generar_documento",
                "question",
                "unsupported",
            ],
        },
        "args": {"type": "object"},
        "assistant_message": {"type": "string"},
    },
    "required": ["intent", "args"],
}


MUTATION_INTENTS = {"reverse_voucher", "correct_voucher", "journal_entry", "account_transfer", "year_close_transfer", "assumption_change", "create_account", "finalizar_periodo"}

SYSTEM_INTENTS = {"guardar_payload", "generar_documento"}


def _system_prompt() -> str:
    return (
        "Sos un asistente contable dentro de una app de certificaciones. "
        "Interpretas instrucciones contables y devuelves una accion estructurada; no calcules saldos finales. "
        "Respondé exclusivamente JSON valido con intent y args. "
        "Intents permitidos: explain_balance(account, month), show_ledger(account), "
        "show_voucher(voucher_id), navigate(target), reverse_voucher(voucher_id), "
        "correct_voucher(voucher_id, correction={month,description,lines}), "
        "journal_entry(month, description, lines=[{account,debit,credit,reference}]), "
        "account_transfer(month, source_account, destination_account, amount), "
        "year_close_transfer(target_month, source_month, amount opcional), "
        "assumption_change(field, new_value, scope=period), "
        "create_account(name, account_type, section), "
        "recalcular_preview(), guardar_payload(), finalizar_periodo(), generar_documento(), "
        "question. "
        "Si falta cuenta, mes o monto, usa question. Si el usuario pide crear una cuenta nueva, usa create_account. "
        "Si pide guardar o recalcular, usa guardar_payload o recalcular_preview. "
        "Si pide finalizar el periodo, usa finalizar_periodo. Si pide generar el documento, usa generar_documento."
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


def _saved_vouchers(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    accounting = dict((payload or {}).get("accounting") or {})
    return [dict(voucher) for voucher in accounting.get("vouchers") or [] if isinstance(voucher, Mapping)]


def _has_correction_payload(args: Mapping[str, Any]) -> bool:
    correction = args.get("correction") if isinstance(args.get("correction"), Mapping) else args
    lines = correction.get("lines") if isinstance(correction, Mapping) else None
    return bool(isinstance(lines, list) and lines)


def _resolve_correctable_voucher(
    payload: Mapping[str, Any],
    all_vouchers: list[Mapping[str, Any]],
    persisted_vouchers: list[Mapping[str, Any]],
    voucher_id: str,
) -> tuple[dict[str, Any], str]:
    target = str(voucher_id or "").upper()
    persisted = next((dict(voucher) for voucher in persisted_vouchers if str(voucher.get("voucher_id") or "").upper() == target), None)
    if persisted:
        return persisted, "persisted"
    generated = next((dict(voucher) for voucher in all_vouchers if str(voucher.get("voucher_id") or "").upper() == target), None)
    if not generated:
        raise AgentValidationError(f"No encontre el comprobante {voucher_id}.")
    instruction_id = str(generated.get("instruction_id") or "").strip()
    entries = dict((payload or {}).get("movements") or {}).get("journal_entries") or []
    if instruction_id and any(str(entry.get("instruction_id") or "").strip() == instruction_id for entry in entries if isinstance(entry, Mapping)):
        return generated, "journal_entry"
    raise AgentValidationError("Ese comprobante es generado automaticamente; en esta fase solo puedo corregir comprobantes guardados o partidas del chat.")


def _find_reversal_for(vouchers: list[Mapping[str, Any]], original_voucher_id: str) -> str:
    original = str(original_voucher_id or "").upper()
    for voucher in vouchers:
        if str(voucher.get("type") or "").lower() != "reversal":
            continue
        if str(voucher.get("reference_voucher_id") or "").upper() == original:
            return str(voucher.get("voucher_id") or "")
    return ""


def _unique_reversal_id(original_voucher_id: str, vouchers: list[Mapping[str, Any]]) -> str:
    existing = {str(voucher.get("voucher_id") or "").upper() for voucher in vouchers}
    original = str(original_voucher_id or "").upper()
    for _ in range(20):
        candidate = f"REV-{original}-{uuid.uuid4().hex[:6].upper()}"
        if candidate.upper() not in existing:
            return candidate
    return f"REV-{original}-{uuid.uuid4().hex[:12].upper()}"


def _journal_entry_from_reversal_voucher(voucher: Mapping[str, Any], *, command_id: str, original_message: str) -> dict[str, Any]:
    return {
        "entry_id": f"JE-{uuid.uuid4().hex[:8].upper()}",
        "voucher_id": str(voucher.get("voucher_id") or ""),
        "reference_voucher_id": str(voucher.get("reference_voucher_id") or ""),
        "month": str(voucher.get("month") or "")[:7],
        "description": str(voucher.get("description") or ""),
        "lines": [
            {
                "account": line.get("account"),
                "debit": line.get("debit") or 0,
                "credit": line.get("credit") or 0,
                "reference": line.get("reference") or "",
            }
            for line in voucher.get("lines") or []
            if isinstance(line, Mapping)
        ],
        "currency": "nio",
        "entry_type": "voucher_reversal",
        "source": "chat_financiero",
        "instruction_id": command_id,
        "locked": True,
        "message": original_message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


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
        "niif_code": str(account.get("niif_code") or "").strip(),
        "name": str(account.get("name") or "").strip(),
        "account_type": _normalize_account_type(account.get("account_type")),
        "section": _normalize_account_section(account.get("section")),
        "normal_balance": str(account.get("normal_balance") or "").strip(),
        "parent_code": str(account.get("parent_code") or "").strip(),
        "aliases": list(account.get("aliases") or []),
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
    try:
        aliases = json.loads(account.aliases_json or "[]")
    except Exception:
        aliases = []
    return {
        "code": account.code,
        "niif_code": account.niif_code,
        "name": account.name,
        "account_type": account.account_type,
        "section": account.section,
        "normal_balance": account.normal_balance,
        "parent_code": account.parent_code,
        "aliases": aliases if isinstance(aliases, list) else [],
        "source": account.source,
    }


def _voucher_rows(voucher: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "account": line.get("account"),
            "debit": line.get("debit") or 0,
            "credit": line.get("credit") or 0,
            "reference": line.get("reference") or line.get("ref") or "",
        }
        for line in voucher.get("lines") or []
    ]


def _journal_totals(lines: list[Mapping[str, Any]]) -> dict[str, Any]:
    debit = round(sum(float(line.get("debit") or 0) for line in lines), 2)
    credit = round(sum(float(line.get("credit") or 0) for line in lines), 2)
    return {"debit": debit, "credit": credit, "balanced": abs(debit - credit) <= 0.01}


def _journal_pair(lines: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    debit_lines = [line for line in lines if float(line.get("debit") or 0) > 0]
    credit_lines = [line for line in lines if float(line.get("credit") or 0) > 0]
    if len(debit_lines) != 1 or len(credit_lines) != 1:
        return None
    debit_amount = round(float(debit_lines[0].get("debit") or 0), 2)
    credit_amount = round(float(credit_lines[0].get("credit") or 0), 2)
    if abs(debit_amount - credit_amount) > 0.01:
        return None
    return {
        "debit_account": debit_lines[0].get("account"),
        "credit_account": credit_lines[0].get("account"),
        "amount": debit_amount,
    }


def _valid_account_suggestions(payload: Mapping[str, Any]) -> list[str]:
    labels = list(LEDGER_ACCOUNT_LABELS.values())
    dynamic = _dynamic_account_lookup(payload)
    for account in dynamic.values():
        name = str(account.get("name") or "").strip()
        if name and name not in labels:
            labels.append(name)
    return sorted(labels)


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


def _payload_months(payload: Mapping[str, Any]) -> list[str]:
    period = dict(payload.get("period") or {})
    start = str(period.get("start_month") or "")[:7]
    end = str(period.get("end_month") or "")[:7]
    if start and end:
        return _months_between(start, end)
    try:
        result = build_financial_model(payload)
        return [str(m) for m in (result.summary.get("all_months") or result.summary.get("months") or [])]
    except Exception:
        return []


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


def _er_value(result, description: str) -> float:
    df = result.df_er_full
    if df is None or df.empty:
        return 0.0
    rows = df[df["Descripcion"] == description]
    if rows.empty:
        return 0.0
    accum_cols = [col for col in df.columns if str(col).startswith("Acumulado")]
    col = accum_cols[0] if accum_cols else df.columns[-2]
    try:
        return float(rows.iloc[0][col] or 0)
    except Exception:
        return 0.0


def _impact_deltas(impact: Mapping[str, Any]) -> dict[str, float]:
    out = {"cash_delta": 0.0, "assets_delta": 0.0, "liabilities_delta": 0.0, "equity_delta": 0.0, "income_delta": 0.0}
    mapping = {
        "caja": "cash_delta",
        "activos": "assets_delta",
        "pasivos": "liabilities_delta",
        "patrimonio": "equity_delta",
        "resultado": "income_delta",
    }
    for item in impact.get("items") or []:
        key = mapping.get(str(item.get("key") or ""))
        if key:
            out[key] = round(float(item.get("delta") or 0), 2)
    return out


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


ASSUMPTION_FIELD_MAP = {
    "cost_pct": ("income", "cost_pct", "pct"),
    "porcentaje_costo": ("income", "cost_pct", "pct"),
    "ingresos_base_usd": ("income", "base_income_usd", "positive"),
    "base_income_usd": ("income", "base_income_usd", "positive"),
    "income_base_usd": ("income", "base_income_usd", "positive"),
    "variabilidad_ingresos_pct": ("income", "income_variability_pct", "pct"),
    "income_variability_pct": ("income", "income_variability_pct", "pct"),
    "variabilidad_costos_pct": ("income", "cost_variability_pct", "pct"),
    "cost_variability_pct": ("income", "cost_variability_pct", "pct"),
    "cash_sales_pct": ("income", "cash_sales_pct", "pct"),
    "porcentaje_contado": ("income", "cash_sales_pct", "pct"),
    "seed": ("period", "seed", "string"),
}


def _normalize_assumption_field(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = raw.replace(" ", "_").replace("-", "_")
    aliases = {
        "costo": "cost_pct",
        "costo_venta": "cost_pct",
        "costo_de_venta": "cost_pct",
        "ventas_contado": "cash_sales_pct",
        "contado": "cash_sales_pct",
        "ingresos": "ingresos_base_usd",
        "ingreso_base": "ingresos_base_usd",
        "variabilidad_ingresos": "variabilidad_ingresos_pct",
        "variabilidad_costos": "variabilidad_costos_pct",
        "semilla": "seed",
    }
    raw = aliases.get(raw, raw)
    if raw not in ASSUMPTION_FIELD_MAP:
        allowed = ", ".join(sorted({"cost_pct", "ingresos_base_usd", "variabilidad_ingresos_pct", "variabilidad_costos_pct", "cash_sales_pct", "seed"}))
        raise AgentValidationError(f"Supuesto no permitido. Campos permitidos: {allowed}.")
    # Use public Spanish key for ingreso base so proposal text matches app language.
    if raw == "base_income_usd":
        return "ingresos_base_usd"
    if raw == "income_variability_pct":
        return "variabilidad_ingresos_pct"
    if raw == "cost_variability_pct":
        return "variabilidad_costos_pct"
    return raw


def _normalize_assumption_value(kind: str, value: Any) -> Any:
    if kind == "string":
        out = str(value or "").strip()
        if not out:
            raise AgentValidationError("El valor de seed no puede estar vacio.")
        return out
    number = _to_float(value)
    if number is None:
        raise AgentValidationError("Indique un valor valido para el supuesto.")
    if kind == "positive":
        if number <= 0:
            raise AgentValidationError("El ingreso base USD debe ser mayor que cero.")
        return float(number)
    if kind == "pct":
        if 0 <= number <= 1:
            number *= 100.0
        if number < 0 or number > 100:
            raise AgentValidationError("El porcentaje debe estar entre 0 y 100%.")
        return float(number)
    return number


def _apply_assumption_change(payload: Mapping[str, Any], *, field: str, value: Any) -> tuple[dict[str, Any], Any, Any]:
    target, key, kind = ASSUMPTION_FIELD_MAP[field]
    after_value = _normalize_assumption_value(kind, value)
    projected = deepcopy(dict(payload or {}))
    section = dict(projected.get(target) or {})
    before_value = section.get(key)
    section[key] = after_value
    projected[target] = section
    return projected, before_value, after_value


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


def _elapsed_seconds(started_at: float) -> float:
    return max(0.0, time.monotonic() - started_at)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _extract_target_month(proposal_payload: Mapping[str, Any], args: Mapping[str, Any]) -> str | None:
    import re
    pattern = re.compile(r"^\d{4}-\d{2}$")
    month_field = str(proposal_payload.get("month") or "").strip()[:7]
    if pattern.match(month_field):
        return month_field
    for key in ("target_month", "month", "source_month"):
        val = str((args or {}).get(key) or "").strip()[:7]
        if pattern.match(val):
            return val
    records = proposal_payload.get("technical_records") or []
    if records and isinstance(records[0], Mapping):
        record_month = str(records[0].get("month") or "").strip()[:7]
        if pattern.match(record_month):
            return record_month
    return None

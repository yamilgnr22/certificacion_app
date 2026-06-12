from __future__ import annotations

import logging
import uuid
import json
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from accounting_accounts import LEDGER_ACCOUNT_LABELS
from accounting_model import reverse_voucher
from llm import LLMProvider, LLMProviderError, get_llm_provider
from model_cache import cached_build_financial_model as build_financial_model
from repositories import AccountRepository, AgentRepository, PeriodoRepository
from services.audit_service import AuditService, stable_hash
from services.agent_errors import (
    AgentCatalogChangedError,
    AgentConfigError,
    AgentNotFoundError,
    AgentPlanFailureError,
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
    _normalize_month_value,
    _payload_months,
    _period_exchange_rate,
    _periodo_snapshot,
    _previous_year_end,
    _proposal_payload,
    _resolve_correctable_voucher,
    _saved_vouchers,
    _statement_value,
    _sync_period_fields,
    _system_prompt,
    _target_amount_value,
    _to_float,
    _unique_reversal_id,
    _user_prompt,
    _valid_account_suggestions,
    _valid_type_section,
    _voucher_rows,
)
from services.agent_constants import (
    MAX_PLAN_ACCOUNTS,
    TARGET_BALANCE_ACCOUNTS,
    TARGET_COUNTER_DEFAULTS,
)
from services.agent_plan_builders import AgentPlanBuilderMixin
from services.agent_proposal_builders import AgentProposalBuilderMixin
from services.agent_tools import AgentToolRegistry
from services.agent_planner import AgentPlanError, AgentPlanner
from services.periodo_service import PeriodoService
from services.rollforward_service import RollforwardService
from services.serializers import agent_plan_to_dict, parse_json_object


logger = logging.getLogger(__name__)

PROMPT_VERSION = "agent-command-v1.0.0"
MAX_TOOL_CALLS_PER_TURN = 3
MAX_TURN_DURATION_S = 30.0
SESSION_CONTEXT_TTL_MINUTES = 30
PLAN_TTL_MINUTES = 10
class AgentCommandService(AgentPlanBuilderMixin, AgentProposalBuilderMixin):
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

        retrying_pending_goal = False
        context = self.agent_repo.get_session_context(
            periodo_id=periodo.id,
            cpa_user=cpa_user,
            ttl_minutes=SESSION_CONTEXT_TTL_MINUTES,
        )
        if _is_save_confirmation(message) and context and context.pending_goal_message:
            message = str(context.pending_goal_message or "").strip()
            retrying_pending_goal = True
            is_dirty = False
            current_payload = None

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
            llm_message = str(interpreted.get("assistant_message") or interpreted.get("question") or "").strip()
            if not llm_message:
                llm_message = (
                    "No pude interpretar la instruccion sin perder informacion. "
                    "Probá con un solo objetivo por mensaje, por ejemplo: "
                    "'ajustá inventario a USD 205k en mayo 2026' o 'mostrame el saldo de caja en abril 2026'."
                )
            response = self._question_response(
                command_id=command_id,
                intent=intent or "question",
                message=llm_message,
            )
        elif intent in MUTATION_INTENTS:
            if used_dirty_payload:
                message_text = "Guarda los cambios antes de preparar una correccion contable." if intent == "correct_voucher" else "Guarda los cambios antes de preparar una propuesta contable."
                if intent == "target_balance_adjustment":
                    message_text = "Guarda los cambios antes de preparar un ajuste por objetivo."
                self._set_pending_goal(periodo.id, cpa_user, message=message, intent=intent)
                response = self._question_response(
                    command_id=command_id,
                    intent=intent,
                    message=message_text,
                )
                response["ui_actions"] = [{
                    "type": "save_and_retry",
                    "retry_message": message,
                    "retry_intent_hint": intent,
                }]
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
                        cpa_user=cpa_user,
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
                    cpa_user=cpa_user,
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
        if retrying_pending_goal:
            self._clear_pending_goal(periodo.id, cpa_user)
        self._update_session_context_from_response(
            periodo_id=periodo.id,
            cpa_user=cpa_user,
            intent=str(response.get("intent") or intent),
            response=response,
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
            if proposal_data.get("kind") == "target_balance_adjustment_proposal":
                self._verify_target_balance_after_apply(result, proposal_data)
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
            if proposal_data.get("kind") == "target_balance_adjustment_proposal":
                target = proposal_data.get("target") if isinstance(proposal_data.get("target"), Mapping) else {}
                metadata.update({
                    "kind": "target_balance_adjustment",
                    "target_account": target.get("account"),
                    "target_month": target.get("month"),
                    "target_amount_original": target.get("target_amount_original"),
                    "target_currency": target.get("target_currency"),
                    "target_amount_nio": target.get("target_amount_nio"),
                    "current_balance_nio_before": target.get("current_balance_nio_before"),
                    "delta_applied_nio": target.get("delta_applied_nio"),
                    "counter_account": proposal_data.get("counter_account"),
                    "journal_entry_id": proposal_data.get("journal_entry_id"),
                    "user_message": proposal_data.get("original_message"),
                    "exchange_rate_used": target.get("exchange_rate_used"),
                })
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

    def get_plan(self, plan_id: str, *, cpa_user: str = "system") -> dict[str, Any]:
        plan_id = str(plan_id or "").strip()
        if not plan_id:
            raise AgentValidationError("Falta plan_id.")
        plan = self.agent_repo.get_plan(plan_id)
        if not plan:
            raise AgentNotFoundError("Plan no encontrado.")
        return {"ok": True, "plan": agent_plan_to_dict(plan)}

    def discard_plan(self, plan_id: str, *, cpa_user: str = "system") -> dict[str, Any]:
        plan_id = str(plan_id or "").strip()
        plan = self.agent_repo.get_plan(plan_id)
        if not plan:
            raise AgentNotFoundError("Plan no encontrado.")
        if plan.status != "pending":
            raise AgentProposalConflictError(f"El plan ya no esta pendiente. Estado actual: {plan.status}.")
        plan.status = "discarded"
        AuditService(self.session).log(
            cpa_user=cpa_user,
            entity_type="periodo",
            entity_id=plan.periodo_id,
            action="agent_discard_plan",
            summary="Descarto plan del asistente contable",
            metadata={"plan_id": plan.id, "plan_kind": plan.kind},
        )
        self.session.commit()
        return {"ok": True, "plan_id": plan.id, "status": plan.status}

    def apply_plan(self, plan_id: str, *, cpa_user: str = "system") -> dict[str, Any]:
        started_at = time.monotonic()
        plan_id = str(plan_id or "").strip()
        if not plan_id:
            raise AgentValidationError("Falta plan_id.")
        plan = self.agent_repo.get_plan(plan_id)
        if not plan:
            raise AgentNotFoundError("Plan no encontrado.")
        if plan.status != "pending":
            raise AgentProposalConflictError(f"El plan ya no esta pendiente. Estado actual: {plan.status}.")

        now = datetime.now(timezone.utc)
        if _as_aware(plan.expires_at) < now:
            plan.status = "expired"
            self.session.commit()
            raise AgentProposalConflictError("El plan expiro, pedi de nuevo la instruccion.")

        periodo = self.periodos.get(plan.periodo_id)
        if not periodo:
            raise AgentNotFoundError("Periodo del plan no encontrado.")
        if periodo.estado != "borrador":
            raise AgentProposalConflictError("Solo se pueden aplicar planes en periodos borrador.")

        persisted_payload = parse_json_object(periodo.payload_json)
        if stable_hash(persisted_payload) != plan.payload_hash:
            plan.status = "stale"
            self.session.commit()
            raise AgentProposalConflictError("El modelo cambio desde que se genero el plan. Pedi un plan nuevo.")

        steps = self._plan_steps(plan)
        if not steps:
            raise AgentValidationError("El plan no tiene pasos aplicables.")

        before = _periodo_snapshot(periodo)
        plan.status = "applying"
        self.session.commit()

        working_payload = deepcopy(persisted_payload)
        current_step_order = 0
        try:
            for step in sorted(steps, key=lambda s: int(s.get("step_order") or 0)):
                current_step_order = int(step.get("step_order") or current_step_order + 1)
                working_payload = self._apply_plan_step_to_payload(
                    step,
                    working_payload,
                    plan_id=plan.id,
                    user_message=plan.user_message,
                )
                result = build_financial_model(working_payload)
                self._verify_plan_step_against_result(step, result)

            result = build_financial_model(working_payload)
            periodo = self.periodos.get(plan.periodo_id)
            if not periodo:
                raise AgentNotFoundError("Periodo del plan no encontrado.")
            periodo.payload_json = json.dumps(working_payload, ensure_ascii=False, sort_keys=True, default=str)
            periodo.validation_json = json.dumps(result.validations, ensure_ascii=False, sort_keys=True, default=str)
            periodo.period_blocks_json = json.dumps(result.metadata.get("period_blocks") or [], ensure_ascii=False, sort_keys=True, default=str)
            _sync_period_fields(periodo, working_payload)
            RollforwardService(self.session).cache_saldos_finales(periodo)

            plan = self.agent_repo.get_plan(plan.id)
            plan.status = "applied"
            plan.applied_at = datetime.now(timezone.utc)
            plan.failed_step_order = None
            plan.failure_reason = None
            duration_ms = round(_elapsed_seconds(started_at) * 1000, 2)
            aggregate_impact = parse_json_object(plan.aggregate_impact_json)
            AuditService(self.session).log(
                cpa_user=cpa_user,
                entity_type="periodo",
                entity_id=periodo.id,
                action="agent_apply_plan",
                summary=str(plan.plan_summary or "Aplico plan del asistente contable"),
                before=before,
                after=_periodo_snapshot(periodo),
                metadata={
                    "kind": "plan_applied",
                    "plan_id": plan.id,
                    "plan_kind": plan.kind,
                    "user_message": plan.user_message,
                    "step_count": len(steps),
                    "aggregate_impact": aggregate_impact,
                    "duration_ms": duration_ms,
                    "exchange_rate_used": _period_exchange_rate(working_payload),
                    "prompt_version": PROMPT_VERSION,
                    "tool_versions_used": self.tools.versions_used([plan.kind, "compute_target_distribution"]),
                },
            )
            self.session.commit()
            return {
                "ok": True,
                "plan_id": plan.id,
                "status": "applied",
                "assistant_message": "Listo, aplique el plan completo al periodo.",
                "periodo_id": periodo.id,
            }
        except Exception as exc:
            self.session.rollback()
            failure_reason = str(exc)[:500]
            failed = self.agent_repo.get_plan(plan_id)
            if failed:
                failed.status = "failed"
                failed.failed_step_order = current_step_order or None
                failed.failure_reason = failure_reason
                AuditService(self.session).log(
                    cpa_user=cpa_user,
                    entity_type="periodo",
                    entity_id=failed.periodo_id,
                    action="agent_plan_failed",
                    summary="Fallo plan del asistente contable; el periodo quedo sin cambios",
                    metadata={
                        "kind": "plan_failed",
                        "plan_id": failed.id,
                        "plan_kind": failed.kind,
                        "user_message": failed.user_message,
                        "step_count_attempted": current_step_order or 0,
                        "failed_step_order": current_step_order or None,
                        "failure_reason": failure_reason,
                        "duration_ms": round(_elapsed_seconds(started_at) * 1000, 2),
                    },
                )
                self.session.commit()
            raise AgentPlanFailureError(f"El plan fallo en el paso {current_step_order or '?'}: {failure_reason}") from exc

    @staticmethod
    def _provider_from_config() -> LLMProvider:
        return get_llm_provider()

    def _set_pending_goal(self, periodo_id: str, cpa_user: str, *, message: str, intent: str) -> None:
        self.agent_repo.upsert_session_context(
            periodo_id=periodo_id,
            cpa_user=cpa_user,
            pending_goal_message=str(message or ""),
            pending_goal_kind=str(intent or ""),
        )

    def _clear_pending_goal(self, periodo_id: str, cpa_user: str) -> None:
        self.agent_repo.upsert_session_context(
            periodo_id=periodo_id,
            cpa_user=cpa_user,
            pending_goal_message=None,
            pending_goal_kind=None,
        )

    def _update_session_context_from_response(
        self,
        *,
        periodo_id: str,
        cpa_user: str,
        intent: str,
        response: Mapping[str, Any],
    ) -> None:
        changes: dict[str, Any] = {}
        data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
        proposal = response.get("proposal") if isinstance(response.get("proposal"), Mapping) else {}
        if data:
            if data.get("account"):
                changes["last_account"] = str(data.get("account") or "")
            if data.get("month"):
                changes["last_month"] = str(data.get("month") or "")[:7]
            changes["last_query_kind"] = str(data.get("kind") or intent or "")
            changes["last_query_payload"] = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        if proposal:
            if proposal.get("id"):
                changes["last_proposal_id"] = str(proposal.get("id") or "")
            target = proposal.get("target") if isinstance(proposal.get("target"), Mapping) else {}
            if target.get("account"):
                changes["last_account"] = str(target.get("account") or "")
            if target.get("month"):
                changes["last_month"] = str(target.get("month") or "")[:7]
        if changes:
            self.agent_repo.upsert_session_context(periodo_id=periodo_id, cpa_user=cpa_user, **changes)

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
        cpa_user: str = "system",
    ) -> dict[str, Any]:
        if intent in PLAN_INTENTS:
            return self._plan_response(
                command_id=command_id,
                intent=intent,
                periodo=periodo,
                payload=payload,
                args=args,
                original_message=original_message,
                cpa_user=cpa_user,
            )
        if intent in {"reverse_voucher", "correct_voucher"} and getattr(periodo, "estado", "") != "borrador":
            raise AgentValidationError("Solo puedo preparar reversos y correcciones en periodos borrador.")
        if intent == "target_balance_adjustment" and getattr(periodo, "estado", "") != "borrador":
            raise AgentValidationError("No puedo modificar este periodo porque ya no esta en borrador.")
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

    def _plan_response(
        self,
        *,
        command_id: str,
        intent: str,
        periodo,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        original_message: str,
        cpa_user: str,
    ) -> dict[str, Any]:
        if getattr(periodo, "estado", "") != "borrador":
            raise AgentValidationError("Solo puedo crear planes en periodos borrador.")
        plan_payload = self._build_agent_plan(
            intent=intent,
            payload=payload,
            args=args,
            command_id=command_id,
            original_message=original_message,
        )
        if plan_payload.get("no_plan"):
            return self._tool_response(
                command_id=command_id,
                intent=intent,
                tool_result={
                    "response_type": "answer",
                    "assistant_message": str(plan_payload.get("assistant_message") or "Ya cumple; no hace falta plan."),
                    "data": {"kind": "plan_not_needed"},
                    "ui_actions": [],
                },
            )

        payload_hash = stable_hash(payload)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=PLAN_TTL_MINUTES)
        steps = list(plan_payload.get("steps") or [])
        aggregate_impact = dict(plan_payload.get("aggregate_impact") or {})
        try:
            self.agent_repo.discard_pending_plans_for_periodo(periodo_id=periodo.id, cpa_user=cpa_user)
            record = self.agent_repo.add_plan(
                periodo_id=periodo.id,
                cpa_user=cpa_user,
                kind=str(plan_payload.get("kind") or intent),
                user_message=original_message,
                plan_summary=str(plan_payload.get("plan_summary") or "Plan del asistente contable"),
                steps_json=json.dumps(steps, ensure_ascii=False, sort_keys=True, default=str),
                aggregate_impact_json=json.dumps(aggregate_impact, ensure_ascii=False, sort_keys=True, default=str),
                payload_hash=payload_hash,
                expires_at=expires_at,
            )
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise AgentProposalConflictError("Ya existe un plan pendiente para este periodo. Refresca y vuelve a intentar.") from exc

        plan = agent_plan_to_dict(record)
        return {
            "ok": True,
            "command_id": command_id,
            "intent": intent,
            "response_type": "plan",
            "assistant_message": plan_payload.get("assistant_message") or plan["plan_summary"],
            "plan": plan,
            "requires_confirmation": True,
            "ui_actions": [{"type": "show_plan", "plan_id": record.id}],
            "audit": self._audit_metadata(command_id),
        }

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
            logger.warning("No se pudo calcular el impacto de la propuesta (intent=%s); se reporta vacio", intent, exc_info=True)
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

    def _normalize_currency(self, value: Any) -> str:
        currency = str(value or "NIO").strip().upper()
        if currency in {"DOLAR", "DOLARES", "USD$"}:
            return "USD"
        if currency in {"CORDOBA", "CORDOBAS", "C$"}:
            return "NIO"
        return currency if currency in {"USD", "NIO"} else "NIO"

    def _months_scope(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> list[str]:
        raw = args.get("months") or args.get("months_scope") or args.get("meses")
        if isinstance(raw, list) and raw:
            months = [_normalize_month_value(item) or str(item or "")[:7] for item in raw]
        else:
            months = _payload_months(payload)
        valid = set(_payload_months(payload))
        out = [m for m in months if m in valid]
        if not out:
            raise AgentValidationError("No encontre meses validos dentro del periodo para el plan.")
        return out

    def _er_month_value(self, result, description: str, month: str) -> float:
        df = getattr(result, "df_er_full", None)
        if df is None or df.empty:
            return 0.0
        col = month if month in df.columns else next((c for c in df.columns if str(c).startswith(month)), None)
        if col is None:
            return 0.0
        rows = df[df["Descripcion"] == description]
        if rows.empty:
            return 0.0
        try:
            return float(rows.iloc[0][col] or 0)
        except Exception:
            return 0.0

    def _compute_assumption_impact(self, payload: Mapping[str, Any], projected_payload: Mapping[str, Any]) -> dict[str, float]:
        try:
            before = build_financial_model(payload)
            after = build_financial_model(projected_payload)
        except Exception:
            logger.warning("No se pudo calcular el impacto del cambio de supuesto; se reporta vacio", exc_info=True)
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


def _is_save_confirmation(message: str) -> bool:
    text = str(message or "").strip().lower()
    replacements = str.maketrans("áéíóúü", "aeiouu")
    text = text.translate(replacements)
    return text in {"ya guarde", "ya guarde.", "ya lo guarde", "ya lo hice", "listo", "guardado", "ya esta guardado"}


AGENT_COMMAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "explain_balance",
                "show_ledger",
                "show_voucher",
                "get_account_balance",
                "get_ledger",
                "get_period_summary",
                "convert_currency",
                "compute_target_delta",
                "navigate",
                "reverse_voucher",
                "correct_voucher",
                "journal_entry",
                "account_transfer",
                "year_close_transfer",
                "assumption_change",
                "monthly_override",
                "create_account",
                "compound_plan",
                "target_balance_adjustment",
                "plan_multi_target_balance",
                "plan_non_negative_account",
                "plan_target_utility",
                "plan_multi_account_target_balance",
                "plan_compound_constraints",
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


PLAN_INTENTS = {"plan_multi_target_balance", "plan_non_negative_account", "plan_target_utility", "plan_multi_account_target_balance", "plan_compound_constraints"}

MUTATION_INTENTS = {"reverse_voucher", "correct_voucher", "journal_entry", "account_transfer", "year_close_transfer", "assumption_change", "monthly_override", "create_account", "compound_plan", "target_balance_adjustment", "finalizar_periodo", *PLAN_INTENTS}

SYSTEM_INTENTS = {"guardar_payload", "generar_documento"}


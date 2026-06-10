"""Adaptador entre los intents de plan del agente y el solver (F2-T1).

Traduce los args que devuelve el LLM a llamadas del ConstraintSolver
(services/solver/constraint_solver.py), donde vive la logica de
simulacion, aplicacion y verificacion de steps. El mixin asume que el
host implementa la interfaz SolverHost (`_normalize_account`,
`_normalize_currency`, `_months_scope`, `_er_month_value`,
`_prepare_target_balance_adjustment`, `_prepare_monthly_override`).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from services.agent_constants import MAX_PLAN_ACCOUNTS, TARGET_BALANCE_ACCOUNTS
from services.agent_errors import AgentValidationError
from services.agent_helpers import (
    _normalize_month_value,
    _payload_months,
    _target_amount_value,
    _to_float,
)
from services.solver import ConstraintSolver, distribute_average


class AgentPlanBuilderMixin:
    """Construye planes multi-paso delegando la simulacion en el solver."""

    @property
    def _solver(self) -> ConstraintSolver:
        return ConstraintSolver(host=self)

    def _build_agent_plan(
        self,
        *,
        intent: str,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> dict[str, Any]:
        if intent == "plan_multi_target_balance":
            return self._build_multi_target_balance_plan(payload, args, command_id, original_message)
        if intent == "plan_non_negative_account":
            return self._build_non_negative_account_plan(payload, args, command_id, original_message)
        if intent == "plan_target_utility":
            return self._build_target_utility_plan(payload, args, command_id, original_message)
        if intent == "plan_multi_account_target_balance":
            return self._build_multi_account_target_balance_plan(payload, args, command_id, original_message)
        raise AgentValidationError("Ese tipo de plan no esta habilitado.")

    def _build_multi_target_balance_plan(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> dict[str, Any]:
        account = self._normalize_account(args.get("account") or args.get("target_account") or args.get("cuenta"))
        if account not in TARGET_BALANCE_ACCOUNTS:
            raise AgentValidationError("Solo puedo planificar objetivos para Inventario, Caja, Cuentas por Cobrar y Proveedores.")
        currency = self._normalize_currency(args.get("currency") or args.get("moneda") or "USD")
        targets = self._plan_targets_from_args(payload, args)
        return self._solver.simulate_target_plan(
            kind="multi_target_balance",
            payload=payload,
            targets=[{**target, "account": account, "currency": currency, "counter_account": args.get("counter_account") or args.get("contrapartida")} for target in targets],
            command_id=command_id,
            original_message=original_message,
            summary_prefix=f"Ajuste multi-mes de {TARGET_BALANCE_ACCOUNTS[account]['label']}",
        )

    def _build_multi_account_target_balance_plan(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> dict[str, Any]:
        targets = args.get("targets") if isinstance(args.get("targets"), list) else []
        if not targets:
            raise AgentValidationError("Indique los objetivos por cuenta para armar el plan multi-cuenta.")
        accounts = {
            self._normalize_account(target.get("account") if isinstance(target, Mapping) else "")
            for target in targets
        }
        accounts.discard("")
        if len(accounts) > MAX_PLAN_ACCOUNTS:
            raise AgentValidationError(
                f"Detecte {len(accounts)} cuentas. Por seguridad procesamos hasta {MAX_PLAN_ACCOUNTS} por plan. Dividilo en varios planes."
            )
        normalized_targets: list[dict[str, Any]] = []
        for target in targets:
            if not isinstance(target, Mapping):
                raise AgentValidationError("Cada objetivo multi-cuenta debe ser un objeto.")
            account = self._normalize_account(target.get("account"))
            if account not in TARGET_BALANCE_ACCOUNTS:
                raise AgentValidationError("Una cuenta del plan multi-cuenta esta fuera de scope.")
            counter = target.get("counter_account") or target.get("contrapartida")
            if not counter:
                raise AgentValidationError("Cada step multi-cuenta debe indicar contrapartida.")
            ta = target.get("target_amount") if target.get("target_amount") is not None else target.get("amount")
            normalized_targets.append({
                "account": account,
                "month": target.get("month") or target.get("target_month"),
                "target_amount": ta,
                "currency": self._normalize_currency(target.get("currency") or "USD"),
                "counter_account": counter,
            })
        return self._solver.simulate_target_plan(
            kind="multi_account_target_balance",
            payload=payload,
            targets=normalized_targets,
            command_id=command_id,
            original_message=original_message,
            summary_prefix="Ajuste multi-cuenta",
        )

    def _build_non_negative_account_plan(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> dict[str, Any]:
        account = self._normalize_account(args.get("account") or args.get("target_account") or "cash")
        floor = float(_to_float(args.get("target_floor") or args.get("floor") or 0) or 0)
        buffer = float(_to_float(args.get("buffer_nio") or args.get("buffer") or 0) or 0)
        counter = args.get("counter_account") or args.get("contrapartida") or "loans_personal"
        months = self._months_scope(payload, args)
        return self._solver.build_non_negative_plan(
            payload,
            account=account,
            floor=floor,
            buffer=buffer,
            counter=counter,
            months=months,
            command_id=command_id,
            original_message=original_message,
        )

    def _build_target_utility_plan(
        self,
        payload: Mapping[str, Any],
        args: Mapping[str, Any],
        command_id: str,
        original_message: str,
    ) -> dict[str, Any]:
        target_usd = _target_amount_value(args) or _to_float(args.get("target_net_income_usd") or args.get("utility_usd"))
        if target_usd is None:
            raise AgentValidationError("Indique la utilidad anual objetivo en USD.")
        lever = str(args.get("lever") or args.get("palanca") or "cogs").strip().lower()
        if lever in {"cost", "costos", "costo"}:
            lever = "cogs"
        if lever in {"ingresos", "ingreso", "ventas"}:
            lever = "revenue"
        return self._solver.build_target_utility_plan(
            payload,
            target_usd=float(target_usd),
            lever=lever,
            command_id=command_id,
            original_message=original_message,
        )

    def _plan_steps(self, plan) -> list[dict[str, Any]]:
        try:
            data = json.loads(plan.steps_json or "[]")
        except Exception:
            data = []
        return [dict(step) for step in data if isinstance(step, Mapping)] if isinstance(data, list) else []

    def _plan_targets_from_args(self, payload: Mapping[str, Any], args: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_targets = args.get("targets") if isinstance(args.get("targets"), list) else []
        if raw_targets:
            valid_months = set(_payload_months(payload))
            normalized: list[dict[str, Any]] = []
            for item in raw_targets:
                if not isinstance(item, Mapping):
                    continue
                raw_month = item.get("month") or item.get("target_month")
                month = _normalize_month_value(raw_month) or (str(raw_month or "")[:7])
                if month not in valid_months:
                    raise AgentValidationError(
                        f"El mes {month or '(vacio)'} no esta dentro del periodo modelado. "
                        f"Meses validos: {', '.join(sorted(valid_months))}."
                    )
                ta = item.get("target_amount") if item.get("target_amount") is not None else item.get("amount")
                normalized.append({
                    "month": month,
                    "target_amount": ta,
                })
            return normalized
        months = self._months_scope(payload, args)
        # Importante: no usar `or` aqui porque average=0 es valido (llevar cuenta a cero)
        raw_avg: Any = None
        for key in ("average", "target_average", "promedio"):
            if key in args and args[key] is not None:
                raw_avg = args[key]
                break
        average = _to_float(raw_avg) if raw_avg is not None else None
        if average is None:
            amount = _target_amount_value(args)
            month = args.get("month") or args.get("target_month")
            if amount is None or not month:
                raise AgentValidationError("Indique targets por mes o un promedio con meses para armar el plan.")
            return [{"month": month, "target_amount": amount}]
        overrides = args.get("overrides") if isinstance(args.get("overrides"), Mapping) else {}
        exceptions = args.get("exceptions") if isinstance(args.get("exceptions"), Mapping) else {}
        merged_overrides = {**overrides, **exceptions}
        variability_pct = _to_float(args.get("variability_pct") or args.get("variabilidad_pct") or 0.0)
        return distribute_average(months, average, merged_overrides, variability_pct or 0.0)

    def _apply_plan_step_to_payload(
        self,
        step: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        plan_id: str,
        user_message: str,
    ) -> dict[str, Any]:
        return self._solver.apply_step(step, payload, plan_id=plan_id, user_message=user_message)

    def _verify_plan_step_against_result(self, step: Mapping[str, Any], result) -> None:
        return self._solver.verify_step(step, result)

    def _compute_plan_aggregate_impact(self, payload: Mapping[str, Any], projected_payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._solver.aggregate_impact(payload, projected_payload)

"""Construccion y simulacion de planes multi-paso del agente contable.

Mixin con la logica de planificacion (objetivos multi-mes, no-negatividad,
utilidad anual, ajustes multi-cuenta) y la simulacion paso a paso.

El mixin asume que el host implementa metodos como `_normalize_account`,
`_normalize_currency`, `_months_scope`, `_er_month_value`,
`_prepare_target_balance_adjustment` y `_prepare_monthly_override`, y
expone `self.tools` (AgentToolRegistry).
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping

from financial_model import build_financial_model
from services.agent_constants import MAX_PLAN_ACCOUNTS, TARGET_BALANCE_ACCOUNTS
from services.agent_errors import AgentValidationError
from services.agent_helpers import (
    _er_value,
    _normalize_month_value,
    _payload_months,
    _period_exchange_rate,
    _statement_value,
    _target_amount_value,
    _to_float,
)


class AgentPlanBuilderMixin:
    """Construye y simula planes multi-paso para AgentCommandService."""

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
        return self._simulate_target_plan(
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
        return self._simulate_target_plan(
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
        if account not in TARGET_BALANCE_ACCOUNTS:
            raise AgentValidationError("La restriccion de no-negatividad solo esta habilitada para cuentas de balance soportadas.")
        floor = float(_to_float(args.get("target_floor") or args.get("floor") or 0) or 0)
        buffer = float(_to_float(args.get("buffer_nio") or args.get("buffer") or 0) or 0)
        counter = args.get("counter_account") or args.get("contrapartida") or "loans_personal"
        months = self._months_scope(payload, args)
        label = TARGET_BALANCE_ACCOUNTS[account]["label"]
        working = deepcopy(dict(payload))
        targets: list[dict[str, Any]] = []
        for month in months:
            result = build_financial_model(working)
            current = _statement_value(result, label, month)
            if current < floor:
                targets.append({
                    "account": account,
                    "month": month,
                    "target_amount": floor + buffer,
                    "currency": "NIO",
                    "counter_account": counter,
                })
                working, _proposal = self._plan_target_projection(
                    targets[-1],
                    working,
                    command_id=command_id,
                    original_message=original_message,
                )
        if not targets:
            return {"no_plan": True, "assistant_message": f"{label} ya cumple el piso de C${floor:,.2f} en el periodo."}
        return self._simulate_target_plan(
            kind="non_negative_account",
            payload=payload,
            targets=targets,
            command_id=command_id,
            original_message=original_message,
            summary_prefix=f"No-negatividad de {label}",
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
        if lever not in {"revenue", "cogs"}:
            raise AgentValidationError("La palanca debe ser revenue o cogs.")

        result = build_financial_model(payload)
        rate = _period_exchange_rate(payload)
        current_nio = float(result.summary.get("net_income_total") or 0)
        target_nio = round(float(target_usd) * rate, 2)
        delta_nio = round(target_nio - current_nio, 2)
        if abs(delta_nio) > max(abs(current_nio) * 3, rate):
            raise AgentValidationError("El delta requerido para esa utilidad es demasiado alto para un ajuste automatico de una sola palanca.")
        months = [str(m) for m in (result.summary.get("all_months") or result.summary.get("months") or _payload_months(payload))]
        if not months:
            raise AgentValidationError("No encontre meses para distribuir la utilidad objetivo.")
        values = [abs(self._er_month_value(result, "Ingresos" if lever == "revenue" else "(-) Costo de ventas", m)) for m in months]
        total = sum(values) or float(len(months))
        working = deepcopy(dict(payload))
        steps: list[dict[str, Any]] = []
        for index, month in enumerate(months, start=1):
            before_result = build_financial_model(working)
            current_revenue = self._er_month_value(before_result, "Ingresos", month)
            current_cogs = self._er_month_value(before_result, "(-) Costo de ventas", month)
            share = (values[index - 1] or 1.0) / total
            allocated_delta = round(delta_nio * share, 2)
            if lever == "revenue":
                after_revenue_usd = round((current_revenue + allocated_delta) / rate, 2)
                if after_revenue_usd < 0:
                    raise AgentValidationError("El objetivo dejaria ingresos negativos en un mes.")
                step = {
                    "step_order": index,
                    "kind": "monthly_override",
                    "month": month,
                    "field": "revenue_usd",
                    "before_revenue_usd": round(current_revenue / rate, 2),
                    "after_revenue_usd": after_revenue_usd,
                    "target_amount_nio": round(current_revenue + allocated_delta, 2),
                    "expected_delta_nio": allocated_delta,
                }
            else:
                after_cogs_nio = current_cogs - allocated_delta
                if after_cogs_nio < 0:
                    raise AgentValidationError("El objetivo dejaria costo de ventas negativo en un mes.")
                step = {
                    "step_order": index,
                    "kind": "monthly_override",
                    "month": month,
                    "field": "cogs_usd",
                    "before_cogs_usd": round(current_cogs / rate, 2),
                    "after_cogs_usd": round(after_cogs_nio / rate, 2),
                    "target_amount_nio": round(after_cogs_nio, 2),
                    "expected_delta_nio": round(-allocated_delta, 2),
                }
            working = self._apply_plan_step_to_payload(step, working, plan_id="preview", user_message=original_message)
            self._verify_plan_step_against_result(step, build_financial_model(working))
            steps.append(step)
        return {
            "kind": "target_utility",
            "plan_summary": f"Ajuste de utilidad anual a USD {float(target_usd):,.2f} usando {lever}",
            "assistant_message": f"Prepare un plan para llevar la utilidad anual a USD {float(target_usd):,.2f} usando {lever}.",
            "steps": steps,
            "aggregate_impact": self._compute_plan_aggregate_impact(payload, working),
        }

    def _simulate_target_plan(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any],
        targets: list[Mapping[str, Any]],
        command_id: str,
        original_message: str,
        summary_prefix: str,
    ) -> dict[str, Any]:
        working = deepcopy(dict(payload))
        steps: list[dict[str, Any]] = []
        seen_months: set[tuple[str, str]] = set()
        valid_months = set(_payload_months(payload))
        for raw in targets:
            account = self._normalize_account(raw.get("account"))
            month = _normalize_month_value(raw.get("month") or raw.get("target_month")) or str(raw.get("month") or "")[:7]
            if (account, month) in seen_months:
                raise AgentValidationError(f"El plan tiene objetivo duplicado para {account} en {month}.")
            seen_months.add((account, month))
            if month not in valid_months:
                raise AgentValidationError(f"El mes {month or '(vacio)'} no esta dentro del periodo modelado.")
            raw_amount = raw.get("target_amount") if raw.get("target_amount") is not None else raw.get("amount")
            step = {
                "step_order": len(steps) + 1,
                "kind": "target_balance",
                "account": account,
                "month": month,
                "target_amount": _to_float(raw_amount) if raw_amount is not None else 0.0,
                "currency": self._normalize_currency(raw.get("currency") or "USD"),
                "counter_account": raw.get("counter_account") or raw.get("contrapartida"),
            }
            try:
                projected, proposal = self._plan_target_projection(
                    step,
                    working,
                    command_id=command_id,
                    original_message=original_message,
                )
            except AgentValidationError as exc:
                if "ya cierra cerca del objetivo" in str(exc):
                    continue
                raise
            target = dict(proposal.get("target") or {})
            step.update({
                "target_amount_nio": target.get("target_amount_nio"),
                "current_balance_nio_before": target.get("current_balance_nio_before"),
                "expected_delta_nio": target.get("delta_applied_nio"),
                "counter_account": proposal.get("counter_account"),
                "counter_account_label": proposal.get("counter_account_label"),
                "account_label": target.get("account_label"),
            })
            working = projected
            self._verify_plan_step_against_result(step, build_financial_model(working))
            steps.append(step)
        if not steps:
            return {"no_plan": True, "assistant_message": "Todos los saldos ya cumplen los objetivos; no hace falta plan."}
        return {
            "kind": kind,
            "plan_summary": f"{summary_prefix}: {len(steps)} paso(s)",
            "assistant_message": f"Prepare un plan de {len(steps)} paso(s). Revisalo antes de aplicarlo.",
            "steps": steps,
            "aggregate_impact": self._compute_plan_aggregate_impact(payload, working),
        }

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
        distribution = self.tools.run(
            "compute_target_distribution",
            payload,
            {"months": months, "average": average, "overrides": merged_overrides, "variability_pct": variability_pct},
        )
        targets = distribution.get("data", {}).get("targets") if isinstance(distribution.get("data"), Mapping) else []
        return [dict(item) for item in targets if isinstance(item, Mapping)]

    def _plan_target_projection(
        self,
        step: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        command_id: str,
        original_message: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        args = {
            "account": step.get("account"),
            "month": step.get("month"),
            "target_amount": step.get("target_amount"),
            "currency": step.get("currency"),
            "counter_account": step.get("counter_account"),
        }
        return self._prepare_target_balance_adjustment(payload, args, command_id, original_message)

    def _apply_plan_step_to_payload(
        self,
        step: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        plan_id: str,
        user_message: str,
    ) -> dict[str, Any]:
        kind = str(step.get("kind") or "")
        if kind == "target_balance":
            projected, _proposal = self._plan_target_projection(
                step,
                payload,
                command_id=f"plan_{plan_id}_{step.get('step_order')}",
                original_message=user_message,
            )
            return projected
        if kind == "monthly_override":
            month = str(step.get("month") or "")[:7]
            update: dict[str, Any] = {"month": month, "note": f"Plan {plan_id}"}
            if step.get("field") == "revenue_usd":
                update["revenue_usd"] = step.get("after_revenue_usd")
            elif step.get("field") == "cogs_usd":
                update["cogs_usd"] = step.get("after_cogs_usd")
            else:
                raise AgentValidationError("Step monthly_override sin campo valido.")
            projected, _proposal = self._prepare_monthly_override(
                payload,
                {"updates": [update]},
                f"plan_{plan_id}_{step.get('step_order')}",
                user_message,
            )
            return projected
        raise AgentValidationError(f"Tipo de step no soportado: {kind or '(vacio)'}")

    def _verify_plan_step_against_result(self, step: Mapping[str, Any], result) -> None:
        kind = str(step.get("kind") or "")
        month = str(step.get("month") or "")[:7]
        if kind == "target_balance":
            account = str(step.get("account") or "")
            label = TARGET_BALANCE_ACCOUNTS.get(account, {}).get("label", "")
            target_nio = float(_to_float(step.get("target_amount_nio")) or 0.0)
            actual = _statement_value(result, label, month)
            if abs(actual - target_nio) > 1.0:
                raise AgentValidationError(
                    f"El step no llevo {label} al objetivo. Objetivo C$ {target_nio:,.2f}, resultado C$ {actual:,.2f}."
                )
            return
        if kind == "monthly_override":
            field = str(step.get("field") or "")
            label = "Ingresos" if field == "revenue_usd" else "(-) Costo de ventas"
            target_nio = float(_to_float(step.get("target_amount_nio")) or 0.0)
            actual = self._er_month_value(result, label, month)
            if abs(actual - target_nio) > 1.0:
                raise AgentValidationError(
                    f"El step no llevo {label} al objetivo. Objetivo C$ {target_nio:,.2f}, resultado C$ {actual:,.2f}."
                )
            return
        raise AgentValidationError(f"No hay verificacion para el step {kind or '(vacio)'}.")

    def _compute_plan_aggregate_impact(self, payload: Mapping[str, Any], projected_payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            before = build_financial_model(payload)
            after = build_financial_model(projected_payload)
        except Exception:
            return {
                "revenue_total_delta": 0,
                "cogs_total_delta": 0,
                "net_income_total_delta": 0,
                "total_assets_end_delta": 0,
                "cash_end_delta": 0,
                "safety_warnings": [],
            }
        months = after.summary.get("all_months") or after.summary.get("months") or before.summary.get("all_months") or before.summary.get("months") or []
        last_month = str(months[-1]) if months else ""
        return {
            "revenue_total_delta": round(float(after.summary.get("income_total") or 0) - float(before.summary.get("income_total") or 0), 2),
            "cogs_total_delta": round(_er_value(after, "(-) Costo de ventas") - _er_value(before, "(-) Costo de ventas"), 2),
            "net_income_total_delta": round(float(after.summary.get("net_income_total") or 0) - float(before.summary.get("net_income_total") or 0), 2),
            "total_assets_end_delta": round(_statement_value(after, "Total Activos", last_month) - _statement_value(before, "Total Activos", last_month), 2),
            "cash_end_delta": round(_statement_value(after, "Efectivo y Equivalentes de Efectivo", last_month) - _statement_value(before, "Efectivo y Equivalentes de Efectivo", last_month), 2),
            "safety_warnings": self._detect_plan_safety_warnings(before, after, months),
        }

    @staticmethod
    def _detect_plan_safety_warnings(before_model, after_model, months: list[str]) -> list[dict[str, Any]]:
        """Detecta cuentas operativas que pasaron a negativo (o quedaron mas negativas) tras el plan.

        No vigila todas las cuentas del catalogo (eso seria ruido); solo las operativas
        comunes donde un saldo negativo casi siempre es bug, no decision contable.
        """
        watched = [
            ("cash", "Efectivo y Equivalentes de Efectivo"),
            ("accounts_receivable", "Cuentas por Cobrar Clientes"),
            ("inventory", "Inventarios"),
            ("suppliers", "Proveedores"),
        ]
        warnings: list[dict[str, Any]] = []
        for account_code, account_label in watched:
            for month in months:
                month = str(month)
                before_val = _statement_value(before_model, account_label, month)
                after_val = _statement_value(after_model, account_label, month)
                # Vigilar cualquier mes que termina negativo (sea por el plan o no)
                if after_val < 0:
                    severity = "negative_after_plan"
                    if before_val < 0 and after_val >= before_val:
                        # ya estaba negativo y mejoro o quedo igual -> no es problema del plan
                        continue
                    if before_val < 0 and after_val < before_val:
                        severity = "more_negative_after_plan"
                    warnings.append({
                        "account": account_code,
                        "account_label": account_label,
                        "month": month,
                        "before_nio": round(before_val, 2),
                        "after_nio": round(after_val, 2),
                        "severity": severity,
                    })
        return warnings

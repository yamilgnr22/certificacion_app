"""Nucleo del solver de restricciones contables (F2-T1).

API pura sobre el motor deterministico: dado un payload y una lista de
``Constraint``, produce los pasos (steps) que cumplen las metas sin violar
los invariantes contables, o reporta por que no es posible (``feasible``
``False`` con ``infeasible_reason``).

La proyeccion numerica de cada paso reutiliza los builders auditados del
host (``AgentCommandService``): el solver orquesta, simula paso a paso y
verifica cada resultado contra su objetivo; no duplica la logica contable
ni toca LLM/HTTP por si mismo. Los formatos de step son exactamente los
que ``AgentPlan.steps_json`` persiste, de modo que ``apply_plan`` puede
reproducirlos.
"""

from __future__ import annotations

import hashlib
import logging
import random
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from model_cache import cached_build_financial_model as build_financial_model
from services.agent_constants import TARGET_BALANCE_ACCOUNTS
from services.agent_errors import AgentValidationError
from services.agent_helpers import (
    _er_value,
    _normalize_month_value,
    _payload_months,
    _period_exchange_rate,
    _statement_value,
    _to_float,
)


logger = logging.getLogger(__name__)


@dataclass
class Constraint:
    """Meta declarativa sobre el modelo financiero.

    kind:
      - "target": la cuenta debe cerrar en `amount` en `month`.
      - "average": el saldo de la cuenta debe promediar `amount` en `months`
        (con `variability_pct` opcional para oscilacion realista).
      - "floor": la cuenta no debe bajar de `amount` (+`buffer`) en `months`.
      - "utility": la utilidad anual debe ser `amount` USD usando `lever`.
    """

    kind: str
    account: str = ""
    month: str = ""
    months: list[str] = field(default_factory=list)
    amount: float | None = None
    currency: str = "USD"
    counter_account: str = ""
    variability_pct: float = 0.0
    overrides: dict[str, float] = field(default_factory=dict)
    buffer: float = 0.0
    lever: str = "cogs"


@dataclass
class SolveResult:
    feasible: bool
    kind: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    plan_summary: str = ""
    assistant_message: str = ""
    aggregate_impact: dict[str, Any] = field(default_factory=dict)
    infeasible_reason: str = ""
    no_plan: bool = False


class SolverHost(Protocol):
    """Interfaz minima que el solver necesita del servicio anfitrion."""

    def _normalize_account(self, raw_account: Any) -> str: ...

    def _normalize_currency(self, value: Any) -> str: ...

    def _er_month_value(self, result: Any, description: str, month: str) -> float: ...

    def _prepare_target_balance_adjustment(
        self, payload: Mapping[str, Any], args: Mapping[str, Any], command_id: str, original_message: str
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def _prepare_monthly_override(
        self, payload: Mapping[str, Any], args: Mapping[str, Any], command_id: str, original_message: str
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...


def distribute_average(
    months: list[Any],
    average: float,
    overrides: Mapping[str, Any] | None = None,
    variability_pct: float = 0.0,
) -> list[dict[str, Any]]:
    """Distribuye un promedio objetivo en montos por mes.

    Los meses con override fijo conservan su valor; el resto se reparte de
    forma que el promedio del conjunto caiga exacto en `average`. Con
    `variability_pct` > 0 los meses libres oscilan de forma deterministica
    (seed derivada de los argumentos) y se renormalizan al promedio.
    """
    months = [str(m).strip()[:7] for m in (months or []) if str(m or "").strip()]
    average = float(_to_float(average) or 0.0)
    override_values = {str(k).strip()[:7]: float(_to_float(v) or 0.0) for k, v in dict(overrides or {}).items()}
    variability_pct = max(0.0, min(50.0, float(_to_float(variability_pct) or 0.0)))
    variability_frac = variability_pct / 100.0

    fixed_total = sum(v for v in override_values.values() if v > 0)
    free_months = [m for m in months if override_values.get(m, 0) <= 0]
    free_avg = (average * len(months) - fixed_total) / len(free_months) if free_months else average

    free_values: dict[str, float] = {}
    if free_months:
        if variability_frac > 0:
            # Seed deterministico: misma combinacion (meses + promedio +
            # variabilidad) => mismos valores
            seed_source = f"{','.join(months)}|{average}|{variability_pct}|{','.join(sorted(override_values))}"
            rng = random.Random(hashlib.sha256(seed_source.encode("utf-8")).hexdigest())
            raw = [free_avg * (1.0 + rng.uniform(-variability_frac, variability_frac)) for _ in free_months]
            # Normalizar para que el promedio efectivo de los meses libres
            # caiga exacto en free_avg
            actual_avg = sum(raw) / len(raw)
            scale = (free_avg / actual_avg) if actual_avg else 1.0
            free_values = {m: max(0.0, raw[i] * scale) for i, m in enumerate(free_months)}
        else:
            free_values = {m: free_avg for m in free_months}

    return [
        {
            "month": month,
            "target_amount": round(override_values[month], 2) if override_values.get(month, 0) > 0 else round(free_values.get(month, free_avg), 2),
        }
        for month in months
    ]


def _warning_suffix(impact: Mapping[str, Any]) -> str:
    """F2-T2: resume con cifras el peor safety warning del plan, si existe."""
    warnings = list(impact.get("safety_warnings") or [])
    if not warnings:
        return ""
    worst = min(warnings, key=lambda w: float(_to_float(w.get("after_nio")) or 0.0))
    extra = f" (y {len(warnings) - 1} aviso(s) mas)" if len(warnings) > 1 else ""
    return (
        f" Ojo: {worst.get('account_label')} quedaria en C$ {float(_to_float(worst.get('after_nio')) or 0.0):,.2f} "
        f"en {worst.get('month')}{extra}."
    )


def diagnose_negative_targets(
    targets: list[Mapping[str, Any]],
    *,
    label: str,
    average: Any,
    currency: str,
    overrides: Mapping[str, Any] | None,
) -> None:
    """F2-T2: si la distribucion requiere saldos negativos, explica con cifras.

    Un promedio bajo combinado con montos fijos altos obliga a los meses
    libres a cerrar en negativo, lo que nunca es aplicable como saldo.
    """
    negative = [t for t in targets if float(_to_float(t.get("target_amount")) or 0.0) < 0]
    if not negative:
        return
    fixed_total = sum(
        float(_to_float(value) or 0.0)
        for value in (overrides or {}).values()
        if float(_to_float(value) or 0.0) > 0
    )
    worst = min(negative, key=lambda t: float(_to_float(t.get("target_amount")) or 0.0))
    worst_amount = float(_to_float(worst.get("target_amount")) or 0.0)
    raise AgentValidationError(
        f"El promedio objetivo de {label} ({float(_to_float(average) or 0.0):,.2f} {currency}) es imposible con esos montos fijos: "
        f"los meses fijados suman {fixed_total:,.2f} y obligarian a los meses libres a cerrar en {worst_amount:,.2f} "
        f"(negativo, por ejemplo en {worst.get('month')}). Baja los montos fijos o subi el promedio."
    )


class ConstraintSolver:
    """Resuelve metas declarativas produciendo steps verificados."""

    def __init__(self, host: SolverHost):
        self.host = host

    # ------------------------------------------------------------------
    # API publica
    # ------------------------------------------------------------------

    def solve(
        self,
        payload: Mapping[str, Any],
        constraints: list[Constraint],
        *,
        command_id: str = "solver",
        original_message: str = "",
    ) -> SolveResult:
        constraints = list(constraints or [])
        if not constraints:
            return SolveResult(feasible=False, infeasible_reason="No hay restricciones que resolver.")
        try:
            plan = self._plan_for(payload, constraints, command_id, original_message)
        except AgentValidationError as exc:
            return SolveResult(feasible=False, infeasible_reason=str(exc))
        if plan.get("no_plan"):
            return SolveResult(
                feasible=True,
                no_plan=True,
                kind=str(plan.get("kind") or ""),
                assistant_message=str(plan.get("assistant_message") or ""),
            )
        return SolveResult(
            feasible=True,
            kind=str(plan.get("kind") or ""),
            steps=list(plan.get("steps") or []),
            plan_summary=str(plan.get("plan_summary") or ""),
            assistant_message=str(plan.get("assistant_message") or ""),
            aggregate_impact=dict(plan.get("aggregate_impact") or {}),
        )

    def _plan_for(
        self,
        payload: Mapping[str, Any],
        constraints: list[Constraint],
        command_id: str,
        original_message: str,
    ) -> dict[str, Any]:
        kinds = {c.kind for c in constraints}
        if kinds == {"target"}:
            targets = [
                {
                    "account": c.account,
                    "month": c.month or (c.months[0] if c.months else ""),
                    "target_amount": c.amount,
                    "currency": c.currency,
                    "counter_account": c.counter_account,
                }
                for c in constraints
            ]
            accounts = {c.account for c in constraints}
            return self.simulate_target_plan(
                kind="multi_account_target_balance" if len(accounts) > 1 else "multi_target_balance",
                payload=payload,
                targets=targets,
                command_id=command_id,
                original_message=original_message,
                summary_prefix="Ajuste por objetivos",
            )
        if len(constraints) != 1:
            return self._solve_compound(payload, constraints, command_id, original_message)
        constraint = constraints[0]
        if constraint.kind == "average":
            months = constraint.months or _payload_months(payload)
            label = TARGET_BALANCE_ACCOUNTS.get(constraint.account, {}).get("label", constraint.account)
            targets = [
                {
                    **item,
                    "account": constraint.account,
                    "currency": constraint.currency,
                    "counter_account": constraint.counter_account,
                }
                for item in distribute_average(months, constraint.amount or 0.0, constraint.overrides, constraint.variability_pct)
            ]
            diagnose_negative_targets(
                targets,
                label=label,
                average=constraint.amount,
                currency=constraint.currency,
                overrides=constraint.overrides,
            )
            return self.simulate_target_plan(
                kind="multi_target_balance",
                payload=payload,
                targets=targets,
                command_id=command_id,
                original_message=original_message,
                summary_prefix=f"Ajuste multi-mes de {label}",
            )
        if constraint.kind == "floor":
            floor_nio = float(constraint.amount or 0.0)
            if str(constraint.currency or "").upper() == "USD":
                floor_nio = round(floor_nio * _period_exchange_rate(payload), 2)
            return self.build_non_negative_plan(
                payload,
                account=constraint.account,
                floor=floor_nio,
                buffer=constraint.buffer,
                counter=constraint.counter_account or "loans_personal",
                months=constraint.months or _payload_months(payload),
                command_id=command_id,
                original_message=original_message,
            )
        if constraint.kind == "utility":
            return self.build_target_utility_plan(
                payload,
                target_usd=float(constraint.amount or 0.0),
                lever=constraint.lever,
                command_id=command_id,
                original_message=original_message,
            )
        raise AgentValidationError(f"Tipo de restriccion no soportado: {constraint.kind or '(vacio)'}")

    # ------------------------------------------------------------------
    # Plan combinado (F2-T3): metas heterogeneas en una sola instruccion
    # ------------------------------------------------------------------

    def plan_constraints(
        self,
        payload: Mapping[str, Any],
        constraints: list[Constraint],
        *,
        command_id: str,
        original_message: str,
    ) -> dict[str, Any]:
        """Version dict de solve() para el flujo del agente (lanza
        AgentValidationError en infactibilidad, como los demas builders)."""
        return self._plan_for(payload, list(constraints or []), command_id, original_message)

    def _solve_compound(
        self,
        payload: Mapping[str, Any],
        constraints: list[Constraint],
        command_id: str,
        original_message: str,
    ) -> dict[str, Any]:
        if len(constraints) > 4:
            raise AgentValidationError("Hasta 4 objetivos por plan combinado; dividilo en varios planes.")
        # Orden de dependencia: la utilidad mueve ingresos/costos (y con ellos
        # caja e inventario), los balances no-caja se ajustan con contrapartidas
        # que no tocan caja, y la caja va al final porque absorbe el efecto de
        # todo lo anterior.
        ordered = sorted(constraints, key=self._constraint_priority)
        working = deepcopy(dict(payload))
        all_steps: list[dict[str, Any]] = []
        for index, constraint in enumerate(ordered):
            plan = self._plan_for(working, [constraint], command_id, original_message)
            if not plan.get("no_plan"):
                for step in plan.get("steps") or []:
                    step = dict(step)
                    step["step_order"] = len(all_steps) + 1
                    working = self.apply_step(step, working, plan_id="compound", user_message=original_message)
                    all_steps.append(step)
            result = build_financial_model(working)
            for previous in ordered[:index]:
                failure = self.check_constraint(previous, result, payload)
                if failure:
                    raise AgentValidationError(
                        f"Conflicto entre objetivos: al aplicar '{self._describe_constraint(constraint)}' "
                        f"se rompe '{self._describe_constraint(previous)}': esperado C$ {failure['expected']:,.2f}, "
                        f"quedaria C$ {failure['actual']:,.2f} (diferencia C$ {failure['difference']:,.2f}). "
                        "Cambia la contrapartida de uno de los dos o procesalos por separado."
                    )
        if not all_steps:
            return {"no_plan": True, "assistant_message": "Todos los objetivos ya se cumplen; no hace falta plan."}
        result = build_financial_model(working)
        for constraint in ordered:
            failure = self.check_constraint(constraint, result, payload)
            if failure:
                raise AgentValidationError(
                    f"El plan combinado no logro cumplir '{self._describe_constraint(constraint)}': "
                    f"esperado C$ {failure['expected']:,.2f}, quedaria C$ {failure['actual']:,.2f}."
                )
        impact = self.aggregate_impact(payload, working)
        return {
            "kind": "compound_constraints",
            "plan_summary": f"Plan combinado: {len(ordered)} objetivo(s), {len(all_steps)} paso(s)",
            "assistant_message": (
                f"Prepare un plan combinado de {len(all_steps)} paso(s) para {len(ordered)} objetivo(s). "
                f"Revisalo antes de aplicarlo.{_warning_suffix(impact)}"
            ),
            "steps": all_steps,
            "aggregate_impact": impact,
        }

    @staticmethod
    def _constraint_priority(constraint: Constraint) -> int:
        if constraint.kind == "utility":
            return 0
        is_cash = constraint.account == "cash"
        if not is_cash and constraint.kind in {"target", "average"}:
            return 1
        if not is_cash and constraint.kind == "floor":
            return 2
        return 3

    @staticmethod
    def _describe_constraint(constraint: Constraint) -> str:
        label = TARGET_BALANCE_ACCOUNTS.get(constraint.account, {}).get("label", constraint.account)
        amount = float(_to_float(constraint.amount) or 0.0)
        if constraint.kind == "target":
            return f"{label} = {amount:,.2f} {constraint.currency} en {constraint.month}"
        if constraint.kind == "average":
            return f"promedio de {label} = {amount:,.2f} {constraint.currency}"
        if constraint.kind == "floor":
            return f"piso de {label} = {amount:,.2f} {constraint.currency}"
        if constraint.kind == "utility":
            return f"utilidad anual = USD {amount:,.2f}"
        return constraint.kind or "(objetivo)"

    def check_constraint(
        self,
        constraint: Constraint,
        result: Any,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Verifica una meta contra un modelo ya construido.

        Devuelve None si se cumple, o {expected, actual, difference, month}
        en NIO si quedo rota (tolerancias: C$1 por mes; promedios y utilidad
        toleran el redondeo acumulado de sus meses).
        """
        rate = _period_exchange_rate(payload)
        factor = rate if str(constraint.currency or "").upper() == "USD" else 1.0
        label = TARGET_BALANCE_ACCOUNTS.get(constraint.account, {}).get("label", constraint.account)
        amount = float(_to_float(constraint.amount) or 0.0)
        if constraint.kind == "target":
            month = constraint.month or (constraint.months[0] if constraint.months else "")
            expected = round(amount * factor, 2)
            actual = _statement_value(result, label, month)
            if abs(actual - expected) > 1.0:
                return {"expected": expected, "actual": round(actual, 2), "difference": round(actual - expected, 2), "month": month}
            return None
        if constraint.kind == "average":
            months = constraint.months or _payload_months(payload)
            expected = round(amount * factor, 2)
            values = [_statement_value(result, label, month) for month in months]
            actual = round(sum(values) / len(values), 2) if values else 0.0
            if abs(actual - expected) > max(2.0, float(len(months))):
                return {"expected": expected, "actual": actual, "difference": round(actual - expected, 2), "month": ""}
            return None
        if constraint.kind == "floor":
            floor_nio = round(amount * factor, 2)
            months = constraint.months or _payload_months(payload)
            for month in months:
                actual = _statement_value(result, label, month)
                if actual < floor_nio - 1.0:
                    return {"expected": floor_nio, "actual": round(actual, 2), "difference": round(actual - floor_nio, 2), "month": month}
            return None
        if constraint.kind == "utility":
            expected = round(amount * rate, 2)
            actual = float(result.summary.get("net_income_total") or 0.0)
            month_count = len(result.summary.get("months") or []) or 1
            if abs(actual - expected) > max(2.0, float(month_count)):
                return {"expected": expected, "actual": round(actual, 2), "difference": round(actual - expected, 2), "month": ""}
            return None
        return None

    # ------------------------------------------------------------------
    # Simulacion (movida desde AgentPlanBuilderMixin)
    # ------------------------------------------------------------------

    def simulate_target_plan(
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
            account = self.host._normalize_account(raw.get("account"))
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
                "currency": self.host._normalize_currency(raw.get("currency") or "USD"),
                "counter_account": raw.get("counter_account") or raw.get("contrapartida"),
            }
            try:
                projected, proposal = self.project_target(
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
            self.verify_step(step, build_financial_model(working))
            steps.append(step)
        if not steps:
            return {"no_plan": True, "assistant_message": "Todos los saldos ya cumplen los objetivos; no hace falta plan."}
        impact = self.aggregate_impact(payload, working)
        return {
            "kind": kind,
            "plan_summary": f"{summary_prefix}: {len(steps)} paso(s)",
            "assistant_message": f"Prepare un plan de {len(steps)} paso(s). Revisalo antes de aplicarlo.{_warning_suffix(impact)}",
            "steps": steps,
            "aggregate_impact": impact,
        }

    def build_non_negative_plan(
        self,
        payload: Mapping[str, Any],
        *,
        account: str,
        floor: float,
        buffer: float,
        counter: Any,
        months: list[str],
        command_id: str,
        original_message: str,
    ) -> dict[str, Any]:
        if account not in TARGET_BALANCE_ACCOUNTS:
            raise AgentValidationError("La restriccion de no-negatividad solo esta habilitada para cuentas de balance soportadas.")
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
                working, _proposal = self.project_target(
                    targets[-1],
                    working,
                    command_id=command_id,
                    original_message=original_message,
                )
        if not targets:
            return {"no_plan": True, "assistant_message": f"{label} ya cumple el piso de C${floor:,.2f} en el periodo."}
        return self.simulate_target_plan(
            kind="non_negative_account",
            payload=payload,
            targets=targets,
            command_id=command_id,
            original_message=original_message,
            summary_prefix=f"No-negatividad de {label}",
        )

    def build_target_utility_plan(
        self,
        payload: Mapping[str, Any],
        *,
        target_usd: float,
        lever: str,
        command_id: str,
        original_message: str,
    ) -> dict[str, Any]:
        if lever not in {"revenue", "cogs"}:
            raise AgentValidationError("La palanca debe ser revenue o cogs.")
        result = build_financial_model(payload)
        rate = _period_exchange_rate(payload)
        current_nio = float(result.summary.get("net_income_total") or 0)
        target_nio = round(float(target_usd) * rate, 2)
        delta_nio = round(target_nio - current_nio, 2)
        limit_nio = max(abs(current_nio) * 3, rate)
        if abs(delta_nio) > limit_nio:
            raise AgentValidationError(
                f"La utilidad objetivo esta demasiado lejos para una sola palanca: pediste USD {float(target_usd):,.2f} "
                f"(C$ {target_nio:,.2f}), la utilidad actual del periodo es C$ {current_nio:,.2f} y el ajuste requerido "
                f"C$ {delta_nio:,.2f} excede el limite automatico de C$ {limit_nio:,.2f} (3x la utilidad actual). "
                "Procesalo por partes o fija ingresos/costos exactos por mes."
            )
        months = [str(m) for m in (result.summary.get("all_months") or result.summary.get("months") or _payload_months(payload))]
        if not months:
            raise AgentValidationError("No encontre meses para distribuir la utilidad objetivo.")
        values = [abs(self.host._er_month_value(result, "Ingresos" if lever == "revenue" else "(-) Costo de ventas", m)) for m in months]
        total = sum(values) or float(len(months))
        working = deepcopy(dict(payload))
        steps: list[dict[str, Any]] = []
        for index, month in enumerate(months, start=1):
            before_result = build_financial_model(working)
            current_revenue = self.host._er_month_value(before_result, "Ingresos", month)
            current_cogs = self.host._er_month_value(before_result, "(-) Costo de ventas", month)
            share = (values[index - 1] or 1.0) / total
            allocated_delta = round(delta_nio * share, 2)
            if lever == "revenue":
                after_revenue_usd = round((current_revenue + allocated_delta) / rate, 2)
                if after_revenue_usd < 0:
                    raise AgentValidationError(
                        f"El objetivo dejaria ingresos negativos en {month}: ingresos actuales C$ {current_revenue:,.2f} "
                        f"+ ajuste C$ {allocated_delta:,.2f} = C$ {current_revenue + allocated_delta:,.2f}."
                    )
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
                    raise AgentValidationError(
                        f"El objetivo dejaria el costo de ventas negativo en {month}: costo actual C$ {current_cogs:,.2f} "
                        f"- ajuste C$ {allocated_delta:,.2f} = C$ {after_cogs_nio:,.2f}."
                    )
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
            working = self.apply_step(step, working, plan_id="preview", user_message=original_message)
            self.verify_step(step, build_financial_model(working))
            steps.append(step)
        impact = self.aggregate_impact(payload, working)
        return {
            "kind": "target_utility",
            "plan_summary": f"Ajuste de utilidad anual a USD {float(target_usd):,.2f} usando {lever}",
            "assistant_message": f"Prepare un plan para llevar la utilidad anual a USD {float(target_usd):,.2f} usando {lever}.{_warning_suffix(impact)}",
            "steps": steps,
            "aggregate_impact": impact,
        }

    # ------------------------------------------------------------------
    # Proyeccion, aplicacion y verificacion de steps
    # ------------------------------------------------------------------

    def project_target(
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
        return self.host._prepare_target_balance_adjustment(payload, args, command_id, original_message)

    def apply_step(
        self,
        step: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        plan_id: str,
        user_message: str,
    ) -> dict[str, Any]:
        kind = str(step.get("kind") or "")
        if kind == "target_balance":
            projected, _proposal = self.project_target(
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
            projected, _proposal = self.host._prepare_monthly_override(
                payload,
                {"updates": [update]},
                f"plan_{plan_id}_{step.get('step_order')}",
                user_message,
            )
            return projected
        raise AgentValidationError(f"Tipo de step no soportado: {kind or '(vacio)'}")

    def verify_step(self, step: Mapping[str, Any], result: Any) -> None:
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
            field_name = str(step.get("field") or "")
            label = "Ingresos" if field_name == "revenue_usd" else "(-) Costo de ventas"
            target_nio = float(_to_float(step.get("target_amount_nio")) or 0.0)
            actual = self.host._er_month_value(result, label, month)
            if abs(actual - target_nio) > 1.0:
                raise AgentValidationError(
                    f"El step no llevo {label} al objetivo. Objetivo C$ {target_nio:,.2f}, resultado C$ {actual:,.2f}."
                )
            return
        raise AgentValidationError(f"No hay verificacion para el step {kind or '(vacio)'}.")

    # ------------------------------------------------------------------
    # Impacto agregado y safety warnings
    # ------------------------------------------------------------------

    def aggregate_impact(self, payload: Mapping[str, Any], projected_payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            before = build_financial_model(payload)
            after = build_financial_model(projected_payload)
        except Exception:
            logger.warning("No se pudo calcular el impacto agregado del plan; se reporta en cero", exc_info=True)
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
            "safety_warnings": self.detect_safety_warnings(before, after, months),
        }

    @staticmethod
    def detect_safety_warnings(before_model: Any, after_model: Any, months: list[str]) -> list[dict[str, Any]]:
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

from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base
from financial_model import build_financial_model
from model_cache import clear_model_cache
from services.agent_helpers import _statement_value
from services.agent_service import AgentCommandService
from services.solver import Constraint, ConstraintSolver, distribute_average

RATE = 36.6243


def solver_payload():
    return {
        "period": {
            "start_month": "2026-01",
            "end_month": "2026-04",
            "exchange_rate": RATE,
            "seed": "solver-test",
        },
        "income": {
            "base_income_usd": 100000,
            "income_variability_pct": 10,
            "cost_pct": 70,
            "cost_variability_pct": 5,
            "cash_sales_pct": 85,
        },
        "movements": {"purchase_base_usd": 100000},
    }


MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04"]


class ConstraintSolverTest(unittest.TestCase):
    """F2-T1: el solver resuelve metas sin Flask ni LLM, con steps verificados."""

    def setUp(self):
        clear_model_cache()
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
        self.solver = ConstraintSolver(host=AgentCommandService(self.session))

    def tearDown(self):
        self.session.close()
        clear_model_cache()

    def _apply_steps(self, payload, steps):
        working = payload
        for step in sorted(steps, key=lambda s: int(s.get("step_order") or 0)):
            working = self.solver.apply_step(step, working, plan_id="test", user_message="test")
        return working

    def test_target_constraint_produces_verified_steps(self):
        payload = solver_payload()
        outcome = self.solver.solve(payload, [
            Constraint(kind="target", account="inventory", month="2026-02",
                       amount=150000, currency="USD", counter_account="suppliers"),
        ])

        self.assertTrue(outcome.feasible, outcome.infeasible_reason)
        self.assertEqual(len(outcome.steps), 1)

        working = self._apply_steps(payload, outcome.steps)
        result = build_financial_model(working)
        self.assertAlmostEqual(
            _statement_value(result, "Inventarios", "2026-02"),
            150000 * RATE,
            delta=1.0,
        )

    def test_multiple_target_constraints_in_one_solve(self):
        payload = solver_payload()
        outcome = self.solver.solve(payload, [
            Constraint(kind="target", account="inventory", month="2026-02",
                       amount=150000, currency="USD", counter_account="suppliers"),
            Constraint(kind="target", account="accounts_receivable", month="2026-03",
                       amount=20000, currency="USD", counter_account="inventory"),
        ])

        self.assertTrue(outcome.feasible, outcome.infeasible_reason)
        self.assertEqual(outcome.kind, "multi_account_target_balance")
        self.assertEqual(len(outcome.steps), 2)

        working = self._apply_steps(payload, outcome.steps)
        result = build_financial_model(working)
        self.assertAlmostEqual(
            _statement_value(result, "Cuentas por Cobrar Clientes", "2026-03"),
            20000 * RATE,
            delta=1.0,
        )

    def test_average_constraint_hits_the_average(self):
        payload = solver_payload()
        outcome = self.solver.solve(payload, [
            Constraint(kind="average", account="inventory", months=MONTHS,
                       amount=150000, currency="USD", counter_account="suppliers",
                       variability_pct=10),
        ])

        self.assertTrue(outcome.feasible, outcome.infeasible_reason)

        working = self._apply_steps(payload, outcome.steps)
        result = build_financial_model(working)
        values = [_statement_value(result, "Inventarios", month) for month in MONTHS]
        self.assertAlmostEqual(sum(values) / len(values), 150000 * RATE, delta=len(MONTHS))
        # Con variabilidad, los meses no son todos iguales.
        self.assertGreater(max(values) - min(values), 1.0)

    def test_floor_constraint_keeps_account_above_floor(self):
        payload = solver_payload()
        outcome = self.solver.solve(payload, [
            Constraint(kind="floor", account="cash", amount=0,
                       counter_account="loans_personal", currency="NIO"),
        ])

        self.assertTrue(outcome.feasible, outcome.infeasible_reason)
        self.assertFalse(outcome.no_plan)

        working = self._apply_steps(payload, outcome.steps)
        result = build_financial_model(working)
        for month in MONTHS:
            self.assertGreaterEqual(
                _statement_value(result, "Efectivo y Equivalentes de Efectivo", month),
                -1.0,
                f"caja negativa en {month}",
            )

    def test_floor_constraint_returns_no_plan_when_already_met(self):
        # El inventario por defecto (5.3M NIO) nunca baja del piso 0.
        outcome = self.solver.solve(solver_payload(), [
            Constraint(kind="floor", account="inventory", amount=0,
                       counter_account="suppliers", currency="NIO"),
        ])

        self.assertTrue(outcome.feasible)
        self.assertTrue(outcome.no_plan)

    def test_infeasible_constraint_reports_reason(self):
        outcome = self.solver.solve(solver_payload(), [
            Constraint(kind="target", account="inventory", month="2030-01",
                       amount=1000, currency="USD", counter_account="suppliers"),
        ])

        self.assertFalse(outcome.feasible)
        self.assertIn("no esta dentro del periodo", outcome.infeasible_reason)

    def test_impossible_average_reports_the_three_numbers(self):
        # F2-T2: promedio 50k USD en 4 meses (total 200k) con un mes fijado
        # en 500k obliga a los meses libres a cerrar en -100k.
        outcome = self.solver.solve(solver_payload(), [
            Constraint(kind="average", account="inventory", months=MONTHS,
                       amount=50000, currency="USD", counter_account="suppliers",
                       overrides={"2026-02": 500000}),
        ])

        self.assertFalse(outcome.feasible)
        reason = outcome.infeasible_reason
        self.assertIn("50,000.00", reason)    # promedio pedido
        self.assertIn("500,000.00", reason)   # monto fijado
        self.assertIn("-100,000.00", reason)  # saldo negativo requerido
        self.assertIn("Inventarios", reason)

    def test_utility_too_far_reports_current_target_and_limit(self):
        outcome = self.solver.solve(solver_payload(), [
            Constraint(kind="utility", amount=1_000_000, lever="cogs"),
        ])

        self.assertFalse(outcome.feasible)
        reason = outcome.infeasible_reason
        self.assertIn("USD 1,000,000.00", reason)   # meta pedida
        self.assertIn("utilidad actual", reason)    # valor actual con cifra
        self.assertIn("limite automatico", reason)  # restriccion que lo bloquea

    def test_safety_warning_is_quantified_in_assistant_message(self):
        # Llevar cuentas por cobrar a 400k USD contra inventario (5.3M NIO)
        # deja el inventario muy negativo: el plan es factible pero el
        # mensaje debe advertirlo con el monto.
        outcome = self.solver.solve(solver_payload(), [
            Constraint(kind="target", account="accounts_receivable", month="2026-02",
                       amount=400000, currency="USD", counter_account="inventory"),
        ])

        self.assertTrue(outcome.feasible, outcome.infeasible_reason)
        self.assertIn("Inventarios", outcome.assistant_message)
        self.assertIn("quedaria en C$", outcome.assistant_message)
        warnings = outcome.aggregate_impact.get("safety_warnings") or []
        self.assertTrue(any(w["account"] == "inventory" for w in warnings))

    def test_compound_cash_average_and_inventory_target(self):
        # F2-T3 (caso de aceptacion del plan): "caja promedio 5,000 USD y que
        # inventario cierre en 100,000 USD en un mes" en UNA sola resolucion.
        payload = solver_payload()
        outcome = self.solver.solve(payload, [
            Constraint(kind="average", account="cash", months=MONTHS,
                       amount=5000, currency="USD", counter_account="capital"),
            Constraint(kind="target", account="inventory", month="2026-02",
                       amount=100000, currency="USD", counter_account="suppliers"),
        ])

        self.assertTrue(outcome.feasible, outcome.infeasible_reason)
        self.assertEqual(outcome.kind, "compound_constraints")
        self.assertGreaterEqual(len(outcome.steps), 5)  # 4 meses de caja + 1 inventario

        working = self._apply_steps(payload, outcome.steps)
        result = build_financial_model(working)
        cash_values = [
            _statement_value(result, "Efectivo y Equivalentes de Efectivo", month)
            for month in MONTHS
        ]
        self.assertAlmostEqual(sum(cash_values) / len(MONTHS), 5000 * RATE, delta=len(MONTHS))
        self.assertAlmostEqual(
            _statement_value(result, "Inventarios", "2026-02"),
            100000 * RATE,
            delta=1.0,
        )

    def test_compound_conflict_reports_the_pair(self):
        # El target de cuentas por cobrar usa inventario como contrapartida,
        # rompiendo el promedio de inventario pedido antes: el solver debe
        # reportar el par en conflicto con cifras.
        outcome = self.solver.solve(solver_payload(), [
            Constraint(kind="average", account="inventory", months=MONTHS,
                       amount=150000, currency="USD", counter_account="suppliers"),
            Constraint(kind="target", account="accounts_receivable", month="2026-03",
                       amount=100000, currency="USD", counter_account="inventory"),
        ])

        self.assertFalse(outcome.feasible)
        reason = outcome.infeasible_reason
        self.assertIn("Conflicto entre objetivos", reason)
        self.assertIn("Cuentas por Cobrar Clientes", reason)
        self.assertIn("promedio de Inventarios", reason)
        self.assertIn("C$", reason)

    def test_distribute_average_respects_overrides_and_average(self):
        targets = distribute_average(MONTHS, 100000, {"2026-02": 130000}, 0.0)

        by_month = {item["month"]: item["target_amount"] for item in targets}
        self.assertEqual(by_month["2026-02"], 130000)
        self.assertAlmostEqual(sum(by_month.values()) / len(MONTHS), 100000, delta=0.01)


if __name__ == "__main__":
    unittest.main()

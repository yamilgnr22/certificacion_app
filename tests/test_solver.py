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

    def test_distribute_average_respects_overrides_and_average(self):
        targets = distribute_average(MONTHS, 100000, {"2026-02": 130000}, 0.0)

        by_month = {item["month"]: item["target_amount"] for item in targets}
        self.assertEqual(by_month["2026-02"], 130000)
        self.assertAlmostEqual(sum(by_month.values()) / len(MONTHS), 100000, delta=0.01)


if __name__ == "__main__":
    unittest.main()

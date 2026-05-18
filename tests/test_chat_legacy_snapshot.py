from __future__ import annotations

import copy
import os
import unittest

from chat_controller import handle_chat_command
from model_chat import preview_chat_adjustment


def sample_payload():
    return {
        "client": {"nombre_completo": "Cliente Prueba", "cedula": "001-010101-0000A", "banco": "BAC"},
        "period": {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "chat-controller-test",
        },
        "income": {
            "base_income_usd": 100000,
            "income_variability_pct": 10,
            "cost_pct": 70,
            "cost_variability_pct": 5,
            "cash_sales_pct": 85,
        },
        "expenses": {
            "Sueldos y Salarios": 2700,
            "Servicios Publicos": 600,
            "Alcaldia y DGI": 50,
            "Combustible": 500,
            "Publicidad": 1500,
            "Renta": 440,
            "Otros Gastos": 350,
        },
        "balances": {
            "cash": 410193,
            "accounts_receivable": 62261,
            "inventory": 5310538,
            "ppe_equipment": 549366,
            "credit_cards": 183122,
            "loans_personal": 47612,
            "retained_earnings": 3424750,
        },
        "movements": {
            "purchase_base_usd": 120000,
            "purchase_variability_pct": 10,
            "events": [],
            "journal_entries": [],
        },
    }


SCOPE = {"mode": "global", "selected_block": {"start_month": "2026-01", "end_month": "2026-04"}}


PREVIEW_SNAPSHOT_CASES = {
    "ajusta compras para que caja final sea 500000": {"ok": False, "kind": None, "clarify": False, "not_viable": True, "events": 0, "journals": 0},
    "ajusta compras para que caja final sea 1000000": {"ok": False, "kind": None, "clarify": False, "not_viable": True, "events": 0, "journals": 0},
    "con proveedores deja caja final en 800000": {"ok": False, "kind": None, "clarify": False, "not_viable": True, "events": 0, "journals": 0},
    "con prestamo deja caja final en 900000": {"ok": True, "kind": None, "clarify": False, "not_viable": False, "events": 1, "journals": 0},
    "con aporte de capital deja caja final en 900000": {"ok": True, "kind": None, "clarify": False, "not_viable": False, "events": 1, "journals": 0},
    "ajusta caja a 800k mensuales en promedio con compras y variacion de 20%": {"ok": True, "kind": None, "clarify": False, "not_viable": False, "events": 16, "journals": 0},
    "ajusta caja mensual alrededor de 800k con proveedores +/-20%": {"ok": False, "kind": None, "clarify": True, "not_viable": False, "events": 0, "journals": 0},
    "cambia el costo de venta a 80% +/- 5% para todo el modelo": {"ok": True, "kind": "assumption_change", "clarify": False, "not_viable": False, "events": 0, "journals": 0, "cost_pct": 80.0},
    "aplica costo de venta 75% con variabilidad 4%": {"ok": True, "kind": "assumption_change", "clarify": False, "not_viable": False, "events": 0, "journals": 0, "cost_pct": 75.0},
    "cambia ingresos base a 120000 dolares": {"ok": False, "kind": None, "clarify": True, "not_viable": False, "events": 0, "journals": 0},
    "cambia compras promedio a 100000 dolares": {"ok": True, "kind": None, "clarify": False, "not_viable": False, "events": 16, "journals": 0},
    "retira de banco 1000000 y restalo en capital en abril 2026": {"ok": True, "kind": None, "clarify": False, "not_viable": False, "events": 1, "journals": 0},
    "retira de banco 500000 y restalo de resultados acumulados en marzo 2026": {"ok": True, "kind": None, "clarify": False, "not_viable": False, "events": 1, "journals": 0},
    "reclasifica 1000000 de capital a resultados acumulados en enero 2026": {"ok": True, "kind": "journal_entry", "clarify": False, "not_viable": False, "events": 0, "journals": 1},
    "traslada resultados del ejercicio 2025 a resultados acumulados en enero 2026": {"ok": True, "kind": "journal_entry", "clarify": False, "not_viable": False, "events": 0, "journals": 1},
    "haz una partida debitando capital y acreditando resultados acumulados por 500000 en enero 2026": {"ok": True, "kind": "journal_entry", "clarify": False, "not_viable": False, "events": 0, "journals": 1},
    "registra vehiculo por 567677 con credito prendario por 454141 en mayo 2025": {"ok": True, "kind": "compound_events", "clarify": False, "not_viable": False, "events": 2, "journals": 0},
    "compra equipo por 200000 en febrero 2026": {"ok": False, "kind": None, "clarify": False, "not_viable": True, "events": 0, "journals": 0},
    "nuevo credito prendario por 454141 en mayo 2025": {"ok": True, "kind": None, "clarify": False, "not_viable": False, "events": 1, "journals": 0},
    "abono credito prendario por 50000 en junio 2025": {"ok": True, "kind": None, "clarify": False, "not_viable": False, "events": 1, "journals": 0},
    "deshacer ultimo ajuste": {"ok": False, "kind": None, "clarify": False, "not_viable": True, "events": 0, "journals": 0},
    "reemplaza el ajuste anterior por caja final 700000": {"ok": False, "kind": None, "clarify": True, "not_viable": False, "events": 0, "journals": 0},
    "revierte el comprobante CD-2026-0008": {"ok": False, "kind": None, "clarify": False, "not_viable": True, "events": 0, "journals": 0},
    "explicame resultados acumulados en enero 2026": {"ok": False, "kind": None, "clarify": True, "not_viable": False, "events": 0, "journals": 0},
    "muestrame el mayor de efectivo": {"ok": False, "kind": None, "clarify": True, "not_viable": False, "events": 0, "journals": 0},
}


COMMAND_SNAPSHOT_CASES = {
    "guarda este borrador": {"ok": True, "response_type": "workflow", "intent": "save_draft"},
    "explicame de donde sale resultados acumulados en enero 2026": {"ok": True, "response_type": "answer", "intent": "explain_balance"},
    "muestrame el mayor de efectivo": {"ok": True, "response_type": "ui_action", "intent": "show_account_ledger"},
    "llevame al libro diario": {"ok": False, "response_type": "clarification", "intent": "clarification_needed"},
    "cambia el costo de venta a 80% +/- 5% para todo el modelo": {"ok": True, "response_type": "proposal", "intent": "assumption_change"},
}


class ChatLegacySnapshotTest(unittest.TestCase):
    def setUp(self):
        self._old_openai_key = os.environ.pop("OPENAI_API_KEY", None)

    def tearDown(self):
        if self._old_openai_key is not None:
            os.environ["OPENAI_API_KEY"] = self._old_openai_key

    def test_preview_legacy_cases_are_stable(self):
        self.assertGreaterEqual(len(PREVIEW_SNAPSHOT_CASES), 25)
        for message, expected in PREVIEW_SNAPSHOT_CASES.items():
            with self.subTest(message=message):
                data = preview_chat_adjustment(copy.deepcopy(sample_payload()), message, scope=SCOPE)
                actual = {
                    "ok": bool(data.get("ok")),
                    "kind": (data.get("proposal") or {}).get("kind"),
                    "clarify": bool(data.get("needs_clarification")),
                    "not_viable": bool(data.get("not_viable")),
                    "events": len(data.get("new_events") or []),
                    "journals": len(data.get("new_journal_entries") or []),
                }
                if "cost_pct" in expected:
                    actual["cost_pct"] = (data.get("adjusted_payload") or {}).get("income", {}).get("cost_pct")
                self.assertEqual(actual, expected, message)

    def test_command_legacy_cases_are_stable(self):
        for message, expected in COMMAND_SNAPSHOT_CASES.items():
            with self.subTest(message=message):
                data = handle_chat_command(copy.deepcopy(sample_payload()), message, ui_context={"scope": SCOPE}, scope=SCOPE)
                actual = {
                    "ok": bool(data.get("ok")),
                    "response_type": data.get("response_type"),
                    "intent": data.get("intent"),
                }
                self.assertEqual(actual, expected, message)


if __name__ == "__main__":
    unittest.main()

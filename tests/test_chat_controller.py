from __future__ import annotations

import unittest

from chat_controller import handle_chat_command
from financial_model import build_financial_model


def sample_payload():
    return {
        "client": {
            "nombre_completo": "Cliente Prueba",
            "cedula": "001-010101-0000A",
            "banco": "BAC",
        },
        "period": {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "chat-controller-test",
        },
        "income": {
            "base_income_usd": 100_000,
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
            "cash": 410_193,
            "accounts_receivable": 62_261,
            "inventory": 5_310_538,
            "ppe_equipment": 549_366,
            "credit_cards": 183_122,
            "loans_personal": 47_612,
            "retained_earnings": 3_424_750,
        },
        "movements": {
            "purchase_base_usd": 120_000,
            "purchase_variability_pct": 10,
            "events": [],
            "journal_entries": [],
        },
    }


class ChatControllerTest(unittest.TestCase):
    def test_save_draft_is_confirmable_workflow(self):
        data = handle_chat_command(sample_payload(), "guarda este borrador")

        self.assertTrue(data["ok"])
        self.assertEqual(data["response_type"], "workflow")
        self.assertEqual(data["workflow"]["action"], "save_draft")
        self.assertTrue(data["requires_confirmation"])

    def test_explain_balance_returns_trace_answer_and_ui_action(self):
        data = handle_chat_command(
            sample_payload(),
            "explicame de donde sale resultados acumulados en enero 2026",
        )

        self.assertTrue(data["ok"])
        self.assertEqual(data["response_type"], "answer")
        self.assertEqual(data["intent"], "explain_balance")
        self.assertIn("saldo inicial", data["assistant_message"])
        self.assertTrue(any(action["type"] == "select_account" for action in data["ui_actions"]))

    def test_assumption_change_uses_existing_financial_solver(self):
        data = handle_chat_command(
            sample_payload(),
            "cambia el costo de venta a 80% +/- 5% para todo el modelo",
            ui_context={"scope": {"mode": "global"}},
        )

        self.assertTrue(data["ok"])
        self.assertEqual(data["response_type"], "proposal")
        self.assertEqual(data["proposal"]["kind"], "assumption_change")
        self.assertEqual(data["adjusted_payload"]["income"]["cost_pct"], 80)
        self.assertIn("chat", data["adjusted_payload"])

    def test_reverse_chat_voucher_creates_confirmable_reversal(self):
        payload = sample_payload()
        payload["movements"]["journal_entries"] = [
            {
                "month": "2026-01",
                "debit_account": "current_earnings",
                "credit_account": "retained_earnings",
                "amount": 1000,
                "currency": "nio",
                "source": "chat_financiero",
                "instruction_id": "chat_original",
                "message": "cierre de prueba",
            }
        ]
        result = build_financial_model(payload)
        voucher_id = next(v["voucher_id"] for v in result.accounting["vouchers"] if v.get("source") == "chat_financiero")

        data = handle_chat_command(payload, f"revierte el comprobante {voucher_id}")

        self.assertTrue(data["ok"])
        self.assertEqual(data["intent"], "reverse_voucher")
        self.assertEqual(data["proposal"]["kind"], "voucher_reversal")
        self.assertIn("Registro contable propuesto", data["assistant_message"])
        reversal = data["new_journal_entries"][0]
        self.assertEqual(reversal["debit_account"], "retained_earnings")
        self.assertEqual(reversal["credit_account"], "current_earnings")
        self.assertEqual(reversal["reference_voucher_id"], voucher_id)

    def test_bare_voucher_reference_shows_voucher_instead_of_cash_error(self):
        payload = sample_payload()
        payload["movements"]["journal_entries"] = [
            {
                "month": "2026-01",
                "debit_account": "current_earnings",
                "credit_account": "retained_earnings",
                "amount": 1000,
                "currency": "nio",
                "source": "chat_financiero",
                "instruction_id": "chat_original",
                "message": "cierre de prueba",
            }
        ]
        result = build_financial_model(payload)
        voucher_id = next(v["voucher_id"] for v in result.accounting["vouchers"] if v.get("source") == "chat_financiero")

        data = handle_chat_command(payload, f"{voucher_id} de enero 2026")

        self.assertTrue(data["ok"])
        self.assertEqual(data["intent"], "show_voucher")
        self.assertIn(voucher_id, data["assistant_message"])

    def test_context_voucher_can_be_reversed_as_this_voucher(self):
        payload = sample_payload()
        payload["movements"]["journal_entries"] = [
            {
                "month": "2026-01",
                "debit_account": "current_earnings",
                "credit_account": "retained_earnings",
                "amount": 1000,
                "currency": "nio",
                "source": "chat_financiero",
                "instruction_id": "chat_original",
                "message": "cierre de prueba",
            }
        ]
        result = build_financial_model(payload)
        voucher_id = next(v["voucher_id"] for v in result.accounting["vouchers"] if v.get("source") == "chat_financiero")

        data = handle_chat_command(payload, "revierte este comprobante", ui_context={"selected_voucher": voucher_id})

        self.assertTrue(data["ok"])
        self.assertEqual(data["intent"], "reverse_voucher")
        self.assertEqual(data["new_journal_entries"][0]["reference_voucher_id"], voucher_id)


if __name__ == "__main__":
    unittest.main()

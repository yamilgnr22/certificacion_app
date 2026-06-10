from __future__ import annotations

import unittest

from financial_model import build_financial_model
from invariants import validate_er_vs_esf, validate_ledger_vs_esf
from tests.test_financial_model import sample_payload


class LedgerEsfInvariantTest(unittest.TestCase):
    """F1-T3: el saldo del mayor debe coincidir con el ESF, cuenta por mes."""

    def test_sane_model_reconciles(self):
        result = build_financial_model(sample_payload())

        self.assertTrue(
            result.validations["ledger_esf"]["ok"],
            result.validations["ledger_esf"]["errors"],
        )

    def test_model_with_journal_entries_and_asset_sale_reconciles(self):
        payload = sample_payload()
        payload["movements"]["events"].append(
            {"month": "2026-03", "account": "asset_vehicle_sale", "amount": 100000, "currency": "nio"}
        )
        payload["movements"]["journal_entries"] = [
            {
                "month": "2026-02",
                "debit_account": "inventory",
                "credit_account": "suppliers",
                "amount_nio": 75000,
                "description": "Compra financiada via asiento",
            }
        ]

        result = build_financial_model(payload)

        self.assertTrue(
            result.validations["ledger_esf"]["ok"],
            result.validations["ledger_esf"]["errors"],
        )

    def test_saved_voucher_without_state_effect_is_detected(self):
        # Un voucher guardado en accounting.vouchers entra al mayor pero NO
        # al estado del modelo: el ESF no se mueve. Eso es exactamente la
        # divergencia entre motores que este invariante debe detectar.
        payload = sample_payload()
        payload["accounting"] = {
            "vouchers": [
                {
                    "voucher_id": "MAN-0001",
                    "month": "2026-02",
                    "date": "2026-02-01",
                    "type": "chat_adjustment",
                    "source": "manual",
                    "description": "Voucher manual sin efecto en estado",
                    "status": "applied",
                    "lines": [
                        {"account": "Efectivo y Equivalentes de Efectivo", "debit": 10000, "credit": 0, "currency": "nio", "reference": ""},
                        {"account": "Capital", "debit": 0, "credit": 10000, "currency": "nio", "reference": ""},
                    ],
                }
            ]
        }

        result = build_financial_model(payload)
        ledger_esf = result.validations["ledger_esf"]

        self.assertFalse(ledger_esf["ok"])
        cash_errors = [
            error for error in ledger_esf["errors"]
            if error["account"] == "Efectivo y Equivalentes de Efectivo" and error["month"] == "2026-02"
        ]
        self.assertEqual(len(cash_errors), 1)
        self.assertAlmostEqual(cash_errors[0]["difference"], 10000, delta=2)

    def test_empty_esf_reports_not_ok(self):
        import pandas as pd

        outcome = validate_ledger_vs_esf({}, pd.DataFrame(), ["2026-01"])

        self.assertFalse(outcome["ok"])


class ErEsfInvariantTest(unittest.TestCase):
    """F1-T5: la utilidad del ER debe fluir a Resultados del Ejercicio."""

    def test_sane_model_reconciles(self):
        result = build_financial_model(sample_payload())

        self.assertTrue(
            result.validations["er_esf"]["ok"],
            result.validations["er_esf"]["errors"],
        )

    def test_journal_entry_against_current_earnings_is_considered(self):
        payload = sample_payload()
        payload["movements"]["journal_entries"] = [
            {
                "month": "2026-02",
                "debit_account": "current_earnings",
                "credit_account": "retained_earnings",
                "amount_nio": 120000,
                "description": "Traslado parcial de resultados",
            }
        ]

        result = build_financial_model(payload)

        self.assertTrue(
            result.validations["er_esf"]["ok"],
            result.validations["er_esf"]["errors"],
        )

    def test_corrupted_result_accum_is_detected(self):
        # Simula un ESF cuyo Resultados del Ejercicio quedo en cero todos
        # los meses pese a que el ER reporta utilidad.
        model = build_financial_model(sample_payload())
        corrupted = [
            {
                "month": month,
                "result_accum": 0.0,
                "result_accum_journal_increase": 0.0,
                "result_accum_journal_decrease": 0.0,
            }
            for month in model.df_er_full.columns[2:-2]
        ]

        outcome = validate_er_vs_esf(model.df_er_full, corrupted)

        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["errors"])
        first = outcome["errors"][0]
        self.assertTrue(abs(first["difference"]) > 1.0)


if __name__ == "__main__":
    unittest.main()

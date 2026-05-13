from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from document_generator import generar_documento_completo
from financial_model import build_financial_model


def sample_payload():
    return {
        "client": {
            "nombre_completo": "Cliente Prueba",
            "cedula": "001-010101-0000A",
            "estado_civil": "casada",
            "profesion": "Comerciante",
            "sexo": "Femenino",
            "domicilio": "Managua",
            "direccion_negocio": "Managua",
            "banco": "BAC",
            "fecha_certificacion": "2026-05-02",
        },
        "period": {
            "end_month": "2026-04",
            "months": 6,
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        },
        "income": {
            "base_income_usd": 100000,
            "income_variability_pct": 10,
            "cost_pct": 70,
            "cost_variability_pct": 5,
            "cash_sales_pct": 85,
        },
        "movements": {
            "purchase_base_usd": 110000,
            "purchase_variability_pct": 10,
            "events": [
                {"month": "2026-02", "account": "owner_withdrawal", "amount": 250000, "currency": "nio"},
                {"month": "2025-12", "account": "asset_vehicle", "amount": 12000, "currency": "usd"},
            ],
        },
    }


class FinancialModelTest(unittest.TestCase):
    def test_model_is_reproducible_and_valid(self):
        one = build_financial_model(sample_payload())
        two = build_financial_model(sample_payload())

        self.assertTrue(one.validations["er"]["ok"])
        self.assertTrue(one.validations["esf"]["ok"])
        self.assertTrue(one.validations["balance"]["ok"])
        self.assertEqual(one.summary, two.summary)
        self.assertTrue(one.df_er.equals(two.df_er))
        self.assertTrue(one.df_esf_mensual.equals(two.df_esf_mensual))

    def test_randomized_rates_stay_inside_configured_ranges(self):
        result = build_financial_model(sample_payload())
        revenue_factors = result.metadata["revenue_factors"]
        cost_rates = result.metadata["cost_rates"]

        self.assertTrue(all(0.90 <= factor <= 1.10 for factor in revenue_factors))
        self.assertTrue(all(0.65 <= rate <= 0.75 for rate in cost_rates))

    def test_docx_can_be_generated_without_excel(self):
        result = build_financial_model(sample_payload())
        with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
            out_path = Path(tmp.name)
        try:
            generar_documento_completo(
                result.df_esf_mensual,
                result.df_er,
                result.df_datos,
                result.df_certificacion,
                str(out_path),
                incluir_validacion=False,
                esf_tipo="mensual",
            )
            self.assertTrue(out_path.exists())
            self.assertGreater(out_path.stat().st_size, 0)
        finally:
            out_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

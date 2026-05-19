from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document

from document_generator import generar_documento_completo
from financial_model import build_financial_model, result_to_json
from model_chat import heuristic_interpret_cash_instruction, solve_cash_target


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


def month_column(df, month):
    for col in df.columns:
        text = col.strftime("%Y-%m") if hasattr(col, "strftime") else str(col)[:7]
        if text == month:
            return col
    raise AssertionError(f"Mes no encontrado: {month}")


def month_columns(df):
    out = []
    for col in df.columns:
        text = col.strftime("%Y-%m") if hasattr(col, "strftime") else str(col)
        if len(text) >= 7 and text[:7].count("-") == 1 and text[:7].replace("-", "").isdigit():
            out.append(text[:7])
    return out


def esf_value(result, label, month):
    col = month_column(result.df_esf_mensual, month)
    row = result.df_esf_mensual[result.df_esf_mensual["Descripcion"] == label].iloc[0]
    return round(float(row[col]))


def full_esf_value(result, label, month):
    col = month_column(result.df_esf_mensual_full, month)
    row = result.df_esf_mensual_full[result.df_esf_mensual_full["Descripcion"] == label].iloc[0]
    return round(float(row[col]))


def account_movement_value(result, account, movement, month):
    col = month_column(result.df_movimiento_cuentas, month)
    rows = result.df_movimiento_cuentas[
        (result.df_movimiento_cuentas["Cuenta"] == account)
        & (result.df_movimiento_cuentas["Movimiento"] == movement)
    ]
    return round(float(rows.iloc[0][col]))


def _docx_text(path: Path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


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

    def test_accounting_layer_generates_balanced_opening_and_monthly_vouchers(self):
        result = build_financial_model(sample_payload())
        accounting = result.accounting

        self.assertGreater(accounting["summary"]["voucher_count"], 0)
        opening = accounting["vouchers"][0]
        self.assertEqual(opening["type"], "opening")
        self.assertTrue(opening["balanced"])
        self.assertTrue(all(voucher["balanced"] for voucher in accounting["vouchers"]))
        self.assertTrue(any(voucher["type"] == "sales" for voucher in accounting["vouchers"]))
        self.assertTrue(any(voucher["type"] == "cogs" for voucher in accounting["vouchers"]))
        self.assertTrue(any(voucher["type"] == "expenses" for voucher in accounting["vouchers"]))
        self.assertIn("Resultados Acumulados", accounting["accounts"])

    def test_accounting_trace_explains_retained_earnings_year_close(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }
        payload["movements"]["journal_entries"] = [
            {
                "month": "2026-01",
                "debit_account": "current_earnings",
                "credit_account": "retained_earnings",
                "amount": 6_023_510,
                "currency": "nio",
                "entry_type": "year_close_transfer",
                "source": "chat_financiero",
                "instruction_id": "chat_cierre",
                "message": "cierre 2025",
            }
        ]

        result = build_financial_model(payload)
        trace = result.accounting["trace"]["Resultados Acumulados|2026-01"]

        self.assertEqual(trace["opening_balance"], 3_424_750)
        self.assertEqual(trace["credits"], 6_023_510)
        self.assertEqual(trace["closing_balance"], 9_448_260)
        self.assertTrue(any(entry["voucher_id"] for entry in trace["entries"]))
        self.assertTrue(any(v["source"] == "chat_financiero" for v in result.accounting["vouchers"]))

    def test_result_json_exposes_accounting_preview(self):
        result = build_financial_model(sample_payload())
        data = result_to_json(result)

        self.assertIn("accounting", data)
        self.assertGreater(data["accounting"]["summary"]["voucher_count"], 0)
        self.assertIn("ledger", data["accounting"])
        self.assertIn("trace", data["accounting"])

    def test_randomized_rates_stay_inside_configured_ranges(self):
        result = build_financial_model(sample_payload())
        revenue_factors = result.metadata["revenue_factors"]
        cost_rates = result.metadata["cost_rates"]

        self.assertTrue(all(0.90 <= factor <= 1.10 for factor in revenue_factors))
        self.assertTrue(all(0.65 <= rate <= 0.75 for rate in cost_rates))

    def test_cash_flow_preview_reconciles_to_esf_cash(self):
        result = build_financial_model(sample_payload())
        cash_flow_final = result.df_flujo_caja[
            result.df_flujo_caja["Concepto"] == "Saldo final de caja"
        ].iloc[0]
        esf_cash = result.df_esf_mensual[
            result.df_esf_mensual["Descripcion"] == "Efectivo y Equivalentes de Efectivo"
        ].iloc[0]

        for col in result.df_esf_mensual.columns[1:]:
            self.assertEqual(cash_flow_final[col], esf_cash[col])

    def test_account_movement_reconciles_each_account(self):
        result = build_financial_model(sample_payload())
        df = result.df_movimiento_cuentas
        months = list(df.columns[2:])

        for account in df["Cuenta"].unique():
            block = df[df["Cuenta"] == account].set_index("Movimiento")
            for month in months:
                computed = (
                    block.loc["Saldo inicial", month]
                    + block.loc["Aumentos", month]
                    + block.loc["Disminuciones", month]
                )
                self.assertAlmostEqual(computed, block.loc["Saldo final", month], delta=1.0, msg=f"{account} {month}")

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

    def test_dynamic_equity_account_flows_to_esf_ledger_and_docx(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2026-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }
        payload["accounting"] = {
            "dynamic_accounts": [
                {
                    "code": "reservas_legales",
                    "name": "Reservas Legales",
                    "account_type": "patrimonio",
                    "section": "patrimonio",
                }
            ]
        }
        payload["movements"]["journal_entries"] = [
            {
                "month": "2026-01",
                "debit_account": "capital",
                "credit_account": "Reservas Legales",
                "amount": 250000,
                "currency": "nio",
            }
        ]
        result = build_financial_model(payload)

        self.assertIn("Reservas Legales", set(result.df_esf_mensual_full["Descripcion"]))
        self.assertIn("Reservas Legales", result.accounting["accounts"])
        self.assertTrue(result.validations["balance"]["ok"])

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
            text = _docx_text(out_path)
            self.assertIn("Reservas Legales", text)
        finally:
            out_path.unlink(missing_ok=True)

    def test_start_end_six_months_uses_single_block(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2025-11",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }

        result = build_financial_model(payload)

        self.assertEqual(result.summary["months"], ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04"])
        self.assertEqual(len(result.statement_blocks), 1)
        self.assertEqual(result.statement_blocks[0]["meta"]["id"], "full_range")
        self.assertEqual(result.statement_blocks[0]["meta"]["label"], "Noviembre 2025-Abril 2026")
        self.assertEqual(month_columns(result.df_er), result.summary["months"])

    def test_more_than_twelve_months_splits_by_calendar_year(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }

        result = build_financial_model(payload)
        metas = [block["meta"] for block in result.statement_blocks]

        self.assertEqual([meta["id"] for meta in metas], ["year_2026", "year_2025"])
        self.assertEqual(metas[0]["label"], "Enero-Abril 2026")
        self.assertEqual(metas[1]["label"], "Enero-Diciembre 2025")
        self.assertEqual(result.summary["months"], ["2026-01", "2026-02", "2026-03", "2026-04"])
        self.assertEqual(result.metadata["full_summary"]["months"][0], "2025-01")
        self.assertEqual(result.metadata["full_summary"]["months"][-1], "2026-04")
        self.assertEqual(len(result.metadata["full_summary"]["months"]), 16)
        self.assertEqual(month_columns(result.df_er), result.summary["months"])
        self.assertTrue(result.validations["balance"]["ok"])

    def test_twenty_eight_months_splits_into_three_blocks(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2024-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }

        result = build_financial_model(payload)

        self.assertEqual([block["meta"]["id"] for block in result.statement_blocks], ["year_2026", "year_2025", "year_2024"])
        self.assertEqual(len(result.metadata["full_summary"]["months"]), 28)

    def test_chat_scope_can_target_selected_block_or_year(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2024-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }
        current = build_financial_model(payload)

        solved_block = solve_cash_target(
            payload,
            {
                "intent": "target_cash_balance",
                "target_cash": 0,
                "lever": "loan_commercial_new",
            },
            scope={"mode": "block", "months": ["2026-01", "2026-02", "2026-03", "2026-04"]},
        )
        self.assertTrue(solved_block["ok"])
        self.assertEqual(solved_block["proposal"]["target_month"], "2026-04")
        self.assertEqual(solved_block["new_events"][0]["month"], "2026-04")

        solved_year = solve_cash_target(
            payload,
            {
                "intent": "target_cash_balance",
                "target_cash": 0,
                "lever": "loan_commercial_new",
            },
            scope={"mode": "year", "year": 2024},
        )
        self.assertTrue(solved_year["ok"])
        self.assertEqual(solved_year["proposal"]["target_month"], "2024-12")
        self.assertEqual(solved_year["new_events"][0]["month"], "2024-12")

    def test_result_json_exposes_block_previews(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }

        data = result_to_json(build_financial_model(payload))

        self.assertEqual([block["id"] for block in data["period_blocks"]], ["year_2026", "year_2025"])
        self.assertIn("full_summary", data)
        self.assertIn("year_2026", data["preview"]["blocks"])
        self.assertIn("year_2025", data["preview"]["blocks"])

    def test_docx_can_be_generated_with_statement_blocks(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }
        result = build_financial_model(payload)
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
                statement_blocks=result.statement_blocks,
            )
            self.assertTrue(out_path.exists())
            self.assertGreater(out_path.stat().st_size, 0)
        finally:
            out_path.unlink(missing_ok=True)

    def test_chat_parser_maps_purchase_cash_target(self):
        result = build_financial_model(sample_payload())
        action = heuristic_interpret_cash_instruction(
            "ajusta compras para caja final 1 mm",
            result.summary["months"],
        )

        self.assertEqual(action["intent"], "target_cash_balance")
        self.assertEqual(action["target_month"], result.summary["months"][-1])
        self.assertEqual(action["target_cash"], 1_000_000)
        self.assertEqual(action["lever"], "purchase_adjustment")

    def test_chat_parser_maps_average_cash_target(self):
        result = build_financial_model(sample_payload())
        action = heuristic_interpret_cash_instruction(
            "ajusta las compras para que los saldos de caja de todos los meses oscilen alrededor de los 800k",
            result.summary["months"],
        )

        self.assertEqual(action["intent"], "target_cash_series")
        self.assertIsNone(action["target_month"])
        self.assertEqual(action["target_cash"], 800_000)
        self.assertEqual(action["cash_variability_pct"], 20)
        self.assertEqual(action["lever"], "purchase_adjustment")

    def test_chat_parser_maps_cost_assumption_change(self):
        result = build_financial_model(sample_payload())
        action = heuristic_interpret_cash_instruction(
            "cambia el costo de venta a 80% con variabilidad de +/-5%",
            result.summary["months"],
        )

        self.assertEqual(action["intent"], "assumption_change")
        self.assertEqual(action["assumption"], "cost_pct")
        self.assertEqual(action["value"], 80)
        self.assertEqual(action["cash_variability_pct"], 5)

    def test_chat_changes_global_cost_assumption(self):
        payload = sample_payload()
        result = build_financial_model(payload)
        solved = solve_cash_target(
            payload,
            {
                "intent": "assumption_change",
                "assumption": "cost_pct",
                "value": 80,
                "cash_variability_pct": 5,
            },
            scope={"mode": "global"},
        )

        self.assertTrue(solved["ok"])
        self.assertEqual(solved["proposal"]["kind"], "assumption_change")
        self.assertEqual(solved["adjusted_payload"]["income"]["cost_pct"], 80)
        self.assertEqual(solved["adjusted_payload"]["income"]["cost_variability_pct"], 5)
        adjusted = solved["_adjusted_result"]
        self.assertTrue(all(0.75 <= rate <= 0.85 for rate in adjusted.metadata["cost_rates"]))
        self.assertTrue(adjusted.validations["balance"]["ok"])
        self.assertNotEqual(result.summary["net_income_total"], adjusted.summary["net_income_total"])

    def test_chat_changes_block_cost_assumption_with_monthly_overrides(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }
        solved = solve_cash_target(
            payload,
            {
                "intent": "assumption_change",
                "assumption": "cost_pct",
                "value": 80,
                "cash_variability_pct": 5,
            },
            scope={"mode": "block", "months": ["2026-01", "2026-02", "2026-03", "2026-04"]},
        )

        self.assertTrue(solved["ok"])
        adjusted_payload = solved["adjusted_payload"]
        self.assertEqual(adjusted_payload["income"]["cost_pct"], 70)
        overrides = {item["month"]: item for item in adjusted_payload["income"]["monthly_overrides"]}
        self.assertEqual(set(overrides), {"2026-01", "2026-02", "2026-03", "2026-04"})
        adjusted = solved["_adjusted_result"]
        months = adjusted.metadata["full_summary"]["months"]
        rates_by_month = dict(zip(months, adjusted.metadata["cost_rates"]))
        self.assertTrue(all(0.75 <= rates_by_month[m] <= 0.85 for m in overrides))
        self.assertTrue(all(0.65 <= rates_by_month[m] <= 0.75 for m in months if m.startswith("2025-")))
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_purchase_adjustment_solver_hits_cash_target(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        current_cash = esf_value(current, "Efectivo y Equivalentes de Efectivo", month)
        target_cash = max(0, current_cash + 100_000)
        difference = target_cash - current_cash

        solved = solve_cash_target(
            payload,
            {
                "intent": "target_cash_balance",
                "target_month": month,
                "target_cash": target_cash,
                "lever": "purchase_adjustment",
            },
        )

        self.assertTrue(solved["ok"])
        self.assertEqual(solved["proposal"]["event"]["account"], "purchase_adjustment")
        self.assertEqual(solved["proposal"]["event"]["amount"], -difference)
        adjusted = solved["_adjusted_result"]
        self.assertEqual(esf_value(adjusted, "Efectivo y Equivalentes de Efectivo", month), target_cash)
        self.assertEqual(solved["proposal"]["impact"]["inventory"], -difference)
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_supplier_financing_improves_cash_without_reducing_inventory(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        current_cash = esf_value(current, "Efectivo y Equivalentes de Efectivo", month)
        current_inventory = esf_value(current, "Inventarios", month)
        current_suppliers = esf_value(current, "Proveedores", month)
        target_cash = max(0, current_cash + 100_000)
        difference = target_cash - current_cash

        solved = solve_cash_target(
            payload,
            {
                "intent": "target_cash_balance",
                "target_month": month,
                "target_cash": target_cash,
                "lever": "supplier_financing",
            },
        )

        self.assertTrue(solved["ok"])
        adjusted = solved["_adjusted_result"]
        self.assertEqual(esf_value(adjusted, "Efectivo y Equivalentes de Efectivo", month), target_cash)
        self.assertEqual(esf_value(adjusted, "Inventarios", month), current_inventory)
        self.assertEqual(esf_value(adjusted, "Proveedores", month), current_suppliers + difference)
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_loan_solver_increases_liabilities_and_cash(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        current_cash = esf_value(current, "Efectivo y Equivalentes de Efectivo", month)
        current_liabilities = esf_value(current, "Total Pasivos", month)
        target_cash = max(0, current_cash + 100_000)
        difference = target_cash - current_cash

        solved = solve_cash_target(
            payload,
            {
                "intent": "target_cash_balance",
                "target_month": month,
                "target_cash": target_cash,
                "lever": "loan_commercial_new",
            },
        )

        self.assertTrue(solved["ok"])
        adjusted = solved["_adjusted_result"]
        self.assertEqual(esf_value(adjusted, "Efectivo y Equivalentes de Efectivo", month), target_cash)
        self.assertEqual(esf_value(adjusted, "Total Pasivos", month), current_liabilities + difference)
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_purchase_adjustment_rejects_negative_purchases(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        current_cash = esf_value(current, "Efectivo y Equivalentes de Efectivo", month)
        current_purchases = account_movement_value(current, "Inventarios", "Aumentos", month)

        solved = solve_cash_target(
            payload,
            {
                "intent": "target_cash_balance",
                "target_month": month,
                "target_cash": current_cash + current_purchases + 10,
                "lever": "purchase_adjustment",
            },
        )

        self.assertFalse(solved["ok"])
        self.assertTrue(solved["not_viable"])

    def test_purchase_adjustment_series_targets_each_month_cash(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        months = current.summary["months"]

        solved = solve_cash_target(
            payload,
            {
                "intent": "target_cash_series",
                "target_cash": 800_000,
                "cash_variability_pct": 20,
                "lever": "purchase_adjustment",
            },
        )

        self.assertTrue(solved["ok"])
        self.assertEqual(solved["proposal"]["events_count"], len(months))
        self.assertGreater(solved["proposal"]["purchase_average_nio"], 0)
        self.assertEqual(solved["proposal"]["cash_variability_pct"], 20)
        self.assertGreater(solved["proposal"]["target_max_cash"], 800_000)
        self.assertLess(solved["proposal"]["target_min_cash"], 800_000)
        adjusted = solved["_adjusted_result"]
        adjusted_cash_values = []
        for month in months:
            cash = esf_value(adjusted, "Efectivo y Equivalentes de Efectivo", month)
            adjusted_cash_values.append(cash)
            self.assertGreaterEqual(cash, 640_000)
            self.assertLessEqual(cash, 960_000)
        self.assertGreater(len(set(adjusted_cash_values)), 1)
        self.assertAlmostEqual(sum(adjusted_cash_values) / len(adjusted_cash_values), 800_000, delta=2.0)
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_chat_parser_maps_equity_cash_adjustment(self):
        result = build_financial_model(sample_payload())
        action = heuristic_interpret_cash_instruction(
            "saca de banco 1 millon y restalo en resultados acumulados",
            result.summary["months"],
        )

        self.assertEqual(action["intent"], "equity_cash_adjustment")
        self.assertEqual(action["target_month"], result.summary["months"][-1])
        self.assertEqual(action["amount"], 1_000_000)
        self.assertEqual(action["lever"], "retained_earnings_distribution")

    def test_owner_withdrawal_lowers_cash_and_capital(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        cash_before = esf_value(current, "Efectivo y Equivalentes de Efectivo", month)
        capital_before = esf_value(current, "Capital", month)

        solved = solve_cash_target(
            payload,
            {
                "intent": "equity_cash_adjustment",
                "target_month": month,
                "amount": 100_000,
                "lever": "owner_withdrawal",
                "instruction_id": "chat_retiro_capital",
                "message": "saca 100k y restalo en capital",
            },
        )

        self.assertTrue(solved["ok"])
        adjusted = solved["_adjusted_result"]
        self.assertEqual(esf_value(adjusted, "Efectivo y Equivalentes de Efectivo", month), cash_before - 100_000)
        self.assertEqual(esf_value(adjusted, "Capital", month), capital_before - 100_000)
        self.assertEqual(solved["new_events"][0]["source"], "chat_financiero")
        self.assertTrue(solved["new_events"][0]["locked"])
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_retained_earnings_distribution_lowers_cash_and_retained_earnings(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        cash_before = esf_value(current, "Efectivo y Equivalentes de Efectivo", month)
        retained_before = esf_value(current, "Resultados Acumulados", month)
        capital_before = esf_value(current, "Capital", month)

        solved = solve_cash_target(
            payload,
            {
                "intent": "equity_cash_adjustment",
                "target_month": month,
                "amount": 100_000,
                "lever": "retained_earnings_distribution",
                "instruction_id": "chat_retiro_resultados",
            },
        )

        self.assertTrue(solved["ok"])
        adjusted = solved["_adjusted_result"]
        self.assertEqual(esf_value(adjusted, "Efectivo y Equivalentes de Efectivo", month), cash_before - 100_000)
        self.assertEqual(esf_value(adjusted, "Resultados Acumulados", month), retained_before - 100_000)
        self.assertEqual(esf_value(adjusted, "Capital", month), capital_before)
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_capital_reclassification_moves_equity_without_cash(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        cash_before = esf_value(current, "Efectivo y Equivalentes de Efectivo", month)
        capital_before = esf_value(current, "Capital", month)
        retained_before = esf_value(current, "Resultados Acumulados", month)

        solved = solve_cash_target(
            payload,
            {
                "intent": "equity_cash_adjustment",
                "target_month": month,
                "amount": 100_000,
                "lever": "capital_reclassification",
                "instruction_id": "chat_reclasifica",
            },
        )

        self.assertTrue(solved["ok"])
        adjusted = solved["_adjusted_result"]
        self.assertEqual(esf_value(adjusted, "Efectivo y Equivalentes de Efectivo", month), cash_before)
        self.assertEqual(esf_value(adjusted, "Capital", month), capital_before - 100_000)
        self.assertEqual(esf_value(adjusted, "Resultados Acumulados", month), retained_before + 100_000)
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_chat_parser_maps_year_close_to_prior_december(self):
        months = [f"2025-{month:02d}" for month in range(1, 13)] + [f"2026-{month:02d}" for month in range(1, 5)]

        action = heuristic_interpret_cash_instruction(
            "traslada el saldo de resultados del ejercicio del ano 2025 a resultados acumulados en enero 2026",
            months,
        )

        self.assertEqual(action["intent"], "year_close_transfer")
        self.assertEqual(action["source_month"], "2025-12")
        self.assertEqual(action["target_month"], "2026-01")
        self.assertEqual(action["debit_account"], "current_earnings")
        self.assertEqual(action["credit_account"], "retained_earnings")

    def test_year_close_transfer_moves_current_earnings_to_retained(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }
        current = build_financial_model(payload)
        amount = full_esf_value(current, "Resultados del Ejercicio", "2025-12")
        retained_before = full_esf_value(current, "Resultados Acumulados", "2026-01")
        current_earnings_before = full_esf_value(current, "Resultados del Ejercicio", "2026-01")
        capital_before = full_esf_value(current, "Capital", "2026-01")

        solved = solve_cash_target(
            payload,
            {
                "intent": "year_close_transfer",
                "target_month": "2026-01",
                "source_month": "2025-12",
                "debit_account": "current_earnings",
                "credit_account": "retained_earnings",
            },
            scope={"mode": "global"},
        )

        self.assertTrue(solved["ok"])
        self.assertEqual(solved["proposal"]["amount"], amount)
        self.assertEqual(len(solved["adjusted_payload"]["movements"]["journal_entries"]), 1)
        adjusted = solved["_adjusted_result"]
        self.assertEqual(full_esf_value(adjusted, "Resultados Acumulados", "2026-01"), retained_before + amount)
        self.assertEqual(full_esf_value(adjusted, "Resultados del Ejercicio", "2026-01"), current_earnings_before - amount)
        self.assertEqual(full_esf_value(adjusted, "Capital", "2026-01"), capital_before)
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_controlled_journal_entry_reclassifies_equity(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        capital_before = esf_value(current, "Capital", month)
        retained_before = esf_value(current, "Resultados Acumulados", month)

        solved = solve_cash_target(
            payload,
            {
                "intent": "journal_entry",
                "target_month": month,
                "amount": 100_000,
                "debit_account": "capital",
                "credit_account": "retained_earnings",
            },
        )

        self.assertTrue(solved["ok"])
        adjusted = solved["_adjusted_result"]
        self.assertEqual(esf_value(adjusted, "Capital", month), capital_before - 100_000)
        self.assertEqual(esf_value(adjusted, "Resultados Acumulados", month), retained_before + 100_000)
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_chat_parser_maps_financed_vehicle_purchase(self):
        payload = sample_payload()
        payload["period"] = {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "modelo-prueba",
        }
        current = build_financial_model(payload)
        months = current.metadata["full_summary"]["months"]
        cash_before = full_esf_value(current, "Efectivo y Equivalentes de Efectivo", "2025-05")
        vehicle_before = full_esf_value(current, "Vehiculos", "2025-05")
        pledge_before = full_esf_value(current, "Creditos Prendarios", "2025-05")

        action = heuristic_interpret_cash_instruction(
            "Registra la incorporacion de vehiculo por 567677. "
            "Aumento en vehiculo, disminucion en efectivo por 113,536. "
            "Ademas para cuadrar la partida registra un pasivo Prendario por 454,141. "
            "Registra la partida en mayo 2025",
            months,
        )
        solved = solve_cash_target(payload, action, scope={"mode": "global"})

        self.assertEqual(action["intent"], "compound_events")
        self.assertTrue(solved["ok"])
        adjusted = solved["_adjusted_result"]
        self.assertEqual(full_esf_value(adjusted, "Vehiculos", "2025-05"), vehicle_before + 567_677)
        self.assertEqual(full_esf_value(adjusted, "Efectivo y Equivalentes de Efectivo", "2025-05"), cash_before - 113_536)
        self.assertEqual(full_esf_value(adjusted, "Creditos Prendarios", "2025-05"), pledge_before + 454_141)
        self.assertTrue(adjusted.validations["balance"]["ok"])

    def test_chat_adjustments_append_sequentially_without_rewriting_previous_events(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        cash_before = esf_value(current, "Efectivo y Equivalentes de Efectivo", month)

        first = solve_cash_target(
            payload,
            {
                "intent": "target_cash_balance",
                "target_month": month,
                "target_cash": max(0, cash_before),
                "lever": "capital_contribution",
                "instruction_id": "chat_uno",
            },
        )
        self.assertTrue(first["ok"])
        first_payload = first["adjusted_payload"]

        second = solve_cash_target(
            first_payload,
            {
                "intent": "equity_cash_adjustment",
                "target_month": month,
                "amount": 50_000,
                "lever": "owner_withdrawal",
                "instruction_id": "chat_dos",
            },
        )

        self.assertTrue(second["ok"])
        events = second["adjusted_payload"]["movements"]["events"]
        instruction_ids = {event.get("instruction_id") for event in events if event.get("source") == "chat_financiero"}
        self.assertIn("chat_uno", instruction_ids)
        self.assertIn("chat_dos", instruction_ids)
        self.assertEqual(second["new_events"][0]["instruction_id"], "chat_dos")
        self.assertTrue(second["_adjusted_result"].validations["balance"]["ok"])

    def test_undo_last_adjustment_removes_only_last_chat_instruction(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        cash_before = esf_value(current, "Efectivo y Equivalentes de Efectivo", month)

        first = solve_cash_target(
            payload,
            {
                "intent": "target_cash_balance",
                "target_month": month,
                "target_cash": max(0, cash_before),
                "lever": "capital_contribution",
                "instruction_id": "chat_uno",
            },
        )
        second = solve_cash_target(
            first["adjusted_payload"],
            {
                "intent": "equity_cash_adjustment",
                "target_month": month,
                "amount": 50_000,
                "lever": "owner_withdrawal",
                "instruction_id": "chat_dos",
            },
        )

        undo = solve_cash_target(second["adjusted_payload"], {"intent": "undo_last_adjustment"})

        self.assertTrue(undo["ok"])
        remaining_ids = {
            event.get("instruction_id")
            for event in undo["adjusted_payload"]["movements"]["events"]
            if event.get("source") == "chat_financiero"
        }
        removed_ids = {event.get("instruction_id") for event in undo["removed_events"]}
        self.assertIn("chat_uno", remaining_ids)
        self.assertNotIn("chat_dos", remaining_ids)
        self.assertEqual(removed_ids, {"chat_dos"})
        self.assertTrue(undo["_adjusted_result"].validations["balance"]["ok"])

    def test_replace_adjustment_only_when_explicit(self):
        payload = sample_payload()
        current = build_financial_model(payload)
        month = current.summary["months"][-1]
        cash_before = esf_value(current, "Efectivo y Equivalentes de Efectivo", month)

        first = solve_cash_target(
            payload,
            {
                "intent": "target_cash_balance",
                "target_month": month,
                "target_cash": max(0, cash_before),
                "lever": "capital_contribution",
                "instruction_id": "chat_uno",
            },
        )
        second = solve_cash_target(
            first["adjusted_payload"],
            {
                "intent": "target_cash_balance",
                "target_month": month,
                "target_cash": 100_000,
                "lever": "capital_contribution",
                "instruction_id": "chat_dos",
            },
        )
        self.assertTrue(second["ok"])
        normal_events = [
            event for event in second["adjusted_payload"]["movements"]["events"]
            if event.get("source") == "chat_financiero" and event.get("account") == "capital_contribution"
        ]
        self.assertEqual(len(normal_events), 2)

        replaced = solve_cash_target(
            second["adjusted_payload"],
            {
                "intent": "target_cash_balance",
                "target_month": month,
                "target_cash": 200_000,
                "lever": "capital_contribution",
                "replace_existing": True,
                "instruction_id": "chat_tres",
            },
        )

        self.assertTrue(replaced["ok"])
        self.assertEqual(len(replaced["replaced_events"]), 2)
        replaced_events = [
            event for event in replaced["adjusted_payload"]["movements"]["events"]
            if event.get("source") == "chat_financiero" and event.get("account") == "capital_contribution"
        ]
        self.assertEqual(len(replaced_events), 1)
        self.assertEqual(replaced_events[0]["instruction_id"], "chat_tres")
        self.assertTrue(replaced["_adjusted_result"].validations["balance"]["ok"])


if __name__ == "__main__":
    unittest.main()

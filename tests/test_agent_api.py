from __future__ import annotations

import json
import os
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import web_server
from datetime import datetime, timedelta, timezone

import services.agent_service as agent_service_module
from services.agent_tools import AgentTool
from db.models import AccountCatalog, AgentMessage, AgentProposal, AgentSessionContext, AuditLog, Base, PeriodoCertificacion
from db.seed import seed_giros
from financial_model import build_financial_model
from llm import LLMProviderError


def cliente_payload(**overrides):
    payload = {
        "nombre_completo": "Cliente Agente",
        "cedula": "001-020202-0000A",
        "nombre_negocio": "Negocio Agente",
        "direccion_negocio": "Managua",
        "giro_negocio_id": "ferreteria",
    }
    payload.update(overrides)
    return payload


def periodo_payload(**overrides):
    payload = {
        "mes_inicial": "2026-01",
        "mes_final": "2026-04",
        "tasa_cambio": 36.6243,
        "ingresos_base_usd": 108000,
        "variabilidad_ingresos_pct": 10,
        "cost_pct": 70,
        "variabilidad_costos_pct": 5,
        "cash_sales_pct": 85,
        "seed": "agent-test",
        "balances_override": {
            "cash": 410193,
            "accounts_receivable": 62261,
            "inventory": 5310538,
            "ppe_equipment": 549366,
            "credit_cards": 183122,
            "loans_personal": 47612,
            "retained_earnings": 3424750,
        },
    }
    payload.update(overrides)
    return payload


class FakeProvider:
    name = "fake"

    def __init__(self, response):
        self.response = response

    def complete_json(self, *, system_prompt, user_prompt, schema=None):
        return self.response


class AgentApiTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        with self.factory() as session:
            seed_giros(session)
            session.commit()
        self.old_engine = web_server.app.config.get("DB_ENGINE")
        self.old_require = web_server.app.config.get("DB_REQUIRE_ALEMBIC")
        self.old_provider = web_server.app.config.get("AGENT_LLM_PROVIDER")
        web_server.app.config["DB_ENGINE"] = self.engine
        web_server.app.config["DB_REQUIRE_ALEMBIC"] = False
        self.client = web_server.app.test_client()
        cliente = self.client.post("/api/clientes", json=cliente_payload()).get_json()["cliente"]
        self.periodo = self.client.post(
            f"/api/clientes/{cliente['id']}/periodos",
            json=periodo_payload(),
        ).get_json()["periodo"]

    def tearDown(self):
        for key, old_value in [
            ("DB_ENGINE", self.old_engine),
            ("DB_REQUIRE_ALEMBIC", self.old_require),
            ("AGENT_LLM_PROVIDER", self.old_provider),
        ]:
            if old_value is None:
                web_server.app.config.pop(key, None)
            else:
                web_server.app.config[key] = old_value

    def db_session(self):
        return self.factory()

    def _payload(self):
        with self.db_session() as session:
            periodo = session.get(PeriodoCertificacion, self.periodo["id"])
            return json.loads(periodo.payload_json)

    def _save_payload(self, payload):
        with self.db_session() as session:
            periodo = session.get(PeriodoCertificacion, self.periodo["id"])
            periodo.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            session.commit()

    def _esf_value(self, payload, description, month):
        result = build_financial_model(payload)
        rows = result.df_esf_mensual_full[result.df_esf_mensual_full["Descripcion"] == description]
        col = month if month in result.df_esf_mensual_full.columns else next(
            item for item in result.df_esf_mensual_full.columns if str(item).startswith(month)
        )
        return float(rows.iloc[0][col])

    def _add_saved_voucher(self, voucher_id="MAN-2026-0001", *, voucher_type="manual", reference_voucher_id="", amount=1000):
        payload = self._payload()
        accounting = dict(payload.get("accounting") or {})
        vouchers = list(accounting.get("vouchers") or [])
        voucher = {
            "voucher_id": voucher_id,
            "month": "2026-01",
            "date": "2026-01-15",
            "type": voucher_type,
            "source": "manual",
            "description": f"Comprobante {voucher_id}",
            "status": "applied",
            "reference_voucher_id": reference_voucher_id,
            "debit_total": amount,
            "credit_total": amount,
            "balanced": True,
            "lines": [
                {"account": "Proveedores", "debit": amount, "credit": 0, "currency": "nio", "reference": "F-1"},
                {"account": "Efectivo y Equivalentes de Efectivo", "debit": 0, "credit": amount, "currency": "nio", "reference": "CK-1"},
            ],
        }
        vouchers.append(voucher)
        accounting["vouchers"] = vouchers
        payload["accounting"] = accounting
        self._save_payload(payload)
        return voucher

    def test_missing_openai_key_returns_clear_error(self):
        old_key = os.environ.pop("OPENAI_API_KEY", None)
        web_server.app.config.pop("AGENT_LLM_PROVIDER", None)
        try:
            resp = self.client.post(
                "/api/agent/command",
                json={"periodo_id": self.periodo["id"], "message": "explicame efectivo"},
            )
        finally:
            if old_key is not None:
                os.environ["OPENAI_API_KEY"] = old_key

        data = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(data["ok"])
        self.assertIn("OpenAI", data["assistant_message"])

    def test_explain_balance_returns_answer_and_persists_message(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "explain_balance", "args": {"account": "Resultados Acumulados", "month": "2026-01"}}
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "explicame resultados acumulados en enero"},
        )
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["response_type"], "answer")
        self.assertEqual(data["intent"], "explain_balance")
        self.assertIn("saldo inicial", data["assistant_message"])
        self.assertIn("tool_versions", data["audit"])
        with self.db_session() as session:
            messages = list(session.scalars(select(AgentMessage)))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].intent, "explain_balance")

    def test_show_voucher_returns_voucher_lines(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "show_voucher", "args": {"voucher_id": "CD-2026-0001"}}
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "muestrame CD-2026-0001"},
        )
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertIn("CD-2026-0001", data["assistant_message"])
        self.assertTrue(any(action["type"] == "select_voucher" for action in data["ui_actions"]))

    def test_navigation_command_returns_ui_action(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "navigate", "args": {"target": "ledger"}}
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "llevame al libro diario"},
        )
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        self.assertEqual(data["response_type"], "navigation")
        self.assertEqual(data["ui_actions"][0]["target"], "ledger")

    def test_show_ledger_returns_structured_rows(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "show_ledger", "args": {"account": "Efectivo", "start_month": "2026-01", "end_month": "2026-02"}}
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "muestrame el mayor de efectivo"},
        )
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        self.assertEqual(data["response_type"], "answer")
        self.assertEqual(data["data"]["kind"], "ledger")
        self.assertEqual(data["data"]["account"], "cash")
        self.assertTrue(data["data"]["rows"])
        self.assertIn("tool_versions_used", data)
        self.assertEqual(data["tool_versions_used"], {"show_ledger": "1.1.0"})

    def test_dirty_payload_is_used_without_persisting(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "explain_balance", "args": {"account": "Efectivo", "month": "2026-01"}}
        )
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]
        dirty_payload = detail["payload"]
        dirty_payload["balances"]["cash"] = 999999

        resp = self.client.post(
            "/api/agent/command",
            json={
                "periodo_id": self.periodo["id"],
                "message": "explicame caja",
                "current_payload": dirty_payload,
                "is_dirty": True,
            },
        )
        data = resp.get_json()
        persisted = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]["payload"]

        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["used_dirty_payload"])
        self.assertIn("cambios sin guardar", data["assistant_message"])
        self.assertTrue(any(entry["debit"] == 999999 for entry in data["data"]["entries"]))
        self.assertEqual(persisted["balances"]["cash"], 410193)
        with self.db_session() as session:
            message = session.scalars(select(AgentMessage)).first()
            stored_response = json.loads(message.response_json)
        self.assertTrue(stored_response["used_dirty_payload"])
        self.assertIn("duration_ms", stored_response)

    def test_tool_registry_rejects_tool_without_version(self):
        with self.assertRaises(ValueError):
            AgentTool("bad_tool", "", False, {}, {})

    def test_turn_timeout_is_recorded(self):
        old_timeout = agent_service_module.MAX_TURN_DURATION_S
        agent_service_module.MAX_TURN_DURATION_S = -1
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "explain_balance", "args": {"account": "Efectivo", "month": "2026-01"}}
        )
        try:
            resp = self.client.post(
                "/api/agent/command",
                json={"periodo_id": self.periodo["id"], "message": "explicame caja"},
            )
        finally:
            agent_service_module.MAX_TURN_DURATION_S = old_timeout
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        self.assertFalse(data["ok"])
        self.assertEqual(data["intent"], "timeout")
        self.assertEqual(data["response_type"], "error")
        with self.db_session() as session:
            message = session.scalars(select(AgentMessage)).first()
            stored_response = json.loads(message.response_json)
        self.assertEqual(stored_response["intent"], "timeout")

    def test_provider_failure_is_error_case(self):
        class BrokenProvider:
            name = "broken"

            def complete_json(self, *, system_prompt, user_prompt, schema=None):
                raise LLMProviderError("Proveedor no configurado")

        web_server.app.config["AGENT_LLM_PROVIDER"] = BrokenProvider()

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "explicame efectivo"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Proveedor no configurado", resp.get_json()["error"])

    def test_assumption_change_proposal_applies_with_hash_guard(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "assumption_change", "args": {"assumption": "cost_pct", "value": 80, "cost_variability_pct": 5, "scope": "global"}}
        )
        proposal_resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "cambia costo a 80 +/- 5"},
        )
        proposal = proposal_resp.get_json()["proposal"]

        apply_resp = self.client.post(f"/api/agent/proposals/{proposal['id']}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(apply_resp.status_code, 200, apply_resp.get_json())
        self.assertEqual(detail["payload"]["income"]["cost_pct"], 80.0)
        with self.db_session() as session:
            stored = session.get(AgentProposal, proposal["id"])
        self.assertEqual(stored.status, "applied")

    def test_assumption_change_supports_base_income_and_preserves_manual_entries(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "description": "Partida previa",
                    "lines": [
                        {"account": "Capital", "debit": 1000, "credit": 0},
                        {"account": "Resultados Acumulados", "debit": 0, "credit": 1000},
                    ],
                },
            }
        )
        journal_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "partida previa"},
        ).get_json()["proposal"]["id"]
        self.client.post(f"/api/agent/proposals/{journal_id}/apply")
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "assumption_change", "args": {"field": "ingresos_base_usd", "new_value": 120000}}
        )

        proposal = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "cambia ingresos base"},
        ).get_json()["proposal"]
        apply_resp = self.client.post(f"/api/agent/proposals/{proposal['id']}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(apply_resp.status_code, 200, apply_resp.get_json())
        self.assertEqual(detail["payload"]["income"]["base_income_usd"], 120000.0)
        self.assertEqual(detail["payload"]["movements"]["journal_entries"][-1]["description"], "Partida previa")
        self.assertEqual(proposal["kind"], "assumption_change_proposal")
        self.assertIn("assumption_impact", proposal)

    def test_monthly_override_proposal_applies_and_recalculates(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "monthly_override",
                "args": {
                    "updates": [
                        {"month": "2026-02", "revenue_usd": 123000, "cogs_usd": 81000, "note": "ventas reales"}
                    ]
                },
            }
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "fija febrero con ingresos y costo reales"},
        )
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        proposal = data["proposal"]
        self.assertEqual(proposal["kind"], "monthly_override_proposal")
        self.assertEqual(proposal["override_rows"][0]["after_revenue_usd"], 123000.0)
        apply_resp = self.client.post(f"/api/agent/proposals/{proposal['id']}/apply")
        self.assertEqual(apply_resp.status_code, 200, apply_resp.get_json())
        payload_after = self._payload()
        overrides = {item["month"]: item for item in payload_after["income"]["monthly_overrides"]}
        self.assertEqual(overrides["2026-02"]["revenue_usd"], 123000.0)
        self.assertEqual(overrides["2026-02"]["cogs_usd"], 81000.0)
        result = build_financial_model(payload_after)
        self.assertEqual(result.summary["exact_revenue_months"], ["2026-02"])
        self.assertEqual(result.summary["exact_cogs_months"], ["2026-02"])

    def test_monthly_override_dirty_payload_does_not_create_proposal(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "monthly_override", "args": {"month": "2026-02", "revenue_usd": 123000}}
        )
        payload = self._payload()
        payload["balances"]["cash"] = 999999

        resp = self.client.post(
            "/api/agent/command",
            json={
                "periodo_id": self.periodo["id"],
                "message": "cambia febrero a ingreso exacto",
                "current_payload": payload,
                "is_dirty": True,
            },
        )
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        self.assertEqual(data["response_type"], "question")
        self.assertIn("Guarda los cambios", data["assistant_message"])
        with self.db_session() as session:
            self.assertEqual(len(list(session.scalars(select(AgentProposal)))), 0)

    def test_assumption_change_rejects_unknown_field_and_out_of_range_value(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "assumption_change", "args": {"field": "margen_magico", "new_value": 10}}
        )
        bad_field = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "cambia campo raro"},
        )
        self.assertEqual(bad_field.status_code, 400)
        self.assertIn("Supuesto no permitido", bad_field.get_json()["error"])

        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "assumption_change", "args": {"field": "cash_sales_pct", "new_value": 120}}
        )
        bad_value = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "contado 120"},
        )
        self.assertEqual(bad_value.status_code, 400)
        self.assertIn("porcentaje", bad_value.get_json()["error"])

    def test_proposal_does_not_apply_to_finalized_periodo(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "assumption_change", "args": {"assumption": "cost_pct", "value": 80}}
        )
        proposal_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "cambia costo"},
        ).get_json()["proposal"]["id"]
        finalize_resp = self.client.post(f"/api/periodos/{self.periodo['id']}/finalizar")
        self.assertEqual(finalize_resp.status_code, 200, finalize_resp.get_json())

        resp = self.client.post(f"/api/agent/proposals/{proposal_id}/apply")

        self.assertEqual(resp.status_code, 409)
        self.assertIn("borrador", resp.get_json()["error"])
        with self.db_session() as session:
            self.assertEqual(session.get(AgentProposal, proposal_id).status, "pending")

    def test_stale_proposal_does_not_apply(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "assumption_change", "args": {"assumption": "cost_pct", "value": 80}}
        )
        proposal_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "cambia costo"},
        ).get_json()["proposal"]["id"]
        # Cambiar el payload antes de aplicar invalida el hash.
        self.client.put(f"/api/periodos/{self.periodo['id']}", json={"cost_pct": 72})

        resp = self.client.post(f"/api/agent/proposals/{proposal_id}/apply")

        self.assertEqual(resp.status_code, 409)
        self.assertIn("modelo cambio", resp.get_json()["error"])
        with self.db_session() as session:
            self.assertEqual(session.get(AgentProposal, proposal_id).status, "stale")

    def test_expired_proposal_does_not_apply(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "assumption_change", "args": {"assumption": "cost_pct", "value": 80}}
        )
        proposal_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "cambia costo"},
        ).get_json()["proposal"]["id"]
        with self.db_session() as session:
            stored = session.get(AgentProposal, proposal_id)
            stored.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            session.commit()

        resp = self.client.post(f"/api/agent/proposals/{proposal_id}/apply")

        self.assertEqual(resp.status_code, 409)
        self.assertIn("expiro", resp.get_json()["error"])
        with self.db_session() as session:
            self.assertEqual(session.get(AgentProposal, proposal_id).status, "expired")

    def test_new_proposal_same_command_supersedes_previous_pending(self):
        command_id = "cmd_same_test"
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "assumption_change", "args": {"field": "cost_pct", "new_value": 75}}
        )
        first = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "cambia costo", "ui_context": {"command_id": command_id}},
        ).get_json()["proposal"]["id"]
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "assumption_change", "args": {"field": "cost_pct", "new_value": 76}}
        )
        second = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "cambia costo otra vez", "ui_context": {"command_id": command_id}},
        ).get_json()["proposal"]["id"]

        with self.db_session() as session:
            self.assertEqual(session.get(AgentProposal, first).status, "superseded")
            self.assertEqual(session.get(AgentProposal, second).status, "pending")

    def test_journal_entry_proposal_adds_entry_to_payload(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "debit_account": "Capital",
                    "credit_account": "Resultados Acumulados",
                    "amount": 500000,
                },
            }
        )
        proposal_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "partida doble"},
        ).get_json()["proposal"]["id"]

        resp = self.client.post(f"/api/agent/proposals/{proposal_id}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(resp.status_code, 200, resp.get_json())
        entries = detail["payload"]["movements"]["journal_entries"]
        self.assertEqual(entries[-1]["debit_account"], "capital")
        self.assertEqual(entries[-1]["credit_account"], "retained_earnings")
        self.assertEqual(entries[-1]["amount"], 500000)

    def test_journal_entry_uses_account_catalog_alias(self):
        with self.db_session() as session:
            session.add(AccountCatalog(
                id="exp_rent",
                code="exp_rent",
                niif_code="619.05",
                name="Renta",
                account_type="gasto",
                section="gastos_operativos",
                aliases_json=json.dumps(["alquiler"]),
                display_order=6195,
                source="niif_pyme_enriched",
                is_postable=1,
                active=1,
            ))
            session.commit()
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "description": "Registro de alquiler",
                    "lines": [
                        {"account": "alquiler", "debit": 1000, "credit": 0},
                        {"account": "cash", "debit": 0, "credit": 1000},
                    ],
                },
            }
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "registra alquiler"},
        )
        entry = resp.get_json()["proposal"]["technical_records"][0]

        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(entry["lines"][0]["account"], "exp_rent")
        self.assertEqual(entry["lines"][0]["account_label"], "Renta")

    def test_journal_entry_accepts_multiple_lines_and_preserves_references(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "description": "Pago parcial a proveedor con impuesto",
                    "lines": [
                        {"account": "Proveedores", "debit": 800, "credit": 0, "reference": "F-100"},
                        {"account": "Impuestos por Pagar", "debit": 200, "credit": 0, "reference": "RET-1"},
                        {"account": "Caja", "debit": 0, "credit": 1000, "reference": "CK-1"},
                    ],
                },
            }
        )

        proposal = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "registra pago a proveedor"},
        ).get_json()["proposal"]
        resp = self.client.post(f"/api/agent/proposals/{proposal['id']}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(resp.status_code, 200, resp.get_json())
        entry = detail["payload"]["movements"]["journal_entries"][-1]
        self.assertEqual(entry["description"], "Pago parcial a proveedor con impuesto")
        self.assertEqual(len(entry["lines"]), 3)
        self.assertEqual(entry["lines"][0]["reference"], "F-100")
        result = build_financial_model(detail["payload"])
        self.assertTrue(result.validations["balance"]["ok"])

    def test_journal_entry_proposal_includes_impact(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "debit_account": "Capital",
                    "credit_account": "Resultados Acumulados",
                    "amount": 500000,
                },
            }
        )
        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "partida doble"},
        )
        proposal = resp.get_json()["proposal"]

        self.assertIn("impact", proposal)
        impact = proposal["impact"]
        self.assertEqual(impact["month"], "2026-01")
        self.assertIsInstance(impact.get("items"), list)
        keys = {item["key"] for item in impact["items"]}
        self.assertEqual(keys, {"caja", "activos", "pasivos", "patrimonio", "resultado"})
        for item in impact["items"]:
            self.assertIn("before", item)
            self.assertIn("after", item)
            self.assertIn("delta", item)
            self.assertAlmostEqual(item["after"] - item["before"], item["delta"], places=2)

    def test_invalid_journal_entry_is_rejected(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "debit_account": "Capital",
                    "credit_account": "Capital",
                    "amount": 500000,
                },
            }
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "partida mala"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("diferentes", resp.get_json()["error"])

    def test_unbalanced_journal_entry_is_rejected(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "description": "Partida descuadrada",
                    "lines": [
                        {"account": "Capital", "debit": 500000, "credit": 0},
                        {"account": "Resultados Acumulados", "debit": 0, "credit": 400000},
                    ],
                },
            }
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "partida descuadrada"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("descuadrada", resp.get_json()["error"])

    def test_unknown_account_journal_entry_is_rejected_with_suggestions(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "description": "Partida con cuenta nueva",
                    "lines": [
                        {"account": "Reservas Inventadas", "debit": 1000, "credit": 0},
                        {"account": "Capital", "debit": 0, "credit": 1000},
                    ],
                },
            }
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "partida con cuenta desconocida"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Cuentas validas", resp.get_json()["error"])

    def test_non_postable_catalog_account_is_rejected_with_child_suggestion(self):
        with self.db_session() as session:
            session.add(
                AccountCatalog(
                    id="niif_611",
                    code="niif_611",
                    niif_code="611",
                    name="Sueldos",
                    account_type="gasto",
                    section="gastos_operativos",
                    source="niif_pyme",
                    is_postable=0,
                )
            )
            session.add(
                AccountCatalog(
                    id="exp_salaries",
                    code="exp_salaries",
                    niif_code="611.01",
                    name="Sueldos y Salarios",
                    account_type="gasto",
                    section="gastos_operativos",
                    parent_code="niif_611",
                    source="niif_pyme_enriched",
                    is_postable=1,
                )
            )
            session.commit()
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "description": "Registro contra rubro",
                    "lines": [
                        {"account": "611", "debit": 1000, "credit": 0},
                        {"account": "cash", "debit": 0, "credit": 1000},
                    ],
                },
            }
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "registra sueldos contra 611"},
        )

        self.assertEqual(resp.status_code, 400)
        error = resp.get_json()["error"]
        self.assertIn("es un rubro", error)
        self.assertIn("611.01 Sueldos y Salarios", error)

    def test_journal_entry_outside_period_is_rejected(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2025-12",
                    "description": "Fuera de periodo",
                    "lines": [
                        {"account": "Capital", "debit": 1000, "credit": 0},
                        {"account": "Resultados Acumulados", "debit": 0, "credit": 1000},
                    ],
                },
            }
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "partida fuera de periodo"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("no esta dentro", resp.get_json()["error"])

    def test_dirty_payload_mutating_intent_does_not_create_proposal(self):
        payload = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]["payload"]
        payload["income"]["cost_pct"] = 71
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "journal_entry", "args": {"month": "2026-01", "description": "x", "lines": []}}
        )

        resp = self.client.post(
            "/api/agent/command",
            json={
                "periodo_id": self.periodo["id"],
                "message": "registra una partida",
                "current_payload": payload,
                "is_dirty": True,
            },
        )

        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertEqual(data["response_type"], "question")
        self.assertIn("Guarda los cambios", data["assistant_message"])
        self.assertEqual(data["ui_actions"][0]["type"], "save_and_retry")
        with self.db_session() as session:
            self.assertEqual(session.query(AgentProposal).count(), 0)
            context = session.scalars(select(AgentSessionContext)).one()
            self.assertEqual(context.pending_goal_message, "registra una partida")
            self.assertEqual(context.pending_goal_kind, "journal_entry")

    def test_save_confirmation_retries_pending_goal(self):
        payload = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]["payload"]
        payload["income"]["cost_pct"] = 71
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "target_balance_adjustment",
                "args": {"account": "inventory", "month": "2026-04", "target_amount": 190000, "currency": "USD"},
            }
        )

        dirty = self.client.post(
            "/api/agent/command",
            json={
                "periodo_id": self.periodo["id"],
                "message": "ajusta inventario a USD 190k abril",
                "current_payload": payload,
                "is_dirty": True,
            },
        )
        self.assertEqual(dirty.status_code, 200, dirty.get_json())

        retried = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "ya guarde", "is_dirty": False},
        )
        data = retried.get_json()

        self.assertEqual(retried.status_code, 200, data)
        self.assertEqual(data["response_type"], "proposal")
        self.assertEqual(data["proposal"]["kind"], "target_balance_adjustment_proposal")
        with self.db_session() as session:
            context = session.scalars(select(AgentSessionContext)).one()
            self.assertIsNone(context.pending_goal_message)

    def test_target_balance_adjustment_reaches_inventory_target_and_audits_intent(self):
        payload = self._payload()
        month = "2026-04"
        current = self._esf_value(payload, "Inventarios", month)
        rate = payload["period"]["exchange_rate"]
        target_nio = current - 100000
        target_usd = round(target_nio / rate, 2)
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "target_balance_adjustment",
                "args": {"account": "inventory", "month": month, "target_amount": target_usd, "currency": "USD"},
            }
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "ajusta inventario a objetivo"},
        )
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        proposal = data["proposal"]
        self.assertEqual(proposal["kind"], "target_balance_adjustment_proposal")
        self.assertAlmostEqual(proposal["target"]["target_amount_nio"], target_nio, delta=1)

        applied = self.client.post(f"/api/agent/proposals/{proposal['id']}/apply", json={})
        self.assertEqual(applied.status_code, 200, applied.get_json())
        payload_after = self._payload()
        final_inventory = self._esf_value(payload_after, "Inventarios", month)
        self.assertAlmostEqual(final_inventory, target_nio, delta=1)
        with self.db_session() as session:
            audit = session.scalars(select(AuditLog).where(AuditLog.action == "agent_apply_proposal")).all()[-1]
            metadata = json.loads(audit.metadata_json)
        self.assertEqual(metadata["kind"], "target_balance_adjustment")
        self.assertEqual(metadata["target_account"], "inventory")
        self.assertEqual(metadata["target_month"], month)
        self.assertIn("journal_entry_id", metadata)

    def test_target_balance_out_of_scope_fails_honestly(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "target_balance_adjustment",
                "args": {"account": "retained_earnings", "month": "2026-04", "target_amount": 100000, "currency": "USD"},
            }
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "ajusta resultados acumulados"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("solo puedo hacerlo", resp.get_json()["error"])

    def test_reverse_voucher_proposal_adds_saved_reversal(self):
        self._add_saved_voucher("MAN-2026-0001", amount=1500)
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "reverse_voucher", "args": {"voucher_id": "MAN-2026-0001"}}
        )
        proposal = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "reversa MAN-2026-0001"},
        ).get_json()["proposal"]

        self.assertEqual(proposal["kind"], "voucher_reversal")
        self.assertEqual(proposal["original_voucher_id"], "MAN-2026-0001")
        self.assertTrue(proposal["reversal_voucher_id"].startswith("REV-MAN-2026-0001-"))
        self.assertEqual(proposal["journal_rows"][0]["credit"], 1500)
        self.assertEqual(proposal["journal_rows"][1]["debit"], 1500)

        resp = self.client.post(f"/api/agent/proposals/{proposal['id']}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(resp.status_code, 200, resp.get_json())
        vouchers = detail["payload"]["accounting"]["vouchers"]
        self.assertEqual(vouchers[-1]["voucher_id"], proposal["reversal_voucher_id"])
        self.assertEqual(vouchers[-1]["reference_voucher_id"], "MAN-2026-0001")
        self.assertEqual(vouchers[-1]["type"], "reversal")
        self.assertEqual(vouchers[-1]["source"], "chat_financiero")
        self.assertEqual(vouchers[-1]["description"], "Reverso de MAN-2026-0001")
        with self.db_session() as session:
            audit = session.scalars(select(AuditLog).where(AuditLog.action == "agent_apply_proposal")).first()
            metadata = json.loads(audit.metadata_json)
        self.assertEqual(metadata["proposal_kind"], "voucher_reversal")
        self.assertEqual(metadata["original_voucher_id"], "MAN-2026-0001")
        self.assertEqual(metadata["reversal_voucher_id"], proposal["reversal_voucher_id"])
        self.assertEqual(metadata["proposal_id"], proposal["id"])

    def test_reverse_voucher_rejects_synthetic_and_missing_vouchers(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "reverse_voucher", "args": {"voucher_id": "CD-2026-0001"}}
        )
        synthetic = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "reversa CD-2026-0001"},
        )
        self.assertEqual(synthetic.status_code, 400)
        self.assertIn("generado automaticamente", synthetic.get_json()["error"])

        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "reverse_voucher", "args": {"voucher_id": "NO-EXISTE"}}
        )
        missing = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "reversa NO-EXISTE"},
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("No encontre", missing.get_json()["error"])

    def test_reverse_voucher_rejects_double_reversal_and_reversal_voucher(self):
        self._add_saved_voucher("MAN-2026-0002")
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "reverse_voucher", "args": {"voucher_id": "MAN-2026-0002"}}
        )
        proposal = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "reversa MAN-2026-0002"},
        ).get_json()["proposal"]
        self.client.post(f"/api/agent/proposals/{proposal['id']}/apply")

        repeated = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "reversa MAN-2026-0002"},
        )
        self.assertEqual(repeated.status_code, 400)
        self.assertIn("ya fue reversado", repeated.get_json()["error"])

        self._add_saved_voucher("REV-MANUAL-0001", voucher_type="reversal", reference_voucher_id="MANUAL-0001")
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "reverse_voucher", "args": {"voucher_id": "REV-MANUAL-0001"}}
        )
        reversal = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "reversa REV-MANUAL-0001"},
        )
        self.assertEqual(reversal.status_code, 400)
        self.assertIn("comprobante de reverso", reversal.get_json()["error"])

    def test_reverse_voucher_dirty_payload_does_not_create_proposal(self):
        payload = self._payload()
        payload["income"]["cost_pct"] = 71
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "reverse_voucher", "args": {"voucher_id": "MAN-2026-0001"}}
        )

        resp = self.client.post(
            "/api/agent/command",
            json={
                "periodo_id": self.periodo["id"],
                "message": "reversa un comprobante",
                "current_payload": payload,
                "is_dirty": True,
            },
        )

        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertEqual(data["response_type"], "question")
        with self.db_session() as session:
            self.assertEqual(session.query(AgentProposal).count(), 0)

    def test_reverse_voucher_expired_and_stale_proposals_are_marked(self):
        self._add_saved_voucher("MAN-2026-0004")
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "reverse_voucher", "args": {"voucher_id": "MAN-2026-0004"}}
        )
        expired_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "reversa MAN-2026-0004"},
        ).get_json()["proposal"]["id"]
        with self.db_session() as session:
            stored = session.get(AgentProposal, expired_id)
            stored.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            session.commit()
        expired = self.client.post(f"/api/agent/proposals/{expired_id}/apply")
        self.assertEqual(expired.status_code, 409)
        with self.db_session() as session:
            self.assertEqual(session.get(AgentProposal, expired_id).status, "expired")

        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "reverse_voucher", "args": {"voucher_id": "MAN-2026-0004"}}
        )
        stale_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "reversa MAN-2026-0004 otra vez"},
        ).get_json()["proposal"]["id"]
        payload = self._payload()
        payload["income"]["cost_pct"] = 72
        self._save_payload(payload)
        stale = self.client.post(f"/api/agent/proposals/{stale_id}/apply")
        self.assertEqual(stale.status_code, 409)
        with self.db_session() as session:
            self.assertEqual(session.get(AgentProposal, stale_id).status, "stale")

    def test_reverse_voucher_finalized_period_is_rejected_at_proposal(self):
        self._add_saved_voucher("MAN-2026-0005")
        self.client.post(f"/api/periodos/{self.periodo['id']}/finalizar")
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "reverse_voucher", "args": {"voucher_id": "MAN-2026-0005"}}
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "reversa MAN-2026-0005"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("periodos borrador", resp.get_json()["error"])

    def test_show_voucher_displays_bidirectional_reversal_references(self):
        self._add_saved_voucher("MAN-2026-0003")
        self._add_saved_voucher("REV-MAN-2026-0003-ABC123", voucher_type="reversal", reference_voucher_id="MAN-2026-0003")

        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "show_voucher", "args": {"voucher_id": "MAN-2026-0003"}}
        )
        original = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "muestrame MAN-2026-0003"},
        ).get_json()
        self.assertIn("Reversado por REV-MAN-2026-0003-ABC123", original["assistant_message"])
        self.assertEqual(original["data"]["voucher"]["reversed_by"], "REV-MAN-2026-0003-ABC123")

        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "show_voucher", "args": {"voucher_id": "REV-MAN-2026-0003-ABC123"}}
        )
        reversal = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "muestrame reverso"},
        ).get_json()
        self.assertIn("Reversa a MAN-2026-0003", reversal["assistant_message"])
        self.assertEqual(reversal["data"]["voucher"]["reference_voucher_id"], "MAN-2026-0003")

    def test_correct_voucher_persisted_creates_compound_proposal_and_applies(self):
        self._add_saved_voucher("MAN-CORR-0001", amount=1000)
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "correct_voucher",
                "args": {
                    "voucher_id": "MAN-CORR-0001",
                    "correction": {
                        "month": "2026-01",
                        "description": "Pago corregido a proveedor",
                        "lines": [
                            {"account": "Proveedores", "debit": 800, "credit": 0, "reference": "F-2"},
                            {"account": "Efectivo", "debit": 0, "credit": 800, "reference": "CK-2"},
                        ],
                    },
                },
            }
        )

        proposal = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "corrige el comprobante MAN-CORR-0001"},
        ).get_json()["proposal"]
        apply_resp = self.client.post(f"/api/agent/proposals/{proposal['id']}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(proposal["kind"], "compound_agent_proposal")
        self.assertEqual(proposal["compound_type"], "voucher_correction")
        self.assertEqual(proposal["original_voucher_id"], "MAN-CORR-0001")
        self.assertTrue(proposal["reversal_voucher_id"].startswith("REV-MAN-CORR-0001-"))
        self.assertEqual(proposal["user_visible_steps"][0]["kind"], "voucher_reversal")
        self.assertEqual(proposal["user_visible_steps"][1]["kind"], "journal_entry")
        self.assertEqual(proposal["correction_rows"][0]["debit"], 800)
        self.assertEqual(apply_resp.status_code, 200, apply_resp.get_json())
        vouchers = detail["payload"]["accounting"]["vouchers"]
        self.assertEqual(vouchers[-1]["reference_voucher_id"], "MAN-CORR-0001")
        entries = detail["payload"]["movements"]["journal_entries"]
        self.assertEqual(entries[-1]["entry_id"], proposal["correction_entry_id"])
        self.assertEqual(entries[-1]["description"], "Pago corregido a proveedor")
        with self.db_session() as session:
            audit = session.scalars(select(AuditLog).where(AuditLog.action == "agent_apply_compound_proposal")).first()
            metadata = json.loads(audit.metadata_json)
        self.assertEqual(metadata["proposal_kind"], "compound_agent_proposal")
        self.assertEqual(metadata["compound_type"], "voucher_correction")
        self.assertEqual(metadata["original_voucher_id"], "MAN-CORR-0001")
        self.assertEqual(metadata["reversal_voucher_id"], proposal["reversal_voucher_id"])
        self.assertEqual(metadata["correction_entry_id"], proposal["correction_entry_id"])

    def test_correct_voucher_generated_from_journal_entry_is_traceable(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "description": "Pago original mal registrado",
                    "lines": [
                        {"account": "Proveedores", "debit": 1000, "credit": 0},
                        {"account": "Efectivo", "debit": 0, "credit": 1000},
                    ],
                },
            }
        )
        original_proposal = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "registra pago original"},
        ).get_json()["proposal"]
        self.client.post(f"/api/agent/proposals/{original_proposal['id']}/apply")
        payload = self._payload()
        original_instruction = payload["movements"]["journal_entries"][-1]["instruction_id"]
        original_voucher = next(
            voucher for voucher in build_financial_model(payload).accounting["vouchers"]
            if voucher.get("instruction_id") == original_instruction
        )
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "correct_voucher",
                "args": {
                    "voucher_id": original_voucher["voucher_id"],
                    "correction": {
                        "month": "2026-01",
                        "description": "Pago original corregido",
                        "lines": [
                            {"account": "Proveedores", "debit": 600, "credit": 0},
                            {"account": "Efectivo", "debit": 0, "credit": 600},
                        ],
                    },
                },
            }
        )

        proposal = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "corrige el comprobante generado"},
        ).get_json()["proposal"]
        apply_resp = self.client.post(f"/api/agent/proposals/{proposal['id']}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(apply_resp.status_code, 200, apply_resp.get_json())
        entries = detail["payload"]["movements"]["journal_entries"]
        self.assertEqual(entries[-2]["entry_type"], "voucher_reversal")
        self.assertEqual(entries[-2]["voucher_id"], proposal["reversal_voucher_id"])
        self.assertEqual(entries[-2]["reference_voucher_id"], original_voucher["voucher_id"])
        self.assertEqual(entries[-1]["entry_id"], proposal["correction_entry_id"])
        result = build_financial_model(detail["payload"])
        self.assertTrue(result.validations["balance"]["ok"])

    def test_correct_voucher_rejects_automatic_missing_and_invalid_correction(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "correct_voucher",
                "args": {
                    "voucher_id": "CD-2026-0001",
                    "correction": {
                        "month": "2026-01",
                        "description": "Correccion",
                        "lines": [
                            {"account": "Capital", "debit": 100, "credit": 0},
                            {"account": "Resultados Acumulados", "debit": 0, "credit": 100},
                        ],
                    },
                },
            }
        )
        automatic = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "corrige CD-2026-0001"},
        )
        self.assertEqual(automatic.status_code, 400)
        self.assertIn("generado automaticamente", automatic.get_json()["error"])

        self._add_saved_voucher("MAN-CORR-0002")
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "correct_voucher", "args": {"voucher_id": "MAN-CORR-0002"}}
        )
        missing = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "corrige MAN-CORR-0002"},
        )
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.get_json()["response_type"], "question")

        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "correct_voucher",
                "args": {
                    "voucher_id": "MAN-CORR-0002",
                    "correction": {
                        "month": "2026-01",
                        "description": "Descuadrado",
                        "lines": [
                            {"account": "Capital", "debit": 100, "credit": 0},
                            {"account": "Resultados Acumulados", "debit": 0, "credit": 80},
                        ],
                    },
                },
            }
        )
        unbalanced = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "corrige descuadrado"},
        )
        self.assertEqual(unbalanced.status_code, 400)
        self.assertIn("descuadrada", unbalanced.get_json()["error"])

    def test_correct_voucher_dirty_expired_and_stale_guards(self):
        self._add_saved_voucher("MAN-CORR-0003")
        payload = self._payload()
        payload["income"]["cost_pct"] = 71
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "correct_voucher",
                "args": {
                    "voucher_id": "MAN-CORR-0003",
                    "correction": {
                        "month": "2026-01",
                        "description": "Correccion dirty",
                        "lines": [
                            {"account": "Capital", "debit": 100, "credit": 0},
                            {"account": "Resultados Acumulados", "debit": 0, "credit": 100},
                        ],
                    },
                },
            }
        )
        dirty = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "corrige", "current_payload": payload, "is_dirty": True},
        )
        self.assertEqual(dirty.status_code, 200)
        self.assertIn("correccion contable", dirty.get_json()["assistant_message"])

        clean_payload = self._payload()
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "correct_voucher",
                "args": {
                    "voucher_id": "MAN-CORR-0003",
                    "correction": {
                        "month": "2026-01",
                        "description": "Correccion expirable",
                        "lines": [
                            {"account": "Capital", "debit": 100, "credit": 0},
                            {"account": "Resultados Acumulados", "debit": 0, "credit": 100},
                        ],
                    },
                },
            }
        )
        expired_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "corrige expirable"},
        ).get_json()["proposal"]["id"]
        with self.db_session() as session:
            stored = session.get(AgentProposal, expired_id)
            stored.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            session.commit()
        expired = self.client.post(f"/api/agent/proposals/{expired_id}/apply")
        self.assertEqual(expired.status_code, 409)
        with self.db_session() as session:
            self.assertEqual(session.get(AgentProposal, expired_id).status, "expired")

        stale_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "corrige stale"},
        ).get_json()["proposal"]["id"]
        clean_payload["income"]["cost_pct"] = 72
        self._save_payload(clean_payload)
        stale = self.client.post(f"/api/agent/proposals/{stale_id}/apply")
        self.assertEqual(stale.status_code, 409)
        with self.db_session() as session:
            self.assertEqual(session.get(AgentProposal, stale_id).status, "stale")

    def test_discard_proposal_marks_discarded(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "assumption_change", "args": {"assumption": "cost_pct", "value": 80}}
        )
        proposal_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "cambia costo"},
        ).get_json()["proposal"]["id"]

        resp = self.client.post(f"/api/agent/proposals/{proposal_id}/discard")

        self.assertEqual(resp.status_code, 200)
        with self.db_session() as session:
            self.assertEqual(session.get(AgentProposal, proposal_id).status, "discarded")

    def test_create_account_requires_confirmation_and_applies_to_catalog(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "create_account",
                "args": {
                    "name": "Reservas Legales",
                    "account_type": "patrimonio",
                    "section": "patrimonio",
                },
            }
        )

        proposal_resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "crea la cuenta reservas legales"},
        )
        proposal = proposal_resp.get_json()["proposal"]

        self.assertEqual(proposal_resp.status_code, 200, proposal_resp.get_json())
        self.assertEqual(proposal["kind"], "create_account")
        self.assertIn("Confirmame", proposal["assistant_message"])
        with self.db_session() as session:
            self.assertIsNone(session.get(AccountCatalog, "reservas_legales"))

        apply_resp = self.client.post(f"/api/agent/proposals/{proposal['id']}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(apply_resp.status_code, 200, apply_resp.get_json())
        with self.db_session() as session:
            account = session.get(AccountCatalog, "reservas_legales")
            self.assertIsNotNone(account)
            self.assertEqual(account.account_type, "patrimonio")
        dynamic_accounts = detail["payload"]["accounting"]["dynamic_accounts"]
        self.assertEqual(dynamic_accounts[-1]["name"], "Reservas Legales")

    def test_create_account_rejects_invalid_type_section(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "create_account",
                "args": {
                    "name": "Reserva Mal Clasificada",
                    "account_type": "activo",
                    "section": "patrimonio",
                },
            }
        )

        resp = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "crea una cuenta rara"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("tipo-seccion", resp.get_json()["error"])

    def test_compound_plan_creates_account_and_journal_atomically(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "compound_plan",
                "steps": [
                    {"tool": "find_account", "args": {"name": "Reserva Legal"}},
                    {"tool": "create_account", "args": {"name": "Reserva Legal", "account_type": "patrimonio", "section": "patrimonio"}},
                    {
                        "tool": "journal_entry",
                        "args": {
                            "month": "2026-01",
                            "description": "Traslado a reserva legal",
                            "lines": [
                                {"account": "Resultados Acumulados", "debit": 100000, "credit": 0},
                                {"account": "Reserva Legal", "debit": 0, "credit": 100000},
                            ],
                        },
                    },
                ],
            }
        )

        response = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "crea reserva legal y traslada 100000"},
        )
        proposal = response.get_json()["proposal"]

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(proposal["kind"], "compound_agent_proposal")
        self.assertEqual(proposal["compound_type"], "planned_account_and_entry")
        self.assertEqual(len(proposal["account_operations"]), 1)
        self.assertEqual(proposal["user_visible_steps"][0]["kind"], "create_account")
        self.assertEqual(proposal["user_visible_steps"][1]["kind"], "journal_entry")

        apply_resp = self.client.post(f"/api/agent/proposals/{proposal['id']}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(apply_resp.status_code, 200, apply_resp.get_json())
        with self.db_session() as session:
            account = session.get(AccountCatalog, "reserva_legal")
            self.assertIsNotNone(account)
            audit = session.scalars(select(AuditLog).where(AuditLog.action == "agent_apply_compound_proposal")).first()
            metadata = json.loads(audit.metadata_json)
        self.assertEqual(metadata["proposal_kind"], "compound_agent_proposal")
        self.assertEqual(metadata["compound_type"], "planned_account_and_entry")
        self.assertIn("reserva_legal", metadata["created_account_codes"])
        entries = detail["payload"]["movements"]["journal_entries"]
        self.assertEqual(entries[-1]["description"], "Traslado a reserva legal")
        self.assertEqual(entries[-1]["lines"][1]["account"], "reserva_legal")

    def test_compound_plan_omits_account_creation_when_account_exists(self):
        with self.db_session() as session:
            session.add(
                AccountCatalog(
                    id="reserva_legal",
                    code="reserva_legal",
                    name="Reserva Legal",
                    account_type="patrimonio",
                    section="patrimonio",
                    aliases_json=json.dumps(["reserva"]),
                    source="test",
                    is_postable=1,
                )
            )
            session.commit()
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "compound_plan",
                "steps": [
                    {"tool": "find_account", "args": {"name": "reserva"}},
                    {"tool": "create_account", "args": {"name": "Reserva Legal", "account_type": "patrimonio", "section": "patrimonio"}},
                    {
                        "tool": "journal_entry",
                        "args": {
                            "month": "2026-01",
                            "description": "Traslado a reserva existente",
                            "lines": [
                                {"account": "Resultados Acumulados", "debit": 1000, "credit": 0},
                                {"account": "reserva", "debit": 0, "credit": 1000},
                            ],
                        },
                    },
                ],
            }
        )

        proposal = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "usa reserva existente"},
        ).get_json()["proposal"]

        self.assertEqual(proposal["kind"], "compound_agent_proposal")
        self.assertEqual(proposal["account_operations"], [])
        self.assertEqual([step["kind"] for step in proposal["user_visible_steps"]], ["journal_entry"])
        self.assertEqual(proposal["technical_records"][-1]["lines"][1]["account"], "reserva_legal")

    def test_compound_plan_catalog_change_marks_stale(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "compound_plan",
                "steps": [
                    {"tool": "create_account", "args": {"name": "Reserva Nueva", "account_type": "patrimonio", "section": "patrimonio"}},
                    {
                        "tool": "journal_entry",
                        "args": {
                            "month": "2026-01",
                            "description": "Traslado a reserva nueva",
                            "lines": [
                                {"account": "Resultados Acumulados", "debit": 1000, "credit": 0},
                                {"account": "Reserva Nueva", "debit": 0, "credit": 1000},
                            ],
                        },
                    },
                ],
            }
        )
        proposal_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "crea reserva nueva"},
        ).get_json()["proposal"]["id"]
        with self.db_session() as session:
            session.add(
                AccountCatalog(
                    id="otra_cuenta",
                    code="otra_cuenta",
                    name="Otra Cuenta",
                    account_type="patrimonio",
                    section="patrimonio",
                    source="test",
                    is_postable=1,
                )
            )
            session.commit()

        apply_resp = self.client.post(f"/api/agent/proposals/{proposal_id}/apply")

        self.assertEqual(apply_resp.status_code, 409)
        with self.db_session() as session:
            self.assertEqual(session.get(AgentProposal, proposal_id).status, "stale")

    def test_short_memory_repeats_last_applied_journal_entry(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "description": "Pago base",
                    "lines": [
                        {"account": "Proveedores", "debit": 1000, "credit": 0},
                        {"account": "Efectivo", "debit": 0, "credit": 1000},
                    ],
                },
            }
        )
        base = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "registra pago base"},
        ).get_json()["proposal"]
        self.client.post(f"/api/agent/proposals/{base['id']}/apply")

        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "journal_entry", "args": {"repeat_last": True, "month": "2026-02", "amount": 1500}}
        )
        repeated = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "hace lo mismo en febrero por 1500"},
        ).get_json()["proposal"]

        self.assertEqual(repeated["kind"], "journal_entry_proposal")
        self.assertEqual(repeated["month"], "2026-02")
        self.assertEqual(repeated["journal_rows"][0]["debit"], 1500)
        self.assertEqual(repeated["journal_rows"][1]["credit"], 1500)

    def test_short_memory_rejects_amount_change_for_multiline_entry(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "description": "Asiento multilinea",
                    "lines": [
                        {"account": "Proveedores", "debit": 1000, "credit": 0},
                        {"account": "Capital", "debit": 500, "credit": 0},
                        {"account": "Efectivo", "debit": 0, "credit": 1500},
                    ],
                },
            }
        )
        base = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "registra asiento multilinea"},
        ).get_json()["proposal"]
        self.client.post(f"/api/agent/proposals/{base['id']}/apply")

        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "journal_entry", "args": {"repeat_last": True, "month": "2026-02", "amount": 1500}}
        )
        repeated = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "hace lo mismo por 1500"},
        )

        self.assertEqual(repeated.status_code, 400)
        self.assertIn("mas de dos lineas", repeated.get_json()["error"])

    def test_dynamic_account_can_be_used_in_ledger_after_creation(self):
        with self.db_session() as session:
            session.add(
                AccountCatalog(
                    id="reservas_legales",
                    code="reservas_legales",
                    name="Reservas Legales",
                    account_type="patrimonio",
                    section="patrimonio",
                    source="test",
                    is_postable=1,
                )
            )
            session.commit()
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {
                "intent": "journal_entry",
                "args": {
                    "month": "2026-01",
                    "debit_account": "Capital",
                    "credit_account": "Reservas Legales",
                    "amount": 250000,
                },
            }
        )

        proposal_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "traslada capital a reserva legal"},
        ).get_json()["proposal"]["id"]
        apply_resp = self.client.post(f"/api/agent/proposals/{proposal_id}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(apply_resp.status_code, 200, apply_resp.get_json())
        self.assertEqual(detail["payload"]["movements"]["journal_entries"][-1]["credit_account"], "reservas_legales")
        self.assertTrue(any(acc["name"] == "Reservas Legales" for acc in detail["payload"]["accounting"]["dynamic_accounts"]))
        result = build_financial_model(detail["payload"])
        self.assertIn("Reservas Legales", set(result.df_esf_mensual_full["Descripcion"]))
        self.assertIn("Reservas Legales", result.accounting["accounts"])
        self.assertTrue(result.validations["balance"]["ok"])


if __name__ == "__main__":
    unittest.main()

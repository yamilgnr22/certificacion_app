from __future__ import annotations

import os
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import web_server
from datetime import datetime, timedelta, timezone

from db.models import AccountCatalog, AgentMessage, AgentProposal, Base, PeriodoCertificacion
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

    def test_reverse_voucher_proposal_adds_saved_reversal(self):
        web_server.app.config["AGENT_LLM_PROVIDER"] = FakeProvider(
            {"intent": "reverse_voucher", "args": {"voucher_id": "CD-2026-0001"}}
        )
        proposal_id = self.client.post(
            "/api/agent/command",
            json={"periodo_id": self.periodo["id"], "message": "reversa CD-2026-0001"},
        ).get_json()["proposal"]["id"]

        resp = self.client.post(f"/api/agent/proposals/{proposal_id}/apply")
        detail = self.client.get(f"/api/periodos/{self.periodo['id']}").get_json()["periodo"]

        self.assertEqual(resp.status_code, 200, resp.get_json())
        vouchers = detail["payload"]["accounting"]["vouchers"]
        self.assertEqual(vouchers[-1]["reference_voucher_id"], "CD-2026-0001")
        self.assertEqual(vouchers[-1]["type"], "reversal")

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
        self.assertEqual(detail["payload"]["movements"]["journal_entries"][-1]["credit_account"], "Reservas Legales")
        self.assertTrue(any(acc["name"] == "Reservas Legales" for acc in detail["payload"]["accounting"]["dynamic_accounts"]))
        result = build_financial_model(detail["payload"])
        self.assertIn("Reservas Legales", set(result.df_esf_mensual_full["Descripcion"]))
        self.assertIn("Reservas Legales", result.accounting["accounts"])
        self.assertTrue(result.validations["balance"]["ok"])


if __name__ == "__main__":
    unittest.main()

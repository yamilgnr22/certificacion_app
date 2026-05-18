from __future__ import annotations

import os
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import web_server
from db.models import AgentMessage, Base
from db.seed import seed_giros
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


if __name__ == "__main__":
    unittest.main()

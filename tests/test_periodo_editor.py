"""Tests del Bloque 4D: editor avanzado de Periodo (payload, editables, update_payload)."""
from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import web_server
from db.models import AuditLog, Base, PeriodoCertificacion
from db.seed import seed_giros


def cliente_payload(**overrides):
    p = {
        "nombre_completo": "Cliente Editor",
        "cedula": "001-edit-0001",
        "nombre_negocio": "Negocio Edit",
        "direccion_negocio": "Managua",
        "giro_negocio_id": "ferreteria",
    }
    p.update(overrides)
    return p


def periodo_body(**overrides):
    p = {
        "mes_inicial": "2026-01",
        "mes_final": "2026-06",
        "tasa_cambio": 36.6243,
        "ingresos_base_usd": 50000,
        "variabilidad_ingresos_pct": 12.0,
        "cost_pct": 70.0,
        "variabilidad_costos_pct": 5.0,
        "cash_sales_pct": 85.0,
        "seed": "editor-seed",
    }
    p.update(overrides)
    return p


class PeriodoEditorTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        with self.factory() as s:
            seed_giros(s)
            s.commit()
        self.old_engine = web_server.app.config.get("DB_ENGINE")
        self.old_require = web_server.app.config.get("DB_REQUIRE_ALEMBIC")
        web_server.app.config["DB_ENGINE"] = self.engine
        web_server.app.config["DB_REQUIRE_ALEMBIC"] = False
        self.client = web_server.app.test_client()

    def tearDown(self):
        if self.old_engine is None:
            web_server.app.config.pop("DB_ENGINE", None)
        else:
            web_server.app.config["DB_ENGINE"] = self.old_engine
        if self.old_require is None:
            web_server.app.config.pop("DB_REQUIRE_ALEMBIC", None)
        else:
            web_server.app.config["DB_REQUIRE_ALEMBIC"] = self.old_require

    def _crear_cliente_y_periodo(self, **periodo_overrides):
        cli = self.client.post("/api/clientes", json=cliente_payload()).get_json()["cliente"]
        per = self.client.post(f"/api/clientes/{cli['id']}/periodos", json=periodo_body(**periodo_overrides)).get_json()["periodo"]
        return cli, per

    # ------------------------------------------------- GET detail con payload
    def test_get_detail_incluye_payload_parseado(self):
        _, per = self._crear_cliente_y_periodo()
        resp = self.client.get(f"/api/periodos/{per['id']}")
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("payload", data["periodo"])
        payload = data["periodo"]["payload"]
        self.assertIsInstance(payload, dict)
        self.assertIn("period", payload)
        self.assertIn("income", payload)

    # -------------------------------------------------- GET editables
    def test_editables_devuelve_borradores_primero(self):
        cli = self.client.post("/api/clientes", json=cliente_payload()).get_json()["cliente"]
        # Crear 3 periodos: uno finalizado, dos borradores
        p_fin = self.client.post(f"/api/clientes/{cli['id']}/periodos", json=periodo_body(mes_inicial="2025-01", mes_final="2025-06")).get_json()["periodo"]
        self.client.post(f"/api/periodos/{p_fin['id']}/finalizar")
        p_b1 = self.client.post(f"/api/clientes/{cli['id']}/periodos", json=periodo_body(mes_inicial="2025-07", mes_final="2025-12")).get_json()["periodo"]
        p_b2 = self.client.post(f"/api/clientes/{cli['id']}/periodos", json=periodo_body(mes_inicial="2026-01", mes_final="2026-06")).get_json()["periodo"]

        resp = self.client.get("/api/periodos/editables")
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        records = data["periodos"]
        self.assertEqual(len(records), 3)
        # Borradores primero
        estados = [r["estado"] for r in records]
        self.assertEqual(estados[0], "borrador")
        self.assertEqual(estados[1], "borrador")
        self.assertEqual(estados[2], "finalizado")
        # Incluye datos del cliente
        self.assertEqual(records[0]["cliente_nombre"], cli["nombre_completo"])

    def test_editables_excluye_clientes_inactivos(self):
        cli, per = self._crear_cliente_y_periodo()
        # Soft-delete del cliente
        self.client.delete(f"/api/clientes/{cli['id']}")
        resp = self.client.get("/api/periodos/editables")
        self.assertEqual(len(resp.get_json()["periodos"]), 0)

    # ---------------------------------------------- PUT /payload
    def test_update_payload_sobre_borrador_persiste_y_audita(self):
        _, per = self._crear_cliente_y_periodo()
        # Cambiar varios bloques del payload
        new_payload = {
            "period": {
                "start_month": "2026-01",
                "end_month": "2026-06",
                "exchange_rate": 36.6243,
                "seed": "editor-modificado",
            },
            "income": {
                "base_income_usd": 75000,
                "cost_pct": 65,
                "income_variability_pct": 10,
                "cost_variability_pct": 3,
                "cash_sales_pct": 90,
            },
            "balances": {"cash": 999999},
        }
        resp = self.client.put(f"/api/periodos/{per['id']}/payload", json={"payload": new_payload})
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertEqual(data["periodo"]["ingresos_base_usd"], 75000)
        self.assertEqual(data["periodo"]["cost_pct"], 65)
        self.assertEqual(data["periodo"]["seed"], "editor-modificado")
        self.assertIn("changed_blocks", data)
        self.assertIn("income", data["changed_blocks"])
        self.assertIn("balances", data["changed_blocks"])
        # Audit log
        with self.factory() as session:
            entries = list(session.scalars(
                select(AuditLog).where(AuditLog.action == "update_payload")
            ))
            self.assertEqual(len(entries), 1)
            meta = json.loads(entries[0].metadata_json)
            self.assertIn("income", meta["changed_blocks"])

    def test_update_payload_sobre_finalizado_devuelve_409(self):
        _, per = self._crear_cliente_y_periodo()
        self.client.post(f"/api/periodos/{per['id']}/finalizar")
        resp = self.client.put(f"/api/periodos/{per['id']}/payload", json={"payload": {"period": {"start_month": "2026-01", "end_month": "2026-06"}}})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("borrador", resp.get_json()["error"].lower())

    def test_update_payload_id_inexistente_devuelve_404(self):
        resp = self.client.put("/api/periodos/no-existe/payload", json={"payload": {"period": {"start_month": "2026-01", "end_month": "2026-06"}}})
        self.assertEqual(resp.status_code, 404)

    def test_update_payload_sin_period_devuelve_400(self):
        _, per = self._crear_cliente_y_periodo()
        resp = self.client.put(f"/api/periodos/{per['id']}/payload", json={"payload": {"income": {}}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("period", resp.get_json()["error"].lower())

    def test_update_payload_marca_descendants_recompute(self):
        cli, parent = self._crear_cliente_y_periodo(mes_inicial="2026-01", mes_final="2026-06")
        # Crear hijo manualmente apuntando al padre
        with self.factory() as session:
            child = PeriodoCertificacion(
                cliente_id=cli["id"],
                periodo_meses=6,
                mes_inicial="2026-07",
                mes_final="2026-12",
                estado="borrador",
                tasa_cambio=36.6243,
                saldos_iniciales_origen="rollforward",
                periodo_anterior_id=parent["id"],
                payload_json="{}",
                recompute_required=0,
            )
            session.add(child)
            session.commit()
            child_id = child.id

        # Editar el padre
        new_payload = {
            "period": {"start_month": "2026-01", "end_month": "2026-06", "exchange_rate": 36.6243},
            "income": {"base_income_usd": 60000},
        }
        resp = self.client.put(f"/api/periodos/{parent['id']}/payload", json={"payload": new_payload})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(child_id, resp.get_json()["invalidated_descendants"])
        with self.factory() as session:
            refreshed = session.get(PeriodoCertificacion, child_id)
            self.assertEqual(refreshed.recompute_required, 1)


if __name__ == "__main__":
    unittest.main()

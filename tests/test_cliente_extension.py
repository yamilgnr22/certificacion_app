"""Tests del Bloque 4C: campos de certificacion en Cliente."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import web_server
from db.models import AuditLog, Base, Cliente
from db.seed import seed_giros


def cliente_base(**overrides):
    p = {
        "nombre_completo": "Cliente Extendido",
        "cedula": "001-ext-0001",
        "nombre_negocio": "Negocio Ext",
        "direccion_negocio": "Managua",
        "giro_negocio_id": "ferreteria",
    }
    p.update(overrides)
    return p


class ClienteExtensionTest(unittest.TestCase):
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

    # --------------------------------------------- modelo y migracion
    def test_cliente_tiene_campos_nuevos(self):
        cols = {c.name for c in Cliente.__table__.columns}
        for f in ["sexo", "estado_civil", "profesion", "banco", "regimen", "antiguedad", "empleados", "domicilio"]:
            self.assertIn(f, cols)

    # ----------------------------------------------------- API
    def test_create_cliente_con_campos_certificacion(self):
        resp = self.client.post("/api/clientes", json=cliente_base(
            sexo="Femenino",
            estado_civil="casada",
            profesion="Licenciada en Economia",
            banco="BAC",
            regimen="Cuota Fija",
            antiguedad="6 anios",
            empleados="6",
            domicilio="Municipio de Managua, Departamento de Managua.",
        ))
        data = resp.get_json()
        self.assertEqual(resp.status_code, 201, data)
        cli = data["cliente"]
        self.assertEqual(cli["sexo"], "Femenino")
        self.assertEqual(cli["estado_civil"], "casada")
        self.assertEqual(cli["profesion"], "Licenciada en Economia")
        self.assertEqual(cli["banco"], "BAC")
        self.assertEqual(cli["empleados"], "6")
        self.assertEqual(cli["domicilio"], "Municipio de Managua, Departamento de Managua.")

    def test_sexo_invalido_devuelve_400(self):
        resp = self.client.post("/api/clientes", json=cliente_base(sexo="Hombrecillo"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("sexo", resp.get_json()["error"].lower())

    def test_sexo_normaliza_capitalizacion(self):
        resp = self.client.post("/api/clientes", json=cliente_base(sexo="FEMENINO"))
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["cliente"]["sexo"], "Femenino")

    def test_update_cliente_actualiza_campos_y_registra_audit(self):
        created = self.client.post("/api/clientes", json=cliente_base()).get_json()["cliente"]
        resp = self.client.put(f"/api/clientes/{created['id']}", json={"profesion": "Contador Publico", "banco": "LAFISE"})
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertEqual(data["cliente"]["profesion"], "Contador Publico")
        self.assertEqual(data["cliente"]["banco"], "LAFISE")
        with self.factory() as session:
            entries = list(session.scalars(select(AuditLog).where(AuditLog.action == "update").order_by(AuditLog.id.desc())))
            self.assertGreaterEqual(len(entries), 1)
            meta = json.loads(entries[0].metadata_json)
            self.assertIn("profesion", meta["changed_fields"])
            self.assertIn("banco", meta["changed_fields"])

    # --------------------------------------------- DOCX usa campos del cliente
    def test_docx_generado_contiene_campos_de_certificacion(self):
        tmp_dir = tempfile.mkdtemp(prefix="docx_ext_")
        os.environ["CERTAPP_DOCUMENTOS_DIR"] = tmp_dir
        try:
            cli = self.client.post("/api/clientes", json=cliente_base(
                sexo="Femenino",
                estado_civil="casada",
                profesion="Licenciada en Economia",
                banco="BAC",
                regimen="Cuota Fija",
                antiguedad="6 anios",
                empleados="6",
            )).get_json()["cliente"]

            per = self.client.post(f"/api/clientes/{cli['id']}/periodos", json={
                "mes_inicial": "2026-01", "mes_final": "2026-06",
                "tasa_cambio": 36.6243, "ingresos_base_usd": 50000,
                "variabilidad_ingresos_pct": 12, "cost_pct": 70,
                "variabilidad_costos_pct": 5, "cash_sales_pct": 85,
            }).get_json()["periodo"]
            self.client.post(f"/api/periodos/{per['id']}/finalizar")

            # Verificar que el adaptador construye el client_block con los campos
            from db.models import Cliente as ClienteModel
            from db.models import PeriodoCertificacion as Periodo
            from db.models import GiroNegocio
            from services.periodo_document_adapter import _client_block
            with self.factory() as session:
                cliente_db = session.get(ClienteModel, cli["id"])
                giro_db = session.get(GiroNegocio, cliente_db.giro_negocio_id)
                block = _client_block(cliente_db, giro_db)
                self.assertEqual(block["sexo"], "Femenino")
                self.assertEqual(block["estado_civil"], "casada")
                self.assertEqual(block["profesion"], "Licenciada en Economia")
                self.assertEqual(block["banco"], "BAC")
                self.assertEqual(block["regimen"], "Cuota Fija")
                self.assertEqual(block["antiguedad"], "6 anios")
                self.assertEqual(block["empleados"], "6")
                self.assertEqual(block["giro_negocio"], giro_db.nombre)

            # Generar el documento de punta a punta
            gen = self.client.post(f"/api/periodos/{per['id']}/generar-documento").get_json()
            self.assertTrue(Path(gen["documento_path"]).exists())
            self.assertGreater(Path(gen["documento_path"]).stat().st_size, 1000)
        finally:
            os.environ.pop("CERTAPP_DOCUMENTOS_DIR", None)
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_fallback_a_vision_ia_cuando_cliente_no_tiene_campo(self):
        """Si el Cliente no tiene sexo guardado, debe usar el extraido por vision IA."""
        from db.models import Cliente as ClienteModel
        from services.periodo_document_adapter import _client_block

        # Crear cliente sin sexo pero con last_cedula_extracted_json con sexo
        cli = self.client.post("/api/clientes", json=cliente_base()).get_json()["cliente"]
        with self.factory() as session:
            cliente_db = session.get(ClienteModel, cli["id"])
            cliente_db.last_cedula_extracted_json = json.dumps({"sexo": "Femenino"})
            session.commit()
            block = _client_block(cliente_db, cliente_db.giro)
            self.assertEqual(block["sexo"], "Femenino")


if __name__ == "__main__":
    unittest.main()

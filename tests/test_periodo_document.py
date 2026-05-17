"""Tests del flujo de generacion y descarga de documento DOCX desde Periodo."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import web_server
from db.models import Base
from db.seed import seed_giros


def cliente_payload(**overrides):
    p = {
        "nombre_completo": "Cliente Documento",
        "cedula": "001-doc-9999",
        "nombre_negocio": "Negocio Doc",
        "direccion_negocio": "Managua, Demo",
        "giro_negocio_id": "ferreteria",
    }
    p.update(overrides)
    return p


def periodo_payload(**overrides):
    p = {
        "mes_inicial": "2026-01",
        "mes_final": "2026-06",
        "tasa_cambio": 36.6243,
        "ingresos_base_usd": 50000,
        "variabilidad_ingresos_pct": 12.0,
        "cost_pct": 70.0,
        "variabilidad_costos_pct": 5.0,
        "cash_sales_pct": 85.0,
        "seed": "test-doc-seed",
    }
    p.update(overrides)
    return p


class PeriodoDocumentTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="certapp_docs_")
        os.environ["CERTAPP_DOCUMENTOS_DIR"] = self.tmp_dir

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
        # Restaurar env
        os.environ.pop("CERTAPP_DOCUMENTOS_DIR", None)
        if self.old_engine is None:
            web_server.app.config.pop("DB_ENGINE", None)
        else:
            web_server.app.config["DB_ENGINE"] = self.old_engine
        if self.old_require is None:
            web_server.app.config.pop("DB_REQUIRE_ALEMBIC", None)
        else:
            web_server.app.config["DB_REQUIRE_ALEMBIC"] = self.old_require
        # Limpiar archivos generados
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _crear_cliente_y_periodo(self):
        cresp = self.client.post("/api/clientes", json=cliente_payload())
        self.assertEqual(cresp.status_code, 201)
        cid = cresp.get_json()["cliente"]["id"]
        presp = self.client.post(f"/api/clientes/{cid}/periodos", json=periodo_payload())
        self.assertEqual(presp.status_code, 201)
        pid = presp.get_json()["periodo"]["id"]
        return cid, pid

    # -------------------------------------------------------------- estados
    def test_generate_on_borrador_returns_409(self):
        _, pid = self._crear_cliente_y_periodo()
        resp = self.client.post(f"/api/periodos/{pid}/generar-documento")
        self.assertEqual(resp.status_code, 409, resp.get_json())
        self.assertIn("borrador", resp.get_json()["error"].lower())

    def test_generate_on_finalizado_creates_file_and_persists_path(self):
        cid, pid = self._crear_cliente_y_periodo()
        self.client.post(f"/api/periodos/{pid}/finalizar")
        resp = self.client.post(f"/api/periodos/{pid}/generar-documento")
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"])
        path = data["documento_path"]
        self.assertTrue(Path(path).exists())
        self.assertGreater(Path(path).stat().st_size, 1000)  # docx no vacio
        # Documento esta dentro del tmp dir
        self.assertTrue(str(path).startswith(self.tmp_dir))

        # Reconsulta el periodo y verifica que persistio path + timestamp
        detail = self.client.get(f"/api/periodos/{pid}").get_json()
        self.assertEqual(detail["periodo"]["documento_path"], path)
        self.assertIsNotNone(detail["periodo"]["documento_generado_at"])

    def test_download_returns_docx_after_generate(self):
        _, pid = self._crear_cliente_y_periodo()
        self.client.post(f"/api/periodos/{pid}/finalizar")
        self.client.post(f"/api/periodos/{pid}/generar-documento")
        resp = self.client.get(f"/api/periodos/{pid}/documento")
        self.assertEqual(resp.status_code, 200)
        ctype = resp.headers.get("Content-Type", "")
        self.assertTrue(
            "wordprocessingml" in ctype or "officedocument" in ctype or "application/octet-stream" in ctype,
            f"Content-Type inesperado: {ctype}",
        )
        self.assertGreater(len(resp.data), 1000)

    def test_download_before_generate_returns_404(self):
        _, pid = self._crear_cliente_y_periodo()
        self.client.post(f"/api/periodos/{pid}/finalizar")
        resp = self.client.get(f"/api/periodos/{pid}/documento")
        self.assertEqual(resp.status_code, 404)

    def test_regenerate_overwrites_file(self):
        _, pid = self._crear_cliente_y_periodo()
        self.client.post(f"/api/periodos/{pid}/finalizar")
        r1 = self.client.post(f"/api/periodos/{pid}/generar-documento").get_json()
        size_1 = Path(r1["documento_path"]).stat().st_size
        mtime_1 = Path(r1["documento_path"]).stat().st_mtime

        # Regenerar
        import time
        time.sleep(0.05)  # asegurar mtime distinto
        r2 = self.client.post(f"/api/periodos/{pid}/generar-documento").get_json()
        self.assertEqual(r2["documento_path"], r1["documento_path"])  # misma ruta
        mtime_2 = Path(r2["documento_path"]).stat().st_mtime
        self.assertGreater(mtime_2, mtime_1)  # archivo nuevo

    def test_generate_logs_audit_entry(self):
        from db.models import AuditLog
        from sqlalchemy import select
        _, pid = self._crear_cliente_y_periodo()
        self.client.post(f"/api/periodos/{pid}/finalizar")
        self.client.post(f"/api/periodos/{pid}/generar-documento")
        with self.factory() as session:
            entries = list(session.scalars(
                select(AuditLog).where(AuditLog.entity_type == "periodo", AuditLog.entity_id == pid)
            ))
            actions = [e.action for e in entries]
            self.assertIn("generate_document", actions)

    def test_generate_404_when_periodo_not_found(self):
        resp = self.client.post("/api/periodos/no-existe/generar-documento")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()

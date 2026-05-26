from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import web_server
from db.models import AccountCatalog, Base
from scripts.import_account_catalog import import_catalog, load_catalog_records


def make_catalog_file() -> Path:
    rows = [
        ("1 ACTIVOS", "11 Activos corriente", "111 Efectivo y Equivalentes al Efectivo", "Deudora"),
        ("1 ACTIVOS", "11 Activos corriente", "113 Deudores Comerciales y otras cuentas por cobrar", "Deudora"),
        ("1 ACTIVOS", "11 Activos corriente", "115 Inventarios", "Deudora"),
        ("1 ACTIVOS", "12 Activos no corriente", "121 Propiedad, Planta y Equipos", "Deudora"),
        ("2 PASIVOS", "21 Pasivos Corriente", "212 Cuentas por pagar comerciales", "Acreedora"),
        ("2 PASIVOS", "21 Pasivos Corriente", "213 Prestamos Pagar Corriente", "Acreedora"),
        ("2 PASIVOS", "21 Pasivos Corriente", "214 Gastos Devengados por pagar", "Acreedora"),
        ("2 PASIVOS", "21 Pasivos Corriente", "219 Impuesto a las Ganancias", "Acreedora"),
        ("2 PASIVOS", "22 Pasivos no corrientes", "221 Prestamo e intereses por pagar", "Acreedora"),
        ("3 CAPITAL CONTABLE", "31 Patrimonio Aportado", "311 Capital Social", "Acreedora"),
        ("3 CAPITAL CONTABLE", "32 Patrimonio Ganado", "321 Resultados Acumulados", "Acreedora"),
        ("3 CAPITAL CONTABLE", "32 Patrimonio Ganado", "322 Resultados del Ejercicio", "Acreedora"),
        ("3 CAPITAL CONTABLE", "32 Patrimonio Ganado", "325 Reserva Legal", "Acreedora"),
        ("4 INGRESOS", "41 Ingresos por Actividades Ordinarias", "411 Ventas", "Acreedora"),
        ("5 COSTOS", "51 Costo de Venta", "511 Costo de los Productos Vendidos", "Deudora"),
        ("6 GASTOS", "61 Operativos", "611 Sueldos", "Deudora"),
        ("6 GASTOS", "61 Operativos", "612 Servicios", "Deudora"),
        ("6 GASTOS", "61 Operativos", "613 Depreciaciones", "Deudora"),
        ("6 GASTOS", "61 Operativos", "615 Gastos Financieros", "Deudora"),
        ("6 GASTOS", "61 Operativos", "619 Otros", "Deudora"),
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = "Catalogo"
    ws.append(["GRUPO", "SUB GRUPO", "RUBRO", "NATURALEZA"])
    for row in rows:
        ws.append(row)
    path = Path(tempfile.gettempdir()) / "catalogo_niif_test.xlsx"
    wb.save(path)
    return path


class AccountCatalogImportTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        self.path = make_catalog_file()
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

    def test_dry_run_does_not_write(self):
        with self.factory() as session:
            summary = import_catalog(self.path, apply=False, session=session)
            count = session.query(AccountCatalog).count()

        self.assertEqual(summary["mode"], "dry-run")
        self.assertGreater(summary["records"], 20)
        self.assertEqual(count, 0)

    def test_apply_loads_niif_and_enriched_accounts(self):
        with self.factory() as session:
            summary = import_catalog(self.path, apply=True, session=session)
            cash = session.get(AccountCatalog, "cash")
            rent = session.get(AccountCatalog, "exp_rent")
            ppe_parent = session.get(AccountCatalog, "niif_121")

        self.assertEqual(summary["mode"], "apply")
        self.assertIsNotNone(cash)
        self.assertEqual(cash.niif_code, "111")
        self.assertEqual(cash.required_model_account, 1)
        self.assertIsNotNone(rent)
        self.assertEqual(rent.parent_code, "niif_619")
        self.assertIsNotNone(ppe_parent)

    def test_missing_columns_fail(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["GRUPO", "RUBRO"])
        path = Path(tempfile.gettempdir()) / "catalogo_niif_bad.xlsx"
        wb.save(path)

        with self.assertRaisesRegex(ValueError, "Faltan columnas"):
            load_catalog_records(path)

    def test_api_lists_catalog_and_searches_alias(self):
        with self.factory() as session:
            import_catalog(self.path, apply=True, session=session)

        resp = self.client.get("/api/catalogo?q=alquiler")
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["accounts"][0]["code"], "exp_rent")
        self.assertFalse(data["summary"]["missing_required"])

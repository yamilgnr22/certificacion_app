from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import web_server
from db.models import AccountCatalog, AuditLog, Base, Cliente
from db.seed import seed_giros
from repositories import PeriodoRepository
from repositories.audit_repo import AuditRepository
from scripts.import_account_catalog import INTERNAL_ACCOUNTS, POSTABLE_ACCOUNT_CODES


def cliente_payload(**overrides):
    payload = {
        "nombre_completo": "Kitiel Rosibel Montiel Gonzalez",
        "cedula": "361-060491-0000X",
        "nombre_negocio": "Motonic",
        "direccion_negocio": "Managua, Altamira",
        "giro_negocio_id": "comercio_general",
        "telefono": "+505 5712 6278",
    }
    payload.update(overrides)
    return payload


class ClienteApiTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        with self.factory() as session:
            seed_giros(session)
            now = datetime.now(timezone.utc)
            for record in INTERNAL_ACCOUNTS:
                session.add(
                    AccountCatalog(
                        id=record.code,
                        code=record.code,
                        niif_code=record.niif_code or None,
                        name=record.name,
                        account_type=record.account_type,
                        section=record.section,
                        normal_balance=record.normal_balance or None,
                        parent_code=record.parent_code or None,
                        aliases_json=json.dumps(list(record.aliases), ensure_ascii=False),
                        display_order=record.display_order,
                        required_model_account=1 if record.required_model_account else 0,
                        is_recurring_expense=1 if record.is_recurring_expense else 0,
                        legacy_payload_key=record.legacy_payload_key or None,
                        is_postable=1 if (record.is_postable or record.code in POSTABLE_ACCOUNT_CODES) else 0,
                        source=record.source,
                        created_at=now,
                        updated_at=now,
                        active=1,
                    )
                )
            session.commit()
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

    def db_session(self):
        return self.factory()

    def create_cliente(self, **overrides):
        resp = self.client.post("/api/clientes", json=cliente_payload(**overrides))
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()["cliente"]

    def audit_entries(self):
        with self.db_session() as session:
            return list(session.scalars(select(AuditLog).order_by(AuditLog.id)))

    def test_get_giros_returns_seed_catalog(self):
        resp = self.client.get("/api/giros")
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertIn("comercio_general", {item["id"] for item in data["giros"]})

    def test_create_cliente_creates_audit_log(self):
        created = self.create_cliente()
        entries = self.audit_entries()

        self.assertEqual(created["cedula"], "361-060491-0000X")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].action, "create")
        self.assertIsNotNone(entries[0].payload_after_hash)

    def test_create_cliente_rejects_duplicate_active_cedula(self):
        self.create_cliente()

        resp = self.client.post("/api/clientes", json=cliente_payload(nombre_negocio="Otro negocio"))

        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.get_json()["ok"])

    def test_create_cliente_rejects_invalid_giro(self):
        resp = self.client.post("/api/clientes", json=cliente_payload(giro_negocio_id="no_existe"))

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Giro", resp.get_json()["error"])

    def test_list_clientes_searches_and_filters(self):
        created = self.create_cliente()

        by_name = self.client.get("/api/clientes?q=Kitiel").get_json()["clientes"]
        by_cedula = self.client.get("/api/clientes?q=361-060491").get_json()["clientes"]
        by_business = self.client.get("/api/clientes?q=Motonic").get_json()["clientes"]
        by_giro = self.client.get("/api/clientes?giro=comercio_general").get_json()["clientes"]

        self.assertEqual(by_name[0]["id"], created["id"])
        self.assertEqual(by_cedula[0]["id"], created["id"])
        self.assertEqual(by_business[0]["id"], created["id"])
        self.assertEqual(by_giro[0]["id"], created["id"])

    def test_get_cliente_includes_periodos_and_effective_template(self):
        created = self.create_cliente()

        resp = self.client.get(f"/api/clientes/{created['id']}")
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["cliente"]["id"], created["id"])
        self.assertEqual(data["periodos"], [])
        self.assertEqual(data["plantilla_gastos"]["origen"], "giro")
        self.assertEqual(data["plantilla_gastos"]["version"], 2)
        self.assertIn("Renta", data["plantilla_gastos"]["plantilla"])
        self.assertIn("exp_rent", {item["account_code"] for item in data["plantilla_gastos"]["items"]})

    def test_update_cliente_audits_hashes_and_changed_fields(self):
        created = self.create_cliente()

        resp = self.client.put(f"/api/clientes/{created['id']}", json={"telefono": "+505 8888 0000"})
        data = resp.get_json()
        entries = self.audit_entries()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["cliente"]["telefono"], "+505 8888 0000")
        self.assertEqual(entries[-1].action, "update")
        self.assertIsNotNone(entries[-1].payload_before_hash)
        self.assertIsNotNone(entries[-1].payload_after_hash)
        self.assertIn("telefono", json.loads(entries[-1].metadata_json)["changed_fields"])

    def test_update_cedula_fails_when_cliente_has_certified_period(self):
        created = self.create_cliente()
        with self.db_session() as session:
            PeriodoRepository(session).create(
                cliente_id=created["id"],
                periodo_meses=1,
                mes_inicial="2026-04",
                mes_final="2026-04",
                estado="certificado",
                tasa_cambio=36.6243,
                saldos_iniciales_origen="manual",
                payload_json="{}",
            )
            session.commit()

        resp = self.client.put(f"/api/clientes/{created['id']}", json={"cedula": "001-010101-0000A"})

        self.assertEqual(resp.status_code, 409)
        self.assertIn("periodos certificados", resp.get_json()["error"])

    def test_delete_cliente_soft_deletes_and_audits(self):
        created = self.create_cliente()

        resp = self.client.delete(f"/api/clientes/{created['id']}")
        listed = self.client.get("/api/clientes").get_json()["clientes"]

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(listed, [])
        with self.db_session() as session:
            stored = session.get(Cliente, created["id"])
            self.assertEqual(stored.activo, 0)
        self.assertEqual(self.audit_entries()[-1].action, "delete")

    def test_set_plantilla_merges_with_giro_template(self):
        created = self.create_cliente()

        resp = self.client.put(
            f"/api/clientes/{created['id']}/plantilla-gastos",
            json={"items": [{"account_code": "exp_rent", "amount_usd": 999}]},
        )
        data = resp.get_json()["plantilla_gastos"]

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["origen"], "cliente")
        self.assertEqual(data["plantilla"]["Renta"], 999.0)
        self.assertIn("Sueldos y Salarios", data["plantilla"])
        self.assertIn("exp_rent", {item["account_code"] for item in data["items"]})

    def test_legacy_template_duplicate_names_merge_by_catalog_account(self):
        created = self.create_cliente()

        resp = self.client.put(
            f"/api/clientes/{created['id']}/plantilla-gastos",
            json={"Servicios Publicos": 600, "Servicios Públicos": 160},
        )
        data = resp.get_json()["plantilla_gastos"]
        services = [item for item in data["items"] if item["account_code"] == "exp_services"]

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["amount_usd"], 760.0)
        self.assertEqual(data["plantilla"]["Servicios Publicos"], 760.0)
        self.assertTrue(data["warnings"])

    def test_audit_chain_for_real_cliente_operations(self):
        created = self.create_cliente()
        self.client.put(f"/api/clientes/{created['id']}", json={"telefono": "1"})
        self.client.put(f"/api/clientes/{created['id']}", json={"email": "a@b.com"})
        self.client.delete(f"/api/clientes/{created['id']}")

        entries = self.audit_entries()

        self.assertEqual([entry.action for entry in entries], ["create", "update", "update", "delete"])
        for previous, current in zip(entries, entries[1:]):
            self.assertEqual(current.prev_entry_hash, AuditRepository.entry_hash(previous))

    def test_extract_from_docs_returns_patch_without_saving_cliente(self):
        fake = {"ok": True, "client_patch": {"nombre_completo": "Cliente Extraido"}, "documents": {}, "raw": {}}
        with patch("web_server.extract_client_documents", return_value=fake):
            resp = self.client.post(
                "/api/clientes/extract-from-docs",
                data={"cedula_front": (io.BytesIO(b"img"), "cedula.png")},
                content_type="multipart/form-data",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["client_patch"]["nombre_completo"], "Cliente Extraido")
        with self.db_session() as session:
            self.assertEqual(list(session.scalars(select(Cliente))), [])

    def test_cliente_persists_name_extraction_metadata(self):
        payload = cliente_payload()
        payload["last_cedula_extracted_json"] = {
            "name_review_required": True,
            "name_review_resolved": True,
            "selected_name_source": "manual",
            "raw_name_candidates": [{"source": "cedula_general", "nombre_completo": "Cliente Uno"}],
        }

        resp = self.client.post("/api/clientes", json=payload)

        self.assertEqual(resp.status_code, 201)
        with self.db_session() as session:
            stored = session.get(Cliente, resp.get_json()["cliente"]["id"])
            meta = json.loads(stored.last_cedula_extracted_json)
        self.assertTrue(meta["name_review_required"])
        self.assertEqual(meta["selected_name_source"], "manual")


if __name__ == "__main__":
    unittest.main()

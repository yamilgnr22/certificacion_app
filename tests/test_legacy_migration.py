from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from db.models import AuditLog, Base, Cliente, PeriodoCertificacion
from db.seed import seed_giros
from scripts.migrate_legacy_draft_to_sqlite import (
    TARGET_RECORD_ID,
    canonical_payload_hash,
    migrate_legacy_draft,
)


def sample_payload(**overrides):
    payload = {
        "client": {
            "nombre_completo": "Kitiel Rosibel Montiel Gonzalez",
            "cedula": "361-060491-0000X",
            "banco": "BAC",
            "estado_civil": "casada",
            "profesion": "Licenciada en Economia",
            "sexo": "Femenino",
            "domicilio": "Municipio de Managua, Departamento de Managua.",
            "direccion_personal": "Residencial Daniel Chavarria",
            "direccion_negocio": "Altamira, Managua",
            "contacto": "+505 5712 6278",
            "regimen": "Cuota Fija",
            "matricula": "RNVD-117331; ROC No. 138034303",
            "giro_negocio": "Venta de mercaderia en general",
            "antiguedad": "6 anos",
            "empleados": 6,
        },
        "period": {
            "start_month": "2025-01",
            "end_month": "2026-04",
            "exchange_rate": 36.6243,
            "seed": "kitiel-2026-04",
        },
        "income": {
            "base_income_usd": 108000,
            "income_variability_pct": 15,
            "cost_pct": 87,
            "cost_variability_pct": 5,
            "cash_sales_pct": 85,
            "monthly_overrides": [],
        },
        "expenses": {
            "Sueldos y Salarios": 2700,
            "Servicios Publicos": 600,
            "Alcaldia y DGI": 50,
            "Combustible": 500,
            "Publicidad": 1500,
            "Mantenimientos": 0,
            "Renta": 440,
            "Seguros": 0,
            "Otros Gastos": 350,
        },
        "balances": {
            "cash": 410193,
            "accounts_receivable": 62261,
            "inventory": 5310538,
            "ppe_real_estate": 0,
            "ppe_equipment": 549366,
            "ppe_vehicles": 0,
            "accum_depreciation": -68676,
            "credit_cards": 183122,
            "suppliers": 0,
            "taxes_payable": 0,
            "accrued_expenses": 0,
            "loans_personal": 47612,
            "loans_pledge": 0,
            "loans_commercial": 0,
            "loans_mortgage": 0,
            "retained_earnings": 3424750,
        },
        "movements": {
            "purchase_base_usd": 120000,
            "purchase_variability_pct": 10,
            "loan_interest_monthly_pct": 0,
            "events": [
                {"month": "2026-02", "account": "owner_withdrawal", "amount": 600000, "currency": "nio"},
            ],
            "journal_entries": [
                {
                    "month": "2026-01",
                    "debit_account": "current_earnings",
                    "credit_account": "retained_earnings",
                    "amount": 2788875,
                    "currency": "nio",
                    "entry_type": "year_close_transfer",
                    "source": "chat_financiero",
                    "instruction_id": "chat_test",
                }
            ],
        },
    }
    payload.update(overrides)
    return payload


def write_legacy_json(path: Path, payload: dict):
    record = {
        "id": TARGET_RECORD_ID,
        "type": "draft",
        "status": "draft",
        "created_at": "2026-05-15T00:37:09Z",
        "updated_at": "2026-05-16T22:16:55Z",
        "client_slug": "kitiel-rosibel-montiel-gonzalez-361-060491-0000x",
        "client_name": payload["client"]["nombre_completo"],
        "cedula": payload["client"]["cedula"],
        "bank": payload["client"].get("banco"),
        "start_month": payload["period"]["start_month"],
        "end_month": payload["period"]["end_month"],
        "period_label": "2025-01 a 2026-04",
        "payload": payload,
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


class LegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        with self.factory() as session:
            seed_giros(session)
            session.commit()
        self.tmp = tempfile.TemporaryDirectory()
        self.legacy_path = Path(self.tmp.name) / f"{TARGET_RECORD_ID}.json"

    def tearDown(self):
        self.tmp.cleanup()

    def session(self):
        return self.factory()

    def counts(self):
        with self.session() as session:
            return {
                "clientes": len(list(session.scalars(select(Cliente)))),
                "periodos": len(list(session.scalars(select(PeriodoCertificacion)))),
                "audit": len(list(session.scalars(select(AuditLog)))),
            }

    def test_dry_run_does_not_write(self):
        write_legacy_json(self.legacy_path, sample_payload())

        with self.session() as session:
            result = migrate_legacy_draft(session, legacy_path=self.legacy_path, apply=False)

        self.assertEqual(result.mode, "dry-run")
        self.assertEqual(result.cliente_action, "create")
        self.assertEqual(result.periodo_action, "create")
        self.assertEqual(self.counts(), {"clientes": 0, "periodos": 0, "audit": 0})

    def test_apply_creates_cliente_periodo_and_audit(self):
        payload = sample_payload()
        write_legacy_json(self.legacy_path, payload)

        with self.session() as session:
            result = migrate_legacy_draft(session, legacy_path=self.legacy_path, apply=True)

        self.assertEqual(result.mode, "apply")
        self.assertEqual(result.cliente_action, "create")
        self.assertEqual(result.periodo_action, "create")
        self.assertTrue(result.cliente_id)
        self.assertTrue(result.periodo_id)

        with self.session() as session:
            cliente = session.get(Cliente, result.cliente_id)
            periodo = session.get(PeriodoCertificacion, result.periodo_id)
            audits = list(session.scalars(select(AuditLog).order_by(AuditLog.id)))

        self.assertEqual(cliente.cedula, "361-060491-0000X")
        self.assertEqual(cliente.giro_negocio_id, "comercio_general")
        self.assertIn("Sueldos y Salarios", json.loads(cliente.plantilla_gastos_json))
        self.assertEqual(periodo.estado, "borrador")
        self.assertEqual(periodo.mes_inicial, "2025-01")
        self.assertEqual(periodo.mes_final, "2026-04")
        self.assertEqual(periodo.saldos_iniciales_origen, "legacy_json")
        self.assertEqual(canonical_payload_hash(json.loads(periodo.payload_json)), canonical_payload_hash(payload))
        self.assertTrue(json.loads(periodo.validation_json))
        self.assertTrue(json.loads(periodo.period_blocks_json))
        self.assertEqual([a.action for a in audits], ["legacy_import", "legacy_import"])

    def test_apply_twice_is_idempotent(self):
        write_legacy_json(self.legacy_path, sample_payload())

        with self.session() as session:
            first = migrate_legacy_draft(session, legacy_path=self.legacy_path, apply=True)
        with self.session() as session:
            second = migrate_legacy_draft(session, legacy_path=self.legacy_path, apply=True)

        self.assertEqual(first.cliente_id, second.cliente_id)
        self.assertEqual(first.periodo_id, second.periodo_id)
        self.assertEqual(second.cliente_action, "reuse")
        self.assertEqual(second.periodo_action, "reuse")
        self.assertEqual(self.counts(), {"clientes": 1, "periodos": 1, "audit": 2})

    def test_existing_cliente_is_reused(self):
        write_legacy_json(self.legacy_path, sample_payload())
        with self.session() as session:
            cliente = Cliente(
                nombre_completo="Kitiel Existente",
                cedula="361-060491-0000X",
                nombre_negocio="Negocio existente",
                direccion_negocio="Managua",
                giro_negocio_id="comercio_general",
            )
            session.add(cliente)
            session.commit()
            cliente_id = cliente.id

        with self.session() as session:
            result = migrate_legacy_draft(session, legacy_path=self.legacy_path, apply=True)

        self.assertEqual(result.cliente_id, cliente_id)
        self.assertEqual(result.cliente_action, "reuse")
        self.assertEqual(self.counts()["clientes"], 1)

    def test_same_range_different_payload_fails_without_writing(self):
        payload = sample_payload()
        write_legacy_json(self.legacy_path, payload)
        with self.session() as session:
            migrate_legacy_draft(session, legacy_path=self.legacy_path, apply=True)

        changed = sample_payload()
        changed["income"]["cost_pct"] = 80
        write_legacy_json(self.legacy_path, changed)

        with self.session() as session:
            with self.assertRaisesRegex(RuntimeError, "payload distinto"):
                migrate_legacy_draft(session, legacy_path=self.legacy_path, apply=True)

        self.assertEqual(self.counts(), {"clientes": 1, "periodos": 1, "audit": 2})


if __name__ == "__main__":
    unittest.main()

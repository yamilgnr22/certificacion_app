from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base
from db.seed import seed_giros
from repositories import AuditRepository, ClienteRepository, GiroRepository, PeriodoRepository


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    seed_giros(session)
    session.commit()
    return session


class DbPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.session = make_session()

    def tearDown(self):
        self.session.close()

    def test_seed_giros_creates_active_catalog(self):
        giros = GiroRepository(self.session).list_active()

        self.assertGreaterEqual(len(giros), 5)
        self.assertIn("ferreteria", {g.id for g in giros})
        ferreteria = GiroRepository(self.session).get("ferreteria")
        self.assertIsNotNone(ferreteria)
        self.assertIn("Sueldos y Salarios", json.loads(ferreteria.plantilla_gastos_json))

    def test_cliente_crud_search_and_unique_cedula(self):
        repo = ClienteRepository(self.session)
        cliente = repo.create(
            nombre_completo="Kitiel Rosibel Montiel Gonzalez",
            cedula="361-060491-0000X",
            nombre_negocio="Motonic",
            direccion_negocio="Managua, Altamira",
            giro_negocio_id="comercio_general",
        )
        self.session.commit()

        self.assertEqual(repo.find_by_cedula("361-060491-0000X").id, cliente.id)
        self.assertEqual(repo.search("Kitiel")[0].id, cliente.id)
        self.assertEqual(repo.search("", giro_id="comercio_general")[0].id, cliente.id)

        self.assertTrue(repo.has_active_cedula("361-060491-0000X"))
        self.assertTrue(repo.has_active_cedula("361-060491-0000X", exclude_id="otro-id"))
        self.assertFalse(repo.has_active_cedula("361-060491-0000X", exclude_id=cliente.id))

    def test_periodo_links_to_cliente_and_latest_finalized(self):
        cliente = ClienteRepository(self.session).create(
            nombre_completo="Cliente Periodo",
            cedula="001-010101-0000A",
            nombre_negocio="Negocio",
            direccion_negocio="Managua",
            giro_negocio_id="ferreteria",
        )
        repo = PeriodoRepository(self.session)
        repo.create(
            cliente_id=cliente.id,
            periodo_meses=12,
            mes_inicial="2025-01",
            mes_final="2025-12",
            estado="finalizado",
            tasa_cambio=36.6243,
            saldos_iniciales_origen="manual",
            payload_json=json.dumps({"period": {"start_month": "2025-01", "end_month": "2025-12"}}),
            saldos_finales_json=json.dumps({"Efectivo y Equivalentes de Efectivo": 1000}),
        )
        current = repo.create(
            cliente_id=cliente.id,
            periodo_meses=4,
            mes_inicial="2026-01",
            mes_final="2026-04",
            estado="borrador",
            tasa_cambio=36.6243,
            saldos_iniciales_origen="rollforward",
            payload_json=json.dumps({"period": {"start_month": "2026-01", "end_month": "2026-04"}}),
        )
        self.session.commit()

        periods = repo.list_for_cliente(cliente.id)
        latest = repo.latest_finalized_for_cliente(cliente.id)

        self.assertEqual(periods[0].id, current.id)
        self.assertEqual(latest.mes_final, "2025-12")

    def test_audit_log_chains_entries(self):
        audit = AuditRepository(self.session)
        first = audit.append(
            cpa_user="system",
            entity_type="cliente",
            entity_id="cliente-1",
            action="create",
            summary="Cliente creado",
        )
        second = audit.append(
            cpa_user="system",
            entity_type="cliente",
            entity_id="cliente-1",
            action="update",
            summary="Cliente actualizado",
            metadata={"field": "telefono"},
        )
        self.session.commit()

        entries = audit.list_for_entity("cliente", "cliente-1")

        self.assertEqual(entries[0].id, second.id)
        self.assertEqual(second.prev_entry_hash, AuditRepository.entry_hash(first))
        self.assertEqual(json.loads(second.metadata_json), {"field": "telefono"})


if __name__ == "__main__":
    unittest.main()

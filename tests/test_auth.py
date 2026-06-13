from __future__ import annotations

import os
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import web_server
from db.models import AuditLog, Base
from db.seed import seed_giros

TOKEN = "s3cr3t-token"


class AuthTest(unittest.TestCase):
    """F6-T1: con token configurado, /api/* exige Authorization: Bearer."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        with self.factory() as session:
            seed_giros(session)
            session.commit()
        self.old = {k: web_server.app.config.get(k) for k in ("DB_ENGINE", "DB_REQUIRE_ALEMBIC", "AUTH_TOKEN")}
        web_server.app.config["DB_ENGINE"] = self.engine
        web_server.app.config["DB_REQUIRE_ALEMBIC"] = False
        web_server.app.config["AUTH_TOKEN"] = TOKEN
        self._old_cpa_user_env = os.environ.pop("CERTAPP_CPA_USER", None)
        self.client = web_server.app.test_client()

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                web_server.app.config.pop(key, None)
            else:
                web_server.app.config[key] = value
        if self._old_cpa_user_env is not None:
            os.environ["CERTAPP_CPA_USER"] = self._old_cpa_user_env

    def test_api_without_token_is_401(self):
        resp = self.client.get("/api/giros")
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.get_json()["ok"])

    def test_api_with_wrong_token_is_401(self):
        resp = self.client.get("/api/giros", headers={"Authorization": "Bearer nope"})
        self.assertEqual(resp.status_code, 401)

    def test_api_with_token_is_200(self):
        resp = self.client.get("/api/giros", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_index_loads_without_token(self):
        # La pantalla (con su login) debe cargar sin token.
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_audit_user_comes_from_session_not_header(self):
        # Con auth activa, el X-CPA-User del cliente se ignora: el audit log
        # registra la identidad configurada del CPA, no lo que mande el cliente.
        headers = {"Authorization": f"Bearer {TOKEN}", "X-CPA-User": "intruso"}
        resp = self.client.post(
            "/api/clientes",
            json={
                "nombre_completo": "Cliente Auth", "cedula": "001-010101-0000A",
                "nombre_negocio": "Negocio", "direccion_negocio": "Managua",
                "giro_negocio_id": "ferreteria",
            },
            headers=headers,
        )
        self.assertEqual(resp.status_code, 201, resp.get_json())
        with self.factory() as session:
            entries = list(session.scalars(select(AuditLog)))
        self.assertTrue(entries)
        self.assertTrue(all(e.cpa_user == "cpa" for e in entries))
        self.assertFalse(any(e.cpa_user == "intruso" for e in entries))


class NoAuthByDefaultTest(unittest.TestCase):
    """Sin token configurado (dev/tests), /api/* funciona sin Authorization."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        with self.factory() as session:
            seed_giros(session)
            session.commit()
        self.old = {k: web_server.app.config.get(k) for k in ("DB_ENGINE", "DB_REQUIRE_ALEMBIC")}
        web_server.app.config["DB_ENGINE"] = self.engine
        web_server.app.config["DB_REQUIRE_ALEMBIC"] = False
        web_server.app.config.pop("AUTH_TOKEN", None)
        self.client = web_server.app.test_client()

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                web_server.app.config.pop(key, None)
            else:
                web_server.app.config[key] = value

    def test_api_open_without_token(self):
        resp = self.client.get("/api/giros")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()

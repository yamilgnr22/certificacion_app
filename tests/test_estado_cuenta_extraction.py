"""Tests de la extraccion de cuentas bancarias desde estados de cuenta.

El LLM se simula con provider fake; lo que se prueba es la normalizacion
determinista y el contrato del endpoint. Datos SINTETICOS (sin PII real).
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from services.estado_cuenta_extraction import (
    EstadoCuentaError,
    _hint_ultima_transaccion,
    _normalizar,
    _reparar_anios_partidos,
    extraer_cuentas_de_texto,
)


class FakeProvider:
    name = "fake"
    last_retries = 0

    def __init__(self, respuesta):
        self.respuesta = respuesta

    def complete_json(self, *, system_prompt, user_prompt, schema=None):
        return self.respuesta


_RESPUESTA = {
    "banco": "LAFISE",
    "titular": "CLIENTE DE PRUEBA",
    "cuentas": [
        {"tipo": "Cuenta Corriente", "moneda": "NIO", "numero": "NI63BCCE0134066187",
         "saldo_final": "946.00", "fecha_corte": "30/04/2026", "confianza": "alta"},
        {"tipo": "Cuenta de Ahorro", "moneda": "USD", "numero": "106207223",
         "saldo_final": 3742.55, "fecha_corte": "2026-04-30", "confianza": "media"},
        # sin numero ni saldo -> se descarta
        {"tipo": "Cuenta", "moneda": "NIO", "numero": "", "saldo_final": 0},
    ],
}


class NormalizacionTest(unittest.TestCase):
    def test_cuentas_normalizadas(self):
        cuentas = _normalizar(_RESPUESTA)
        self.assertEqual(len(cuentas), 2)
        c1, c2 = cuentas
        self.assertEqual(c1["banco"], "LAFISE")
        self.assertEqual(c1["moneda"], "NIO")
        self.assertEqual(c1["saldo"], 946.0)
        self.assertEqual(c1["fecha_corte"], "2026-04-30")  # DD/MM/AAAA -> ISO
        self.assertEqual(c2["moneda"], "USD")
        self.assertEqual(c2["saldo"], 3742.55)
        self.assertEqual(c2["fecha_corte"], "2026-04-30")

    def test_flujo_con_provider_fake(self):
        r = extraer_cuentas_de_texto("texto del estado", provider=FakeProvider(_RESPUESTA))
        self.assertTrue(r["ok"])
        self.assertEqual(r["banco"], "LAFISE")
        self.assertEqual(len(r["cuentas"]), 2)


class ReparacionYHintTest(unittest.TestCase):
    """Piezas deterministas calibradas con estados reales de los 4 bancos
    (Banpro clasico/divisa, LAFISE, BAC) — jul 2026."""

    def test_repara_anio_partido_estilo_lafise(self):
        texto = ("31/DIC/202 5059263 Pagos 5,000.00 199,402.94 E2\n"
                 "5\n"
                 "30/DIC/202 5046795 Transferencia entre 500.00 204,402.94 TB\n"
                 "5 cuentas")
        rep = _reparar_anios_partidos(texto)
        lineas = rep.split("\n")
        self.assertTrue(lineas[0].startswith("31/DIC/2025 "))
        self.assertTrue(lineas[1].startswith("30/DIC/2025 "))
        self.assertEqual(lineas[2], "cuentas")  # resto de descripcion sobrevive

    def test_anio_completo_no_se_toca(self):
        texto = "31/12/2025 301401342 TF 500,000.00 0.00 22,544.79\n5,000.00"
        self.assertEqual(_reparar_anios_partidos(texto), texto)

    def test_hint_lista_descendente_lafise(self):
        # Descendente: la fila mas reciente es la PRIMERA (misma fecha ->
        # tambien la primera, que es la ultima operacion del dia).
        texto = ("Fecha Numero Descripcion Debito Credito Saldo\n"
                 "31/DIC/2025 111 Pagos 5,000.00 199,402.94 E2\n"
                 "31/DIC/2025 222 Pago Luis 18,312.00 204,402.94 E5\n"
                 "16/ENE/2025 333 TEXTO 4,350.00 341,076.03 TB\n")
        hint = _hint_ultima_transaccion(texto)
        self.assertIn("199,402.94", hint)

    def test_hint_lista_ascendente_bac(self):
        # Ascendente: la fila mas reciente es la ULTIMA del dia mas reciente.
        texto = ("02/01/2025 301002201 DP 0.00 2,470.00 55,693.99\n"
                 "31/12/2025 301401342 TF 500,000.00 0.00 22,544.79\n"
                 "31/12/2025 301405648 TF 0.00 1,856.00 24,400.79\n")
        hint = _hint_ultima_transaccion(texto)
        self.assertIn("24,400.79", hint)

    def test_hint_sin_transacciones(self):
        self.assertIsNone(_hint_ultima_transaccion("Saldo Final: US$ 1,200.01"))


class EndpointTest(unittest.TestCase):
    def setUp(self):
        import web_server

        web_server.app.config["TESTING"] = True
        self.client = web_server.app.test_client()

    def test_sin_archivo_400(self):
        r = self.client.post("/api/motor/v2/cuentas/extract", data={})
        self.assertEqual(r.status_code, 400)

    def test_feliz_simulado(self):
        with patch("services.estado_cuenta_extraction.procesar_estado_cuenta") as m:
            m.return_value = {"ok": True, "banco": "LAFISE", "titular": "X",
                              "cuentas": _normalizar(_RESPUESTA), "llm_retries": 0}
            r = self.client.post(
                "/api/motor/v2/cuentas/extract",
                data={"archivo": (io.BytesIO(b"%PDF-1.4 fake"), "estado.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(len(r.get_json()["cuentas"]), 2)

    def test_error_extraccion_422(self):
        with patch("services.estado_cuenta_extraction.procesar_estado_cuenta",
                   side_effect=EstadoCuentaError("Formato no soportado")):
            r = self.client.post(
                "/api/motor/v2/cuentas/extract",
                data={"archivo": (io.BytesIO(b"x"), "estado.txt")},
                content_type="multipart/form-data",
            )
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()

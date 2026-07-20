"""Tests de la extraccion de deudas desde reportes TransUnion (PDF).

La llamada al LLM se simula con un provider fake (mismo patron que el
asistente); lo que se prueba en serio es la normalizacion determinista
(fechas, montos, estrategia, dedupe) y el contrato del endpoint.
Los datos son SINTETICOS (nunca PII real en fixtures).
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from services.deuda_extraction import (
    DeudaExtractionError,
    _estrategia,
    _fecha_iso,
    _normalizar_siboif,
    _normalizar_tuca,
    _num,
    detectar_fuente,
    extraer_deudas,
    extraer_texto_pdf,
)

# Alias de compat: los tests TUCA existentes usaban _normalizar_deudas.
_normalizar_deudas = _normalizar_tuca


class FakeProvider:
    name = "fake"
    last_retries = 0

    def __init__(self, respuesta):
        self.respuesta = respuesta
        self.prompts = []

    def complete_json(self, *, system_prompt, user_prompt, schema=None):
        self.prompts.append(user_prompt)
        return self.respuesta


_RESPUESTA_TU = {
    "titular": {"nombre": "CLIENTE DE PRUEBA", "cedula": "000-000000-0000X"},
    "fecha_reporte": "18/07/2026",
    "deudas": [
        {
            "numero": "9935", "entidad": "BANCO DEMO", "tipo_credito": "TARJETAS DE CRÉDITO INTERNACIONAL",
            "moneda": "NIO", "limite": "36,624.30", "saldo": "37,150.96", "cuota": "2,755.00",
            "fecha_otorgamiento": "28/12/2022", "fecha_vencimiento": "--/--/----",
            "fecha_actualizado": "05/2026", "confianza": "alta",
        },
        {
            "numero": "0200", "entidad": "BANCO DEMO", "tipo_credito": "CARTERA DE CONSUMO",
            "moneda": "USD", "limite": 1300, "saldo": 67.22, "cuota": 70,
            "fecha_otorgamiento": "02/07/2024", "fecha_vencimiento": "22/06/2026",
            "fecha_actualizado": "05/2026", "confianza": "media",
        },
        # Duplicado (PDF con reporte repetido): debe deduplicarse
        {
            "numero": "9935", "entidad": "BANCO DEMO", "tipo_credito": "TARJETAS DE CRÉDITO INTERNACIONAL",
            "moneda": "NIO", "limite": "36,624.30", "saldo": "37,150.96", "cuota": "2,755.00",
            "fecha_otorgamiento": "28/12/2022", "fecha_vencimiento": "--/--/----",
            "fecha_actualizado": "05/2026", "confianza": "alta",
        },
    ],
}


class NormalizacionTest(unittest.TestCase):
    def test_fechas(self):
        self.assertEqual(_fecha_iso("28/12/2022"), "2022-12-28")
        self.assertEqual(_fecha_iso("05/2026"), "2026-05-01")
        self.assertEqual(_fecha_iso("2026-05-01"), "2026-05-01")
        self.assertEqual(_fecha_iso("2026-05"), "2026-05-01")
        self.assertIsNone(_fecha_iso("--/--/----"))
        self.assertIsNone(_fecha_iso(""))
        self.assertIsNone(_fecha_iso(None))
        self.assertIsNone(_fecha_iso("basura"))

    def test_montos(self):
        self.assertEqual(_num("36,624.30"), 36624.30)
        self.assertEqual(_num(1300), 1300.0)
        self.assertEqual(_num("-"), 0.0)
        self.assertEqual(_num(None), 0.0)
        self.assertEqual(_num("no-numero"), 0.0)

    def test_estrategia_por_tipo(self):
        self.assertEqual(_estrategia("TARJETAS DE CRÉDITO INTERNACIONAL"), "revolving")
        self.assertEqual(_estrategia("CARTERA ROTATIVA"), "revolving")
        self.assertEqual(_estrategia("CARTERA DE CONSUMO"), "amortizable")
        self.assertEqual(_estrategia("CARTERA DE VEHICULOS"), "amortizable")
        self.assertEqual(_estrategia("EXTRAFINANCIAMIENTO"), "amortizable")

    def test_normalizar_dedupe_y_campos(self):
        deudas = _normalizar_deudas(_RESPUESTA_TU)
        self.assertEqual(len(deudas), 2)  # el duplicado se fue
        tarjeta = deudas[0]
        self.assertEqual(tarjeta["numero"], "9935")
        self.assertEqual(tarjeta["estrategia"], "revolving")
        self.assertEqual(tarjeta["moneda"], "NIO")
        self.assertEqual(tarjeta["valor_inicial"], 36624.30)
        self.assertEqual(tarjeta["saldo_reportado"], 37150.96)
        self.assertEqual(tarjeta["cuota"], 2755.00)
        self.assertEqual(tarjeta["fecha_otorgamiento"], "2022-12-28")
        self.assertIsNone(tarjeta["fecha_vencimiento"])
        self.assertEqual(tarjeta["fecha_actualizado"], "2026-05-01")
        self.assertTrue(tarjeta["incluir_en_er"])
        consumo = deudas[1]
        self.assertEqual(consumo["estrategia"], "amortizable")
        self.assertEqual(consumo["moneda"], "USD")
        self.assertEqual(consumo["fecha_vencimiento"], "2026-06-22")

    def test_sin_numero_se_omite(self):
        deudas = _normalizar_deudas({"deudas": [{"entidad": "X", "saldo": 5}]})
        self.assertEqual(deudas, [])

    def test_linea_de_moneda_sin_movimiento_se_filtra(self):
        # Caso real (Lidia): tarjeta 9935 opera en NIO; su lado USD tiene
        # limite pero saldo 0 y cuota 0 -> ruido, se filtra.
        raw = {"deudas": [
            {"numero": "9935", "entidad": "B", "tipo_credito": "TARJETAS", "moneda": "NIO",
             "limite": 36624.30, "saldo": 37150.96, "cuota": 2755},
            {"numero": "9935", "entidad": "B", "tipo_credito": "TARJETAS", "moneda": "USD",
             "limite": 1000, "saldo": 0, "cuota": 0},
        ]}
        deudas = _normalizar_deudas(raw)
        self.assertEqual([(d["numero"], d["moneda"]) for d in deudas], [("9935", "NIO")])

    def test_obligacion_con_unica_linea_vacia_se_conserva(self):
        raw = {"deudas": [
            {"numero": "0001", "entidad": "B", "tipo_credito": "CARTERA DE CONSUMO",
             "moneda": "NIO", "limite": 5000, "saldo": 0, "cuota": 0},
        ]}
        deudas = _normalizar_deudas(raw)
        self.assertEqual(len(deudas), 1)  # no desaparece del panel


class ExtraerDeudasTest(unittest.TestCase):
    def test_flujo_completo_con_provider_fake(self):
        prov = FakeProvider(_RESPUESTA_TU)
        r = extraer_deudas("texto del reporte...", provider=prov)
        self.assertEqual(r["titular"]["cedula"], "000-000000-0000X")
        self.assertEqual(r["fecha_reporte"], "2026-07-18")
        self.assertEqual(len(r["deudas"]), 2)
        self.assertIn("texto del reporte", prov.prompts[0])

    def test_deudas_compatibles_con_deuda_input(self):
        # Las filas extraidas deben poder entrar al motor tal cual
        from motor.json_io import _deuda

        prov = FakeProvider(_RESPUESTA_TU)
        for d in extraer_deudas("x", provider=prov)["deudas"]:
            deuda = _deuda(d)
            self.assertEqual(deuda.numero, d["numero"])
            self.assertEqual(deuda.saldo_reportado, d["saldo_reportado"])


class ExtraerTextoPdfTest(unittest.TestCase):
    def test_stream_no_pdf_da_error_claro(self):
        with self.assertRaises(DeudaExtractionError):
            extraer_texto_pdf(io.BytesIO(b"no soy un pdf"))


class DetectarFuenteTest(unittest.TestCase):
    def test_tuca(self):
        self.assertEqual(detectar_fuente("Reporte de Historial Crediticio TransUnion"), "tuca")

    def test_siboif(self):
        self.assertEqual(detectar_fuente("REPORTE SIBOIF\nInformacion Personal"), "siboif")

    def test_desconocido(self):
        self.assertIsNone(detectar_fuente("cualquier otra cosa"))


class NormalizarSiboifTest(unittest.TestCase):
    _RAW = {
        "fuente": "siboif",
        "titular": {"nombre": "CLIENTE SIBOIF", "cedula": "0011409980002L"},
        "resumen": {"saldo_general": 66261.58, "interes_general": 241.13, "cuota_mensual_total": 4238.21},
        "deudas": [
            {"tipo_credito": "Consumo", "destino": "Tarjetas de Credito",
             "moneda": "Nacional con Mantenimiento de Valor", "cant_instituciones": 3,
             "saldo": "55,379.50", "interes_corriente": "241.13", "confianza": "alta"},
            {"tipo_credito": "Consumo", "destino": "Tarjetas de Credito",
             "moneda": "Extranjera (US$ Dolares)", "cant_instituciones": 2,
             "saldo": "8,382.20", "interes_corriente": 0, "confianza": "media"},
            # fila sin saldo ni interes -> se filtra
            {"tipo_credito": "Consumo", "destino": "Personales", "moneda": "NIO",
             "cant_instituciones": 1, "saldo": 0, "interes_corriente": 0},
        ],
    }

    def test_agregado_a_filas_de_deuda(self):
        deudas = _normalizar_siboif(self._RAW)
        self.assertEqual(len(deudas), 2)  # la vacia se filtro
        d0 = deudas[0]
        self.assertEqual(d0["saldo_reportado"], 55379.50)
        self.assertEqual(d0["moneda"], "NIO")
        self.assertEqual(d0["estrategia"], "revolving")  # tarjetas
        self.assertEqual(d0["numero"], "")        # SIBOIF no lo trae
        self.assertEqual(d0["cuota"], 0.0)        # a completar
        self.assertIn("institucion", d0["entidad"])
        self.assertIn("completa", d0["notas"].lower())
        self.assertEqual(deudas[1]["moneda"], "USD")

    def test_filas_compatibles_con_deuda_input(self):
        from motor.json_io import _deuda

        for d in _normalizar_siboif(self._RAW):
            d = {**d, "numero": d["numero"] or "SIBOIF-1"}  # el CPA pone numero
            deuda = _deuda(d)
            self.assertEqual(deuda.saldo_reportado, d["saldo_reportado"])
            self.assertEqual(deuda.estrategia, d["estrategia"])


class EndpointTest(unittest.TestCase):
    def setUp(self):
        import web_server

        web_server.app.config["TESTING"] = True
        self.client = web_server.app.test_client()

    def test_sin_archivo_400(self):
        r = self.client.post("/api/motor/v2/deudas/extract", data={})
        self.assertEqual(r.status_code, 400)

    def test_formato_no_soportado_422(self):
        with patch("services.deuda_extraction.procesar_reporte",
                   side_effect=DeudaExtractionError("Formato no soportado")):
            r = self.client.post(
                "/api/motor/v2/deudas/extract",
                data={"archivo": (io.BytesIO(b"x"), "archivo.txt")},
                content_type="multipart/form-data",
            )
        self.assertEqual(r.status_code, 422)

    def test_feliz_tuca_pdf(self):
        with patch("services.deuda_extraction.procesar_reporte") as m:
            m.return_value = {
                "ok": True, "fuente": "tuca",
                "titular": {"nombre": "CLIENTE DE PRUEBA", "cedula": "000-000000-0000X"},
                "fecha_reporte": "2026-07-18",
                "deudas": _normalizar_tuca(_RESPUESTA_TU),
                "llm_retries": 0,
            }
            r = self.client.post(
                "/api/motor/v2/deudas/extract",
                data={"archivo": (io.BytesIO(b"%PDF-1.4 fake"), "TUReport.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["fuente"], "tuca")
        self.assertEqual(len(j["deudas"]), 2)

    def test_feliz_siboif_imagen(self):
        with patch("services.deuda_extraction.procesar_reporte") as m:
            m.return_value = {
                "ok": True, "fuente": "siboif",
                "titular": {"nombre": "CLIENTE SIBOIF", "cedula": "0011409980002L"},
                "fecha_reporte": None,
                "deudas": _normalizar_siboif(NormalizarSiboifTest._RAW),
                "resumen": {"saldo_general": 66261.58, "interes_general": 241.13, "cuota_mensual_total": 4238.21},
                "llm_retries": 0,
            }
            r = self.client.post(
                "/api/motor/v2/deudas/extract",
                data={"archivo": (io.BytesIO(b"\x89PNG fake"), "siboif.png")},
                content_type="multipart/form-data",
            )
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual(j["fuente"], "siboif")
        self.assertEqual(j["resumen"]["cuota_mensual_total"], 4238.21)

    def test_error_extraccion_422(self):
        with patch("services.deuda_extraction.procesar_reporte",
                   side_effect=DeudaExtractionError("El PDF no tiene texto extraible")):
            r = self.client.post(
                "/api/motor/v2/deudas/extract",
                data={"archivo": (io.BytesIO(b"%PDF-1.4 fake"), "scan.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(r.status_code, 422)
        self.assertIn("texto extraible", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()

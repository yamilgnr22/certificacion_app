"""Bandas de oscilacion configurables (motor.inputs.Bandas).

Lo que se garantiza:
  - La banda cambia CUANTO se mueve la cuenta mes a mes, nunca el saldo del
    corte: la cifra dura (reporte de deuda / balance final) sigue mandando.
  - Banda 0 = linea plana (sin oscilacion).
  - El JSON de la UI llega hasta el calculo; sin bloque `bandas` valen los
    defaults historicos (20/10/10/10).
"""

from __future__ import annotations

import unittest
from datetime import date

from motor import (
    Bandas,
    DatosCliente,
    DeudaInput,
    ER_LineaMes,
    ESF_Saldos,
    InputsTipoA,
    PeriodoSpec,
    certificar_tipo_a,
)
from motor.json_io import inputs_from_json

MESES = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def _datos() -> DatosCliente:
    return DatosCliente(
        nombre_completo="Ana Maria Lopez Ruiz",
        cedula="001-010190-0001A",
        domicilio="Managua",
        contacto="+505 8888 8888",
        regimen="Cuota Fija",
        matricula="RNVD-000001",
        direccion_negocio="Managua",
        giro="Pulperia",
        antiguedad="05 anios",
        empleados=1,
        estado_civil="soltera",
        profesion="Comerciante",
        sexo="Femenino",
        banco="BAC",
        fecha_certificacion=date(2026, 7, 1),
    )


def _tarjeta() -> DeudaInput:
    return DeudaInput(
        numero="4111",
        entidad="BAC",
        tipo_credito="Tarjeta de Credito",
        estrategia="revolving",
        moneda="NIO",
        valor_inicial=50_000.0,
        saldo_reportado=35_526.0,
        cuota=2_000.0,
        fecha_otorgamiento=date(2020, 1, 1),
        fecha_actualizado=date(2026, 6, 30),
    )


def _inputs(bandas: Bandas, con_tarjeta: bool = True) -> InputsTipoA:
    er = [
        ER_LineaMes(mes=m, ingresos=100_000.0, costo_ventas=60_000.0, sueldos_salarios=10_000.0)
        for m in MESES
    ]
    si = ESF_Saldos(efectivo=400_000.0, inventarios=200_000.0, proveedores=80_000.0)
    sf = ESF_Saldos(efectivo=0.0, inventarios=260_000.0, proveedores=95_000.0)
    return InputsTipoA(
        periodo=PeriodoSpec(tipo="A", mes_inicial=MESES[0], mes_final=MESES[-1], tasa_cambio=36.6243),
        datos=_datos(),
        er_mensual=er,
        saldos_iniciales=si,
        saldos_finales=sf,
        deudas=[_tarjeta()] if con_tarjeta else [],
        bandas=bandas,
    )


def _serie(modelo, cuenta: str) -> list[float]:
    return [getattr(e, cuenta) for e in modelo.esf.meses]


def _amplitud(serie: list[float]) -> float:
    """Dispersion relativa de los meses INTERMEDIOS (el corte esta anclado)."""
    interior = serie[:-1]
    base = sum(interior) / len(interior)
    return (max(interior) - min(interior)) / base if base else 0.0


class BandasDataclassTest(unittest.TestCase):
    def test_defaults_historicos(self):
        b = Bandas()
        self.assertEqual(
            (b.tarjetas_pct, b.creditos_pct, b.inventario_pct, b.proveedores_pct),
            (20.0, 10.0, 10.0, 10.0),
        )

    def test_rechaza_fuera_de_rango(self):
        for kw in ({"tarjetas_pct": 51.0}, {"inventario_pct": -1.0}):
            with self.assertRaises(ValueError):
                Bandas(**kw)


class BandaCambiaOscilacionTest(unittest.TestCase):
    """Mas banda = mas movimiento intermedio; el corte no se mueve."""

    def setUp(self):
        self.angosto = certificar_tipo_a(_inputs(Bandas(tarjetas_pct=2.0, inventario_pct=2.0)))
        self.ancho = certificar_tipo_a(_inputs(Bandas(tarjetas_pct=40.0, inventario_pct=30.0)))

    def test_tarjetas_oscilan_mas_con_banda_ancha(self):
        self.assertGreater(
            _amplitud(_serie(self.ancho, "tarjetas_credito")),
            _amplitud(_serie(self.angosto, "tarjetas_credito")),
        )

    def test_inventario_oscila_mas_con_banda_ancha(self):
        self.assertGreater(
            _amplitud(_serie(self.ancho, "inventarios")),
            _amplitud(_serie(self.angosto, "inventarios")),
        )

    def test_el_corte_no_depende_de_la_banda(self):
        for cuenta in ("tarjetas_credito", "inventarios", "proveedores"):
            self.assertAlmostEqual(
                getattr(self.ancho.esf.corte(), cuenta),
                getattr(self.angosto.esf.corte(), cuenta),
                delta=1.0,
                msg=f"la banda movio el corte de {cuenta}",
            )

    def test_tarjeta_ancla_en_el_saldo_reportado(self):
        for modelo in (self.angosto, self.ancho):
            self.assertAlmostEqual(modelo.esf.corte().tarjetas_credito, 35_526.0, delta=1.0)


class BandaCeroEsPlanaTest(unittest.TestCase):
    def test_tarjeta_sin_banda_no_se_mueve(self):
        modelo = certificar_tipo_a(_inputs(Bandas(tarjetas_pct=0.0)))
        serie = _serie(modelo, "tarjetas_credito")
        self.assertEqual(len(set(round(v) for v in serie)), 1, f"deberia ser plana: {serie}")


class BandasDesdeJSONTest(unittest.TestCase):
    def _body(self, bandas=None) -> dict:
        body = {
            "periodo": {"tipo": "A", "mes_inicial": MESES[0], "mes_final": MESES[-1], "tasa_cambio": 36.6243},
            "datos": {"nombre_completo": "Ana Maria Lopez Ruiz", "cedula": "001-010190-0001A"},
            "er_mensual": [{"mes": m, "ingresos": 100_000} for m in MESES],
            "saldos_iniciales": {"efectivo": 400_000},
            "saldos_finales": {"efectivo": 400_000},
            "deudas": [],
        }
        if bandas is not None:
            body["bandas"] = bandas
        return body

    def test_sin_bloque_usa_defaults(self):
        self.assertEqual(inputs_from_json(self._body()).bandas, Bandas())

    def test_bloque_parcial_completa_con_defaults(self):
        b = inputs_from_json(self._body({"tarjetas_pct": 5})).bandas
        self.assertEqual((b.tarjetas_pct, b.creditos_pct, b.inventario_pct, b.proveedores_pct),
                         (5.0, 10.0, 10.0, 10.0))

    def test_valor_invalido_revienta_con_valueerror(self):
        # El endpoint traduce ValueError a 400 con el mensaje visible.
        with self.assertRaises(ValueError):
            inputs_from_json(self._body({"proveedores_pct": 80}))


if __name__ == "__main__":
    unittest.main()

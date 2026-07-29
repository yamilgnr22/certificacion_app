"""Test de integracion del motor Tipo A con un caso sintetico consistente.

Construye inputs simples donde el balance cierra por construccion, corre
certificar_tipo_a y verifica que los 9 invariantes pasen (validacion.ok) y
que el ESF de corte pegue a los saldos finales dados.

Esto valida el motor headless completo sin depender del Excel de Gloria
(ese es el golden test separado, cifra a cifra).
"""

from __future__ import annotations

import unittest
from datetime import date

from motor import (
    DatosCliente,
    ER_LineaMes,
    ESF_Saldos,
    InputsTipoA,
    PeriodoSpec,
    certificar_tipo_a,
)


def _datos() -> DatosCliente:
    return DatosCliente(
        nombre_completo="Gloria Elena Guillen Robinson",
        cedula="601-140998-0002L",
        domicilio="Residencial Casa Real",
        contacto="+505 8510 8735",
        regimen="Cuota Fija",
        matricula="RNVD-118495",
        direccion_negocio="Bolonia",
        giro="Servicios de envio y paqueteria",
        antiguedad="05 anios",
        empleados=1,
        estado_civil="soltera",
        profesion="Ingeniera Industrial",
        sexo="Femenino",
        banco="FICOHSA",
        fecha_certificacion=date(2026, 6, 5),
    )


def _er_6_meses() -> list[ER_LineaMes]:
    # Sin deudas: gastos financieros = 0. Todo contado.
    meses = ["2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
    lineas = []
    for m in meses:
        lineas.append(ER_LineaMes(
            mes=m,
            ingresos=100_000.0,
            costo_ventas=60_000.0,
            sueldos_salarios=10_000.0,
            renta=5_000.0,
            gasto_depreciacion=1_000.0,
        ))
    return lineas


class MotorTipoASinDeudaTest(unittest.TestCase):
    def setUp(self):
        er = _er_6_meses()
        # Utilidad neta mensual = 100k - 60k - (10k+5k+1k) = 24k
        # 6 meses => utilidad acumulada 144k
        # Caja: cobros 100k - pagos (60k+15k) = +25k/mes (depr no sale) => +150k
        # Saldos iniciales en cero salvo lo minimo.
        self.si = ESF_Saldos(
            efectivo=0.0,
            cuentas_por_cobrar=0.0,
            inventarios=0.0,
            depreciacion_acumulada=0.0,
            resultados_acumulados=0.0,
        )
        # Final esperado tras 6 meses:
        #   efectivo = 150,000
        #   depr_acumulada = -6,000
        #   resultados_ejercicio = 144,000
        #   capital apertura = 0 (activos0 - pasivos0 = 0)
        self.sf = ESF_Saldos(
            efectivo=150_000.0,
            cuentas_por_cobrar=0.0,
            inventarios=0.0,
            depreciacion_acumulada=-6_000.0,
            resultados_acumulados=0.0,
        )
        self.inputs = InputsTipoA(
            periodo=PeriodoSpec(tipo="A", mes_inicial="2025-12", mes_final="2026-05", tasa_cambio=36.6243),
            datos=_datos(),
            er_mensual=er,
            saldos_iniciales=self.si,
            saldos_finales=self.sf,
            deudas=[],
        )
        self.modelo = certificar_tipo_a(self.inputs)

    def test_validacion_ok(self):
        errores = [h.mensaje for h in self.modelo.validacion.errores]
        self.assertTrue(self.modelo.ok, f"Errores: {errores}")

    def test_caja_final_150k(self):
        self.assertAlmostEqual(self.modelo.esf.corte().efectivo, 150_000.0, places=2)

    def test_utilidad_acumulada_144k(self):
        self.assertAlmostEqual(self.modelo.esf.corte().resultados_ejercicio, 144_000.0, places=2)

    def test_depr_acumulada_menos_6k(self):
        self.assertAlmostEqual(self.modelo.esf.corte().depreciacion_acumulada, -6_000.0, places=2)

    def test_capital_apertura_cero(self):
        self.assertAlmostEqual(self.modelo.esf.capital_apertura, 0.0, places=2)

    def test_balance_cuadra_todos_los_meses(self):
        for e in self.modelo.esf.meses:
            self.assertLessEqual(abs(e.diferencia), 1.0, f"Mes {e.mes} descuadra {e.diferencia}")

    def test_er_renombra_utilidad_bruta(self):
        descripciones = self.modelo.df_er["Descripcion"].tolist()
        self.assertIn("(=) Utilidad Bruta", descripciones)
        self.assertNotIn("(=) Ingresos Brutos", descripciones)

    def test_certificacion_ingresos_brutos_es_total_ingresos(self):
        # Ingresos Brutos DOCX = total Ingresos = 600,000 (no la utilidad bruta 240,000)
        cert = self.modelo.df_certificacion
        fila = cert[cert["Descripcion"] == "Ingresos Brutos"]
        self.assertAlmostEqual(float(fila["Datos"].iloc[0]), 600_000.0, places=2)

    def test_certificacion_utilidad_periodo(self):
        cert = self.modelo.df_certificacion
        fila = cert[cert["Descripcion"] == "Utilidad del Período"]
        self.assertAlmostEqual(float(fila["Datos"].iloc[0]), 144_000.0, places=2)


class MotorTipoACapitalNoCuadraTest(unittest.TestCase):
    def test_capital_enviado_incoherente_es_bloqueante(self):
        from motor.esf import ESFError

        si = ESF_Saldos(efectivo=100_000.0, capital=999.0)  # activos0=100k, pasivos0=0 => cap debe ser 100k
        inputs = InputsTipoA(
            periodo=PeriodoSpec(tipo="A", mes_inicial="2026-01", mes_final="2026-01", tasa_cambio=36.6),
            datos=_datos(),
            er_mensual=[ER_LineaMes(mes="2026-01", ingresos=0.0)],
            saldos_iniciales=si,
            saldos_finales=ESF_Saldos(efectivo=100_000.0),
        )
        with self.assertRaises(ESFError):
            certificar_tipo_a(inputs)


if __name__ == "__main__":
    unittest.main()

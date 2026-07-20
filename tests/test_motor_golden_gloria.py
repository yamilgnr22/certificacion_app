"""Golden test cifra a cifra contra el Excel real de Gloria (Tipo A, 6 meses).

Reconstruye los inputs de Gloria (ER mensual, saldos iniciales, tarjeta
revolving con saldos mensuales) y verifica que el motor reproduzca:
  - ER: ingresos, utilidad neta por mes, acumulados
  - Caja mensual (Mov) exacta
  - ESF de corte: efectivo, PPE, tarjetas, capital, resultados, total activos
  - Los 9 invariantes (validacion.ok)

Fuente: Gloria Elena Guillen Robinson.xlsx (hojas ER, Mov, ESF_Corte).
Los planes 6532/8797 son documentales (incluir_en_er=False), no se modelan;
la unica deuda activa es la tarjeta de credito (revolving).
"""

from __future__ import annotations

import unittest
from datetime import date

from motor import DatosCliente, ER_LineaMes, ESF_Saldos, InputsTipoA, PeriodoSpec, certificar_tipo_a
from motor.inputs import DeudaInput


MESES = ["2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
TC = 36.6243

# --- ER real de Gloria (NIO) ---
INGRESOS = [332915, 543402, 301554, 325063, 298999, 425647]
COGS = [166524, 277026, 152888, 164774, 150127, 213888]
# Gastos fijos mensuales (constantes los 6 meses)
SUELDOS, SERVICIOS, ALCALDIA, COMBUSTIBLE = 15016, 5311, 732, 5311
PUBLICIDAD, RENTA, DEPRECIACION, OTROS = 5494, 9522, 9003, 1831

# Utilidad neta real esperada por mes
UTIL_NETA = [114171, 214156, 96446, 108069, 96652, 159539]
# Caja real (Mov fila Efectivo) por mes
CAJA = [101200, 322528, 433470, 552373, 659860, 841220]
# Saldo tarjeta real por mes (ESF_Mensual / Mov fila 87)
TARJETA_MENSUAL = {
    "2025-12": 40287, "2026-01": 38456, "2026-02": 43949,
    "2026-03": 45780, "2026-04": 47612, "2026-05": 60430,
}
TARJETA_APERTURA = 62261


def _inputs_gloria() -> InputsTipoA:
    er = [
        ER_LineaMes(
            mes=MESES[i], ingresos=INGRESOS[i], costo_ventas=COGS[i],
            sueldos_salarios=SUELDOS, servicios_publicos=SERVICIOS,
            alcaldia_dgi=ALCALDIA, combustible=COMBUSTIBLE, publicidad=PUBLICIDAD,
            renta=RENTA, gasto_depreciacion=DEPRECIACION, otros_gastos=OTROS,
        )
        for i in range(6)
    ]

    # Tarjeta revolving: saldos mensuales dados, sin gasto financiero (cuota 0).
    tarjeta = DeudaInput(
        numero="TARJETA", entidad="Tarjeta", tipo_credito="TARJETA DE CREDITO",
        estrategia="revolving", moneda="NIO", valor_inicial=TARJETA_APERTURA,
        saldo_reportado=60430.0, cuota=0.0,
        fecha_otorgamiento=date(2020, 1, 1), fecha_actualizado=date(2026, 5, 31),
        saldo_apertura=TARJETA_APERTURA, saldos_mensuales=TARJETA_MENSUAL,
    )

    # Saldos iniciales de apertura (Mov col "Saldos Iniciales")
    si = ESF_Saldos(
        efectivo=0.0,
        mobiliario_equipos=366243.0,
        vehiculos=695862.0,
        depreciacion_acumulada=0.0,
        tarjetas_credito=TARJETA_APERTURA,  # coincide con la apertura de la tarjeta
    )
    sf = ESF_Saldos(
        efectivo=841220.0, mobiliario_equipos=366243.0, vehiculos=695862.0,
        depreciacion_acumulada=-54018.0, tarjetas_credito=60430.0,
    )
    datos = DatosCliente(
        nombre_completo="Gloria Elena Guillen Robinson", cedula="601-140998-0002L",
        domicilio="Residencial Casa Real", contacto="+505 8510 8735",
        regimen="Cuota Fija", matricula="RNVD-118495", direccion_negocio="Bolonia",
        giro="Servicios de envio y paqueteria", antiguedad="05 anios", empleados=1,
        estado_civil="soltera", profesion="Ingeniera Industrial", sexo="Femenino",
        banco="FICOHSA", fecha_certificacion=date(2026, 6, 5),
    )
    return InputsTipoA(
        periodo=PeriodoSpec(tipo="A", mes_inicial="2025-12", mes_final="2026-05", tasa_cambio=TC),
        datos=datos, er_mensual=er, saldos_iniciales=si, saldos_finales=sf, deudas=[tarjeta],
    )


class GoldenGloriaTest(unittest.TestCase):
    def setUp(self):
        self.m = certificar_tipo_a(_inputs_gloria())

    def test_utilidad_neta_por_mes(self):
        for i, mes in enumerate(MESES):
            self.assertAlmostEqual(
                self.m.er.utilidad_neta_mes[mes], UTIL_NETA[i], delta=1.0,
                msg=f"Utilidad neta {mes}",
            )

    def test_ingresos_brutos_acumulado(self):
        self.assertAlmostEqual(self.m.er.total_ingresos(), 2227580, delta=1.0)

    def test_utilidad_periodo_acumulada(self):
        self.assertAlmostEqual(self.m.er.total_utilidad_neta(), 789033, delta=2.0)

    def test_caja_mensual_exacta(self):
        for i, mes in enumerate(MESES):
            self.assertAlmostEqual(
                self.m.mov.saldo_final_mes(mes), CAJA[i], delta=1.0,
                msg=f"Caja {mes}: motor={self.m.mov.saldo_final_mes(mes)} excel={CAJA[i]}",
            )

    def test_esf_corte_efectivo(self):
        self.assertAlmostEqual(self.m.esf.corte().efectivo, 841220, delta=1.0)

    def test_esf_corte_tarjetas(self):
        self.assertAlmostEqual(self.m.esf.corte().tarjetas_credito, 60430, delta=1.0)

    def test_esf_corte_capital_999844(self):
        self.assertAlmostEqual(self.m.esf.capital_apertura, 999844, delta=1.0)

    def test_esf_corte_resultados_ejercicio(self):
        self.assertAlmostEqual(self.m.esf.corte().resultados_ejercicio, 789033, delta=2.0)

    def test_esf_corte_total_activos(self):
        self.assertAlmostEqual(self.m.esf.corte().total_activos, 1849307, delta=2.0)

    def test_esf_corte_depreciacion_acumulada(self):
        self.assertAlmostEqual(self.m.esf.corte().depreciacion_acumulada, -54018, delta=1.0)

    def test_balance_cuadra_todos_los_meses(self):
        for e in self.m.esf.meses:
            self.assertLessEqual(abs(e.diferencia), 1.0, f"Mes {e.mes} descuadra {e.diferencia}")

    def test_validacion_ok(self):
        errores = [f"inv#{h.invariante}: {h.mensaje}" for h in self.m.validacion.errores]
        self.assertTrue(self.m.ok, "Errores:\n" + "\n".join(errores))


if __name__ == "__main__":
    unittest.main()

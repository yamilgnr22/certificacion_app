"""Continuar una certificacion emitida sin alterar lo ya firmado.

El caso real: el documento se emitio (ene-jun), el credito no se tramito y
vencio; hay que llegar a julio. El banco ya tiene enero-junio firmados, asi
que esas cifras no pueden moverse ni un cordoba.

El test que sostiene todo es `test_ningun_mes_certificado_cambia`: sin eso,
el CPA firmaria dos documentos que se contradicen.
"""

from __future__ import annotations

import copy
import unittest
from datetime import date

from motor.continuacion import (
    preparar_continuacion,
    verificar_inmutabilidad,
)
from motor.json_io import modelo_from_json

MESES = [f"2026-{m:02d}" for m in range(1, 7)]


def _emitido(er_modo: str = "generado") -> dict:
    """Un periodo Tipo A ya certificado, con deuda amortizable y tarjeta."""
    base = {
        "periodo": {"tipo": "A", "mes_inicial": "2026-01", "mes_final": "2026-06",
                    "tasa_cambio": 36.6243},
        "datos": {"nombre_completo": "Ana Maria Lopez Ruiz", "cedula": "001-010190-0001A"},
        "saldos_iniciales": {"efectivo": 120_000, "mobiliario_equipos": 312_000,
                             "depreciacion_acumulada": -26_000},
        "saldos_finales": {"mobiliario_equipos": 312_000},
        "deudas": [
            {"numero": "4411", "entidad": "BAC", "tipo_credito": "CARTERA DE CONSUMO",
             "estrategia": "amortizable", "moneda": "NIO", "valor_inicial": 120_000,
             "saldo_reportado": 42_000, "cuota": 4_200,
             "fecha_otorgamiento": "2024-01-15", "fecha_actualizado": "2026-06-30",
             "fecha_vencimiento": "2027-01-15"},
            {"numero": "9080", "entidad": "BAC", "tipo_credito": "TARJETAS DE CREDITO",
             "estrategia": "revolving", "moneda": "NIO", "valor_inicial": 60_000,
             "saldo_reportado": 37_151, "cuota": 2_100,
             "fecha_otorgamiento": "2022-05-10", "fecha_actualizado": "2026-06-30"},
        ],
    }
    if er_modo == "generado":
        base["er_modo"] = "generado"
        base["er_generado"] = {
            "ingreso_base": 100_000, "costo_pct_sobre_venta": 12,
            "banda_ingreso_pct": 10, "banda_costo_pct": 5,
            "gasto_depreciacion_mensual": 5_200,
            "gastos_fijos": {"renta": 15_000, "servicios_publicos": 1_000,
                             "alcaldia_dgi": 800, "publicidad": 1_200, "otros_gastos": 500},
        }
    else:
        base["er_modo"] = "manual"
        base["er_mensual"] = [
            {"mes": m, "ingresos": 100_000 + i * 1_000, "costo_ventas": 12_000,
             "renta": 15_000, "servicios_publicos": 1_000, "alcaldia_dgi": 800,
             "publicidad": 1_200, "otros_gastos": 500, "gasto_depreciacion": 5_200}
            for i, m in enumerate(MESES)
        ]
    return base


class ContinuacionTest(unittest.TestCase):
    def setUp(self):
        self.inputs = _emitido()
        self.emitido = modelo_from_json(copy.deepcopy(self.inputs))
        self.body = preparar_continuacion(self.inputs, "2026-07")
        self.cont = modelo_from_json(copy.deepcopy(self.body))

    # ------------------------------------------------------- lo que no cambia
    def test_ningun_mes_certificado_cambia(self):
        """La red de seguridad de toda la funcionalidad."""
        fallas = verificar_inmutabilidad(self.emitido, self.cont, MESES)
        self.assertFalse(fallas, "\n  " + "\n  ".join(fallas))

    def test_los_ingresos_mes_a_mes_son_identicos(self):
        for mes in MESES:
            self.assertAlmostEqual(self.cont.er.ingresos_mes[mes],
                                   self.emitido.er.ingresos_mes[mes], delta=1.0, msg=mes)

    def test_los_gastos_financieros_no_se_recalculan(self):
        # El punto fino: sin congelar el interes, dar los saldos mes a mes
        # haria que el motor asuma la cuota completa y el ER cambiaria.
        for mes in MESES:
            self.assertAlmostEqual(self.cont.er.gastos_financieros_mes[mes],
                                   self.emitido.er.gastos_financieros_mes[mes],
                                   delta=1.0, msg=mes)

    def test_el_capital_de_apertura_se_mantiene(self):
        self.assertAlmostEqual(self.cont.esf.capital_apertura,
                               self.emitido.esf.capital_apertura, delta=1.0)

    def test_la_apertura_declarada_no_se_toca(self):
        self.assertEqual(self.body["saldos_iniciales"], self.inputs["saldos_iniciales"])

    # ------------------------------------------------------- lo que si cambia
    def test_agrega_el_mes_nuevo(self):
        self.assertEqual(self.cont.er.meses, MESES + ["2026-07"])
        self.assertGreater(self.cont.er.ingresos_mes["2026-07"], 0)

    def test_los_totales_y_promedios_se_recalculan_sobre_el_periodo_nuevo(self):
        self.assertGreater(self.cont.er.total_ingresos(), self.emitido.er.total_ingresos())
        # El promedio ahora divide entre 7, no entre 6.
        self.assertAlmostEqual(
            self.cont.er.promedio_ingresos(), self.cont.er.total_ingresos() / 7, delta=1.0)

    def test_el_mes_nuevo_usa_los_mismos_gastos_fijos(self):
        from motor.er import LABEL_RENTA
        self.assertAlmostEqual(
            self.cont.er.gastos_por_label_mes[LABEL_RENTA]["2026-07"],
            self.emitido.er.gastos_por_label_mes[LABEL_RENTA]["2026-06"], delta=1.0)

    # ----------------------------------------------------------------- deuda
    def test_la_deuda_de_los_meses_viejos_queda_congelada(self):
        por_no = {p.deuda.numero: p for p in self.emitido.planes}
        for p in self.cont.planes:
            viejo = por_no[p.deuda.numero]
            s_viejo = {c.mes: c.saldo_final_nio for c in viejo.cuotas}
            for c in p.cuotas:
                if c.mes in MESES:
                    self.assertAlmostEqual(c.saldo_final_nio, s_viejo[c.mes], delta=1.0,
                                           msg=f"{p.deuda.numero} {c.mes}")

    def test_el_credito_sigue_amortizando_en_el_mes_nuevo(self):
        plan = next(p for p in self.cont.planes if p.deuda.numero == "4411")
        saldos = {c.mes: c.saldo_final_nio for c in plan.cuotas}
        self.assertLess(saldos["2026-07"], saldos["2026-06"], "deberia bajar un mes mas")

    def test_la_tarjeta_no_amortiza(self):
        plan = next(p for p in self.cont.planes if p.deuda.numero == "9080")
        saldos = {c.mes: c.saldo_final_nio for c in plan.cuotas}
        self.assertAlmostEqual(saldos["2026-07"], saldos["2026-06"], delta=1.0)

    def test_el_corte_nuevo_es_el_saldo_proyectado(self):
        # Sin reporte de credito actualizado, el saldo al corte es la
        # proyeccion declarada; el invariante #1 la valida contra si misma.
        self.assertTrue(self.cont.ok, [h.mensaje for h in self.cont.validacion.errores])

    # ------------------------------------------------------------- integridad
    def test_el_balance_cuadra_todos_los_meses(self):
        for e in self.cont.esf.meses:
            self.assertLessEqual(abs(e.diferencia), 1.0, f"{e.mes}: {e.diferencia}")

    def test_la_validacion_pasa_sin_errores(self):
        self.assertTrue(self.cont.ok, [h.mensaje for h in self.cont.validacion.errores])

    def test_es_reproducible(self):
        otro = modelo_from_json(copy.deepcopy(preparar_continuacion(self.inputs, "2026-07")))
        self.assertEqual([e.efectivo for e in otro.esf.meses],
                         [e.efectivo for e in self.cont.esf.meses])

    def test_dos_meses_nuevos_de_una(self):
        body = preparar_continuacion(self.inputs, "2026-08")
        m = modelo_from_json(copy.deepcopy(body))
        self.assertEqual(m.er.meses, MESES + ["2026-07", "2026-08"])
        self.assertFalse(verificar_inmutabilidad(self.emitido, m, MESES))

    def test_rechaza_un_corte_que_no_avanza(self):
        for corte in ("2026-06", "2026-05"):
            with self.assertRaises(ValueError):
                preparar_continuacion(self.inputs, corte)

    def test_tambien_funciona_con_er_manual(self):
        inputs = _emitido(er_modo="manual")
        emitido = modelo_from_json(copy.deepcopy(inputs))
        cont = modelo_from_json(copy.deepcopy(preparar_continuacion(inputs, "2026-07")))
        self.assertFalse(verificar_inmutabilidad(emitido, cont, MESES))
        self.assertIn("2026-07", cont.er.meses)


class VerificarInmutabilidadTest(unittest.TestCase):
    """El guardia tiene que DETECTAR un cambio, no solo aprobar."""

    def test_detecta_un_mes_alterado(self):
        inputs = _emitido()
        emitido = modelo_from_json(copy.deepcopy(inputs))
        body = preparar_continuacion(inputs, "2026-07")
        # Alguien toca marzo a mano: tiene que saltar.
        for linea in body["er_mensual"]:
            if linea["mes"] == "2026-03":
                linea["ingresos"] = float(linea["ingresos"]) + 5_000
        alterado = modelo_from_json(copy.deepcopy(body))
        fallas = verificar_inmutabilidad(emitido, alterado, MESES)
        self.assertTrue(fallas)
        self.assertTrue(any("2026-03" in f for f in fallas), fallas)


if __name__ == "__main__":
    unittest.main()

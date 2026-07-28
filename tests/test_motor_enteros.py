"""Certificacion en cordobas ENTEROS (los bancos exigen cuadre exacto).

El motor redondea a entero en cada calculo (motor/*/_redondear). Como no
usa plug, el balance cuadra EXACTO (delta 0, no la tolerancia de C$1 de los
invariantes) y todas las cifras del ER/ESF son enteras. Este test fija esa
garantia: si un cambio futuro reintroduce decimales o rompe el cuadre exacto,
falla aca.
"""

from __future__ import annotations

import unittest

import tests.test_motor_tipo_b as tb
from motor import certificar_tipo_a, certificar_tipo_b
from tests.test_motor_golden_gloria import _inputs_gloria


def _es_entero(v: float) -> bool:
    return abs(float(v) - round(float(v))) < 1e-9


def _descuadre_balance(esf_mes) -> float:
    A = (esf_mes.efectivo + esf_mes.cuentas_por_cobrar + esf_mes.inventarios
         + esf_mes.bienes_inmuebles + esf_mes.mobiliario_equipos + esf_mes.vehiculos
         + esf_mes.depreciacion_acumulada)
    P = (esf_mes.tarjetas_credito + esf_mes.proveedores + esf_mes.impuestos_por_pagar
         + esf_mes.gastos_acumulados + esf_mes.creditos_hipotecarios + esf_mes.creditos_consumo
         + esf_mes.creditos_personales + esf_mes.creditos_prendarios + esf_mes.creditos_comerciales)
    PAT = esf_mes.capital + esf_mes.resultados_acumulados + esf_mes.resultados_ejercicio
    return A - (P + PAT)


def _modelos():
    yield "Gloria-A", certificar_tipo_a(_inputs_gloria())
    for seed in ("s1", "s2", "s3"):
        yield f"TipoB-{seed}", certificar_tipo_b(tb._inputs(seed=seed))
        yield f"TipoB-inv-{seed}", certificar_tipo_b(tb._inputs(seed=seed, con_inventario=True))


class CuadreExactoEnterosTest(unittest.TestCase):
    def test_balance_cuadra_exacto_sin_tolerancia(self):
        for nombre, m in _modelos():
            for e in m.esf.meses:
                self.assertEqual(_descuadre_balance(e), 0.0,
                                 f"{nombre} mes {e.mes}: balance no cuadra exacto")

    def test_todas_las_cifras_del_esf_son_enteras(self):
        campos = ["efectivo", "inventarios", "bienes_inmuebles", "mobiliario_equipos",
                  "vehiculos", "depreciacion_acumulada", "capital", "resultados_acumulados",
                  "resultados_ejercicio", "creditos_comerciales", "tarjetas_credito"]
        for nombre, m in _modelos():
            for e in m.esf.meses:
                for c in campos:
                    v = getattr(e, c)
                    self.assertTrue(_es_entero(v), f"{nombre} {c}={v} no es entero")

    def test_er_en_enteros(self):
        for nombre, m in _modelos():
            for mes in m.er.meses:
                self.assertTrue(_es_entero(m.er.ingresos_mes[mes]), f"{nombre} ingresos {mes}")
                self.assertTrue(_es_entero(m.er.utilidad_neta_mes[mes]), f"{nombre} utilidad {mes}")
                self.assertTrue(_es_entero(m.er.gastos_financieros_mes[mes]), f"{nombre} GF {mes}")

    def test_resultado_esf_igual_utilidad_er_exacto(self):
        # Sin ajuste de redondeo: el resultado del ejercicio del ESF coincide
        # EXACTO con la utilidad acumulada del ER (no hay diferencia de +-1).
        for nombre, m in _modelos():
            corte = m.esf.corte()
            util_acum = m.er.total_utilidad_neta()
            self.assertEqual(corte.resultados_ejercicio, util_acum,
                             f"{nombre}: resultado ESF != utilidad ER")


class ProveedoresEnBandaTest(unittest.TestCase):
    """Proveedores oscila en banda atado a las compras y a la caja:
    lo comprado y no pagado queda como pasivo (sale menos efectivo); cuando
    el pasivo baja, se paga mas. El balance debe cuadrar EXACTO igual."""

    MESES = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    ER = [{"mes": m, "ingresos": 21_000_000, "costo_ventas": 19_000_000,
           "sueldos_salarios": 128_185, "gasto_depreciacion": 43_432} for m in MESES]

    def _modelo_a(self, prov_ini=3_582_796, prov_fin=3_000_000):
        from motor.json_io import inputs_from_json

        return certificar_tipo_a(inputs_from_json({
            "periodo": {"tipo": "A", "mes_inicial": "2026-01", "mes_final": "2026-06",
                        "tasa_cambio": 36.6243},
            "datos": {"nombre_completo": "D", "cedula": "001", "empleados": 1,
                      "fecha_certificacion": "2026-07-26"},
            "er_mensual": self.ER,
            "saldos_iniciales": {"efectivo": 2_095_487, "inventarios": 21_892_008,
                                 "proveedores": prov_ini, "mobiliario_equipos": 915_610},
            "saldos_finales": {"efectivo": 0, "inventarios": 27_994_232,
                               "proveedores": prov_fin, "mobiliario_equipos": 915_610,
                               "depreciacion_acumulada": -260_592},
            "deudas": [],
        }))

    def test_tipo_a_oscila_y_ancla_al_saldo_final(self):
        m = self._modelo_a()
        provs = [e.proveedores for e in m.esf.meses]
        self.assertEqual(provs[-1], 3_000_000)        # ancla dura EXACTA
        self.assertGreater(len(set(provs)), 1)        # oscila, no plano
        for e in m.esf.meses:
            self.assertEqual(_descuadre_balance(e), 0.0, f"mes {e.mes}")

    def test_tipo_a_sin_proveedores_no_rompe(self):
        m = self._modelo_a(prov_ini=0, prov_fin=0)
        self.assertTrue(all(e.proveedores == 0 for e in m.esf.meses))
        for e in m.esf.meses:
            self.assertEqual(_descuadre_balance(e), 0.0)

    def test_tipo_b_oscila_alrededor_de_la_apertura(self):
        from motor.json_io import inputs_tipo_b_from_json

        m = certificar_tipo_b(inputs_tipo_b_from_json({
            "periodo": {"tipo": "B", "mes_inicial": "2026-01", "mes_final": "2026-06",
                        "tasa_cambio": 36.6243},
            "datos": {"nombre_completo": "D", "cedula": "001", "empleados": 1,
                      "fecha_certificacion": "2026-07-26"},
            "er_mensual": self.ER,
            "saldos_iniciales": {"efectivo": 2_000_000, "inventarios": 21_892_008,
                                 "proveedores": 3_582_796, "mobiliario_equipos": 915_610},
            "cuentas_objetivo": [{"cuenta": "efectivo", "objetivo": 2_000_000, "tolerancia_pct": 20},
                                 {"cuenta": "inventarios", "objetivo": 25_000_000, "tolerancia_pct": 20}],
            "seed": "prov-b",
        }))
        provs = [e.proveedores for e in m.esf.meses]
        self.assertGreater(len(set(provs)), 1)        # oscila
        for p in provs:                                # dentro de la banda +-10%
            self.assertLessEqual(p, 3_582_796 * 1.10 + 1)
            self.assertGreaterEqual(p, 3_582_796 * 0.90 - 1)
        for e in m.esf.meses:
            self.assertEqual(_descuadre_balance(e), 0.0, f"mes {e.mes}")


class CreditoNuevoDelPeriodoTest(unittest.TestCase):
    """Credito otorgado DENTRO del periodo (caso real Jose Daniel): no existia
    al inicio, asi que arranca en 0, aparece con su desembolso el mes del
    otorgamiento (que ENTRA a caja) y amortiza hasta el saldo del corte."""

    MESES = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]

    def _modelo(self):
        from motor.json_io import inputs_from_json

        return certificar_tipo_a(inputs_from_json({
            "periodo": {"tipo": "A", "mes_inicial": "2026-01", "mes_final": "2026-06",
                        "tasa_cambio": 36.6243},
            "datos": {"nombre_completo": "D", "cedula": "001", "empleados": 1,
                      "fecha_certificacion": "2026-07-26"},
            "er_mensual": [{"mes": m, "ingresos": 21_000_000, "costo_ventas": 19_000_000}
                           for m in self.MESES],
            "saldos_iniciales": {"efectivo": 2_095_487, "creditos_prendarios": 732_486},
            "saldos_finales": {"efectivo": 0, "creditos_prendarios": 334_290.27},
            "deudas": [
                # Anterior al periodo: existia en enero
                {"numero": "6332", "entidad": "LAFISE", "tipo_credito": "CARTERA DE VEHICULOS",
                 "estrategia": "amortizable", "moneda": "NIO", "valor_inicial": 732_486,
                 "saldo_reportado": 0, "cuota": 0, "fecha_otorgamiento": "2024-01-04",
                 "fecha_actualizado": "2026-06-01"},
                # NUEVO: otorgado en mayo, dentro del periodo
                {"numero": "0622", "entidad": "FICOHSA", "tipo_credito": "CARTERA DE VEHICULOS",
                 "estrategia": "amortizable", "moneda": "NIO", "valor_inicial": 509_077.77,
                 "saldo_reportado": 334_290.27, "cuota": 175_666,
                 "fecha_otorgamiento": "2026-05-11", "fecha_actualizado": "2026-06-01"},
            ],
        }))

    def test_apertura_solo_incluye_los_creditos_que_existian(self):
        m = self._modelo()
        nuevo = next(p for p in m.planes if p.deuda.numero == "0622")
        viejo = next(p for p in m.planes if p.deuda.numero == "6332")
        self.assertEqual(nuevo.saldo_apertura_nio, 0.0)      # no existia en enero
        self.assertGreater(viejo.saldo_apertura_nio, 0.0)    # si existia

    def test_saldo_cero_hasta_el_desembolso_y_ancla_al_corte(self):
        m = self._modelo()
        plan = next(p for p in m.planes if p.deuda.numero == "0622")
        por_mes = {c.mes: c.saldo_final_nio for c in plan.cuotas}
        for mes in ["2026-01", "2026-02", "2026-03", "2026-04"]:
            self.assertEqual(por_mes[mes], 0.0, f"{mes}: aun no existia")
        self.assertEqual(por_mes["2026-05"], 509_078.0)      # desembolso (cordobas enteros)
        self.assertEqual(por_mes["2026-06"], 334_290.27)     # ancla del corte

    def test_desembolso_entra_a_caja_ese_mes(self):
        m = self._modelo()
        por_mes = {mv.mes: mv for mv in m.mov.movs}
        self.assertEqual(por_mes["2026-04"].financiamiento_credito, 0.0)
        self.assertEqual(por_mes["2026-05"].financiamiento_credito, 509_078.0)

    def test_balance_cuadra_exacto(self):
        for e in self._modelo().esf.meses:
            self.assertEqual(_descuadre_balance(e), 0.0, f"mes {e.mes}")


if __name__ == "__main__":
    unittest.main()

"""Tests del ER modo 'generado' (base + bandas centradas, reproducible).

Cubre las 4 reglas criticas: independencia por mes sobre base fija, banda
centrada sin sesgo (promedio a ±2% de la base, NO exacto), seed determinista
y costo < venta (validacion bloqueante). Mas gastos fijos + overrides con
claves restringidas, y e2e con Tipo B via json_io.
"""

from __future__ import annotations

import unittest

from motor.er_generado import ERGeneradoParams, generar_er_mensual
from motor.inputs import PeriodoSpec


def _periodo_12() -> PeriodoSpec:
    return PeriodoSpec(tipo="B", mes_inicial="2026-01", mes_final="2026-12", tasa_cambio=36.6243)


def _params(**kw) -> ERGeneradoParams:
    base = dict(
        ingreso_base=350_000.0,
        costo_pct_sobre_venta=50.0,
        banda_ingreso_pct=20.0,
        banda_costo_pct=5.0,
        seed="test-er-1",
        gasto_depreciacion_mensual=9_003.0,
        gastos_fijos={"sueldos_salarios": 15_016, "renta": 9_522},
    )
    base.update(kw)
    return ERGeneradoParams(**base)


class GeneracionIngresosTest(unittest.TestCase):
    def test_reproducible_misma_seed(self):
        a = generar_er_mensual(_params(), _periodo_12())
        b = generar_er_mensual(_params(), _periodo_12())
        self.assertEqual([l.ingresos for l in a], [l.ingresos for l in b])

    def test_seed_distinta_cambia_serie(self):
        a = generar_er_mensual(_params(seed="s1"), _periodo_12())
        b = generar_er_mensual(_params(seed="s2"), _periodo_12())
        self.assertNotEqual([l.ingresos for l in a], [l.ingresos for l in b])

    def test_seed_default_derivada_de_cedula(self):
        a = generar_er_mensual(_params(seed=""), _periodo_12(), cedula="001-X")
        b = generar_er_mensual(_params(seed=""), _periodo_12(), cedula="001-X")
        c = generar_er_mensual(_params(seed=""), _periodo_12(), cedula="002-Y")
        self.assertEqual([l.ingresos for l in a], [l.ingresos for l in b])
        self.assertNotEqual([l.ingresos for l in a], [l.ingresos for l in c])

    def test_cada_mes_dentro_de_banda(self):
        for seed in ("s1", "s2", "s3", "s4", "s5"):
            lineas = generar_er_mensual(_params(seed=seed), _periodo_12())
            for l in lineas:
                self.assertGreaterEqual(l.ingresos, 350_000 * 0.80 - 1, f"seed {seed} mes {l.mes}")
                self.assertLessEqual(l.ingresos, 350_000 * 1.20 + 1, f"seed {seed} mes {l.mes}")

    def test_promedio_cerca_de_base_sin_ser_exacto(self):
        # Regla 2 + decision del usuario: centrado (±2%) pero NO clavado en la base.
        exactos = 0
        for seed in ("s1", "s2", "s3", "s4", "s5"):
            for periodo in (_periodo_12(), PeriodoSpec(tipo="A", mes_inicial="2025-12", mes_final="2026-05", tasa_cambio=36.6)):
                lineas = generar_er_mensual(_params(seed=seed), periodo)
                prom = sum(l.ingresos for l in lineas) / len(lineas)
                desvio = abs(prom - 350_000) / 350_000
                self.assertLessEqual(desvio, 0.02, f"seed {seed}: promedio {prom:,.0f} se desvia {desvio:.1%}")
                if abs(prom - 350_000) < 1.0:
                    exactos += 1
        self.assertLess(exactos, 10, "el promedio no debe quedar clavado en la base")

    def test_independiente_por_mes_no_camino(self):
        # Sobre base fija: el mes con banda 0 es exactamente la base (sin deriva).
        lineas = generar_er_mensual(_params(banda_ingreso_pct=0), _periodo_12())
        for l in lineas:
            self.assertAlmostEqual(l.ingresos, 350_000.0, places=2)


class GeneracionCostoTest(unittest.TestCase):
    def test_costo_nunca_supera_venta(self):
        for seed in ("s1", "s2", "s3"):
            lineas = generar_er_mensual(_params(seed=seed), _periodo_12())
            for l in lineas:
                self.assertLess(l.costo_ventas, l.ingresos, f"seed {seed} mes {l.mes}")

    def test_tasa_dentro_de_banda(self):
        lineas = generar_er_mensual(_params(), _periodo_12())
        for l in lineas:
            tasa = l.costo_ventas / l.ingresos
            self.assertGreaterEqual(tasa, 0.50 * 0.95 - 0.001)
            self.assertLessEqual(tasa, 0.50 * 1.05 + 0.001)

    def test_costo_pct_que_puede_superar_100_es_bloqueante(self):
        with self.assertRaises(ValueError):
            _params(costo_pct_sobre_venta=96.0, banda_costo_pct=5.0)

    def test_costo_pct_fuera_de_rango(self):
        with self.assertRaises(ValueError):
            _params(costo_pct_sobre_venta=0)
        with self.assertRaises(ValueError):
            _params(costo_pct_sobre_venta=100)


class GastosTest(unittest.TestCase):
    def test_gastos_fijos_en_todos_los_meses(self):
        lineas = generar_er_mensual(_params(), _periodo_12())
        for l in lineas:
            self.assertEqual(l.sueldos_salarios, 15_016)
            self.assertEqual(l.renta, 9_522)
            self.assertEqual(l.gasto_depreciacion, 9_003)

    def test_override_puntual_solo_ese_mes(self):
        p = _params(gastos_overrides={"2026-03": {"alcaldia_dgi": 2_196}})
        lineas = generar_er_mensual(p, _periodo_12())
        por_mes = {l.mes: l for l in lineas}
        self.assertEqual(por_mes["2026-03"].alcaldia_dgi, 2_196)
        self.assertEqual(por_mes["2026-02"].alcaldia_dgi, 0)
        self.assertEqual(por_mes["2026-03"].renta, 9_522)  # fijos se conservan

    def test_depreciacion_no_va_en_gastos_fijos(self):
        with self.assertRaises(ValueError):
            _params(gastos_fijos={"gasto_depreciacion": 9_003})

    def test_financieros_no_van_en_overrides(self):
        with self.assertRaises(ValueError):
            _params(gastos_overrides={"2026-01": {"gastos_financieros": 100}})

    def test_override_fuera_del_periodo_es_error(self):
        p = _params(gastos_overrides={"2027-01": {"renta": 1}})
        with self.assertRaises(ValueError):
            generar_er_mensual(p, _periodo_12())


class E2EJsonIoTest(unittest.TestCase):
    def test_tipo_b_con_er_generado_valida_ok(self):
        from motor.json_io import inputs_tipo_b_from_json
        from motor import certificar_tipo_b

        body = {
            "periodo": {"tipo": "B", "mes_inicial": "2026-01", "mes_final": "2026-12", "tasa_cambio": 36.6243},
            "datos": {"nombre_completo": "Demo Generado", "cedula": "001-000000-0000X", "empleados": 1,
                      "fecha_certificacion": "2027-01-15"},
            "er_modo": "generado",
            "er_generado": {
                "ingreso_base": 200_000, "banda_ingreso_pct": 20,
                "costo_pct_sobre_venta": 60, "banda_costo_pct": 5,
                "seed": "e2e-gen", "gasto_depreciacion_mensual": 4_000,
                "gastos_fijos": {"sueldos_salarios": 15_000, "renta": 8_000},
            },
            "saldos_iniciales": {"efectivo": 20_000, "mobiliario_equipos": 240_000},
            "cuentas_objetivo": [{"cuenta": "efectivo", "objetivo": 120_000, "tolerancia_pct": 20}],
            "seed": "e2e-caja",
        }
        modelo = certificar_tipo_b(inputs_tipo_b_from_json(body))
        errores = [f"inv#{h.invariante}: {h.mensaje}" for h in modelo.validacion.errores]
        self.assertTrue(modelo.ok, "Errores:\n" + "\n".join(errores))
        self.assertEqual(len(modelo.er.meses), 12)
        # Reproducible de punta a punta
        modelo2 = certificar_tipo_b(inputs_tipo_b_from_json(body))
        self.assertEqual(
            [m.saldo_final for m in modelo.mov.movs],
            [m.saldo_final for m in modelo2.mov.movs],
        )

    def test_er_modo_invalido(self):
        from motor.json_io import inputs_from_json

        body = {
            "periodo": {"tipo": "A", "mes_inicial": "2026-01", "mes_final": "2026-06", "tasa_cambio": 36.6},
            "er_modo": "magico",
        }
        with self.assertRaises(ValueError):
            inputs_from_json(body)


class CostoSobreIngresosDadosTest(unittest.TestCase):
    """Caso real (Jose Daniel): el CPA tiene la VENTA exacta mes a mes pero no
    el costo, solo sabe que ronda un % de la venta. El motor genera el costo
    con banda sobre los ingresos dados, SIN tocarlos."""

    MESES = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    INGRESOS = {
        "2026-01": 25_180_990, "2026-02": 21_888_289, "2026-03": 21_798_222,
        "2026-04": 17_335_831, "2026-05": 21_237_262, "2026-06": 23_852_784,
    }

    def _gen(self, pct=91.0, banda=2.0, seed="s1"):
        from motor.er_generado import generar_costo_sobre_ingresos
        return generar_costo_sobre_ingresos(self.INGRESOS, self.MESES, pct, banda, seed)

    def test_cada_mes_dentro_de_la_banda(self):
        costos = self._gen()
        for mes in self.MESES:
            tasa = costos[mes] / self.INGRESOS[mes] * 100
            self.assertGreaterEqual(tasa, 91.0 * 0.98 - 0.01, f"mes {mes}")
            self.assertLessEqual(tasa, 91.0 * 1.02 + 0.01, f"mes {mes}")

    def test_promedio_cerca_del_pct_sin_ser_exacto(self):
        exactos = 0
        for seed in ("s1", "s2", "s3", "s4", "s5"):
            costos = self._gen(seed=seed)
            tasa_prom = sum(costos.values()) / sum(self.INGRESOS.values()) * 100
            self.assertLess(abs(tasa_prom - 91.0), 1.0, f"seed {seed}: {tasa_prom:.2f}%")
            if abs(tasa_prom - 91.0) < 0.01:
                exactos += 1
        self.assertLess(exactos, 5, "el promedio no debe quedar clavado en el pct")

    def test_reproducible_y_seed_cambia_serie(self):
        self.assertEqual(self._gen(seed="a"), self._gen(seed="a"))
        self.assertNotEqual(self._gen(seed="a"), self._gen(seed="b"))

    def test_costo_nunca_supera_la_venta(self):
        for seed in ("s1", "s2", "s3"):
            costos = self._gen(pct=95.0, banda=4.0, seed=seed)
            for mes in self.MESES:
                self.assertLess(costos[mes], self.INGRESOS[mes], f"seed {seed} mes {mes}")

    def test_pct_con_banda_que_supera_100_es_bloqueante(self):
        with self.assertRaises(ValueError):
            self._gen(pct=98.0, banda=5.0)   # 98 * 1.05 = 102.9%
        with self.assertRaises(ValueError):
            self._gen(pct=0)
        with self.assertRaises(ValueError):
            self._gen(pct=100)

    def test_e2e_modo_manual_conserva_ingresos_y_genera_costo(self):
        from motor.json_io import inputs_from_json

        body = {
            "periodo": {"tipo": "A", "mes_inicial": "2026-01", "mes_final": "2026-06", "tasa_cambio": 36.6243},
            "datos": {"nombre_completo": "Demo", "cedula": "443-260488-0001K", "empleados": 10,
                      "fecha_certificacion": "2026-07-26"},
            "er_modo": "manual",
            "er_mensual": [{"mes": m, "ingresos": v, "costo_ventas": 0} for m, v in self.INGRESOS.items()],
            "costo_generado": {"pct_sobre_venta": 91, "banda_pct": 2},
            "saldos_iniciales": {"efectivo": 100_000},
            "saldos_finales": {"efectivo": 0},
        }
        lineas = inputs_from_json(body).er_mensual
        # Los ingresos NO se tocan (dato duro del cliente)
        self.assertEqual([l.ingresos for l in lineas], list(self.INGRESOS.values()))
        # El costo se genero dentro de la banda y reemplaza el 0 cargado
        for l in lineas:
            tasa = l.costo_ventas / l.ingresos * 100
            self.assertGreater(l.costo_ventas, 0)
            self.assertGreaterEqual(tasa, 91.0 * 0.98 - 0.01)
            self.assertLessEqual(tasa, 91.0 * 1.02 + 0.01)

    def test_sin_bloque_el_costo_manual_se_respeta(self):
        from motor.json_io import inputs_from_json

        body = {
            "periodo": {"tipo": "A", "mes_inicial": "2026-01", "mes_final": "2026-01", "tasa_cambio": 36.6},
            "datos": {"nombre_completo": "D", "cedula": "001", "empleados": 1,
                      "fecha_certificacion": "2026-02-01"},
            "er_mensual": [{"mes": "2026-01", "ingresos": 100_000, "costo_ventas": 60_000}],
            "saldos_iniciales": {"efectivo": 1000}, "saldos_finales": {"efectivo": 0},
        }
        self.assertEqual(inputs_from_json(body).er_mensual[0].costo_ventas, 60_000)


class FechaDeudaTest(unittest.TestCase):
    """La UI manda fechas de deuda como 'AAAA-MM' (mismo formato que los
    meses); el parser debe aceptarlo (dia 01) y dar error claro si es basura."""

    def test_acepta_mes_sin_dia(self):
        from datetime import date
        from motor.json_io import _fecha
        self.assertEqual(_fecha("2026-01"), date(2026, 1, 1))
        self.assertEqual(_fecha("2026-01-15"), date(2026, 1, 15))
        self.assertIsNone(_fecha(""))
        self.assertIsNone(_fecha(None))

    def test_fecha_basura_mensaje_claro(self):
        from motor.json_io import _fecha
        with self.assertRaises(ValueError) as ctx:
            _fecha("enero 2026")
        self.assertIn("AAAA-MM-DD", str(ctx.exception))

    def test_deuda_con_otorgamiento_en_formato_mes(self):
        from motor.json_io import inputs_from_json
        from motor import certificar_tipo_a
        body = {
            "periodo": {"tipo": "A", "mes_inicial": "2026-01", "mes_final": "2026-06", "tasa_cambio": 36.6243},
            "datos": {"nombre_completo": "T", "cedula": "001-X", "empleados": 1, "fecha_certificacion": "2026-07-01"},
            "er_mensual": [{"mes": f"2026-{m:02d}", "ingresos": 100000, "costo_ventas": 60000, "sueldos_salarios": 10000} for m in range(1, 7)],
            "saldos_iniciales": {"efectivo": 50000},
            "saldos_finales": {},
            "deudas": [{
                "numero": "C1", "entidad": "Banco", "tipo_credito": "CREDITO PERSONAL",
                "estrategia": "amortizable", "moneda": "USD", "valor_inicial": 2600,
                "saldo_reportado": 2000, "cuota": 100, "fecha_otorgamiento": "2026-01",
                "incluir_en_er": True,
            }],
        }
        modelo = certificar_tipo_a(inputs_from_json(body))  # no debe lanzar
        self.assertEqual(len(modelo.planes), 1)
        self.assertEqual(modelo.planes[0].cuenta_esf, "creditos_personales")


if __name__ == "__main__":
    unittest.main()

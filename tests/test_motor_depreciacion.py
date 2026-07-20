"""Tests de la depreciacion por componentes de PPE (motor/depreciacion).

Linea recta: gasto mensual = valor * pct/100 / (vida_anios * 12), con el
valor tomado del saldo inicial de la cuenta. El bloque `depreciacion_ppe`
del JSON reemplaza a la depreciacion manual del ER en ambos modos.
"""

from __future__ import annotations

import unittest

from motor.depreciacion import ComponenteDepreciacion, calcular_depreciacion


class ComponenteTest(unittest.TestCase):
    def test_ejemplo_propiedad_20pct_10_anios(self):
        c = ComponenteDepreciacion(
            cuenta="bienes_inmuebles", valor=1_000_000, pct_depreciable=20, vida_util_anios=10
        )
        self.assertEqual(c.base_depreciable, 200_000.0)
        self.assertEqual(c.gasto_mensual, 1_666.67)

    def test_100pct_5_anios(self):
        c = ComponenteDepreciacion(
            cuenta="vehiculos", valor=600_000, pct_depreciable=100, vida_util_anios=5
        )
        self.assertEqual(c.base_depreciable, 600_000.0)
        self.assertEqual(c.gasto_mensual, 10_000.0)

    def test_saldo_cero_da_gasto_cero(self):
        c = ComponenteDepreciacion(
            cuenta="mobiliario_equipos", valor=0, pct_depreciable=50, vida_util_anios=5
        )
        self.assertEqual(c.gasto_mensual, 0.0)

    def test_validaciones_bloqueantes(self):
        with self.assertRaises(ValueError):
            ComponenteDepreciacion(cuenta="inventarios", valor=1, pct_depreciable=50, vida_util_anios=5)
        with self.assertRaises(ValueError):
            ComponenteDepreciacion(cuenta="vehiculos", valor=1, pct_depreciable=0, vida_util_anios=5)
        with self.assertRaises(ValueError):
            ComponenteDepreciacion(cuenta="vehiculos", valor=1, pct_depreciable=101, vida_util_anios=5)
        with self.assertRaises(ValueError):
            ComponenteDepreciacion(cuenta="vehiculos", valor=1, pct_depreciable=50, vida_util_anios=0)
        with self.assertRaises(ValueError):
            ComponenteDepreciacion(cuenta="vehiculos", valor=-1, pct_depreciable=50, vida_util_anios=5)


class CalcularDesdeSpecTest(unittest.TestCase):
    SALDOS = {"bienes_inmuebles": 1_000_000, "vehiculos": 600_000, "mobiliario_equipos": 240_000}

    def test_total_suma_componentes(self):
        dep = calcular_depreciacion(
            {
                "bienes_inmuebles": {"pct_depreciable": 20, "vida_util_anios": 10},
                "vehiculos": {"pct_depreciable": 100, "vida_util_anios": 5},
            },
            self.SALDOS,
        )
        self.assertEqual(len(dep.componentes), 2)
        self.assertEqual(dep.gasto_mensual_total, 1_666.67 + 10_000.0)

    def test_componente_no_declarado_no_se_deprecia(self):
        dep = calcular_depreciacion(
            {"mobiliario_equipos": {"pct_depreciable": 100, "vida_util_anios": 2}}, self.SALDOS
        )
        self.assertEqual([c.cuenta for c in dep.componentes], ["mobiliario_equipos"])
        self.assertEqual(dep.gasto_mensual_total, 10_000.0)

    def test_cuenta_desconocida_es_error(self):
        with self.assertRaises(ValueError):
            calcular_depreciacion({"terrenos": {"pct_depreciable": 10, "vida_util_anios": 10}}, self.SALDOS)

    def test_acepta_esf_saldos(self):
        from motor.inputs import ESF_Saldos

        dep = calcular_depreciacion(
            {"vehiculos": {"pct_depreciable": 100, "vida_util_anios": 5}},
            ESF_Saldos(vehiculos=600_000),
        )
        self.assertEqual(dep.gasto_mensual_total, 10_000.0)


class JsonIoOverrideTest(unittest.TestCase):
    _DEP_PPE = {
        "bienes_inmuebles": {"pct_depreciable": 20, "vida_util_anios": 10},
        "vehiculos": {"pct_depreciable": 100, "vida_util_anios": 5},
    }
    _SALDOS = {"efectivo": 50_000, "bienes_inmuebles": 1_000_000, "vehiculos": 600_000}

    def test_modo_manual_reemplaza_cada_mes(self):
        from motor.json_io import inputs_from_json

        body = {
            "periodo": {"tipo": "A", "mes_inicial": "2026-01", "mes_final": "2026-03", "tasa_cambio": 36.6},
            "datos": {"nombre_completo": "X", "cedula": "001", "empleados": 1,
                      "fecha_certificacion": "2026-04-01"},
            "er_mensual": [
                {"mes": "2026-01", "ingresos": 100, "gasto_depreciacion": 999},
                {"mes": "2026-02", "ingresos": 100, "gasto_depreciacion": 0},
                {"mes": "2026-03", "ingresos": 100},
            ],
            "saldos_iniciales": self._SALDOS,
            "depreciacion_ppe": self._DEP_PPE,
        }
        inputs = inputs_from_json(body)
        self.assertEqual([ln.gasto_depreciacion for ln in inputs.er_mensual],
                         [11_666.67, 11_666.67, 11_666.67])

    def test_modo_generado_reemplaza_el_parametro(self):
        from motor.json_io import inputs_from_json

        body = {
            "periodo": {"tipo": "A", "mes_inicial": "2026-01", "mes_final": "2026-03", "tasa_cambio": 36.6},
            "datos": {"nombre_completo": "X", "cedula": "001", "empleados": 1,
                      "fecha_certificacion": "2026-04-01"},
            "er_modo": "generado",
            "er_generado": {
                "ingreso_base": 100_000, "costo_pct_sobre_venta": 50,
                "seed": "dep-test", "gasto_depreciacion_mensual": 999,
            },
            "saldos_iniciales": self._SALDOS,
            "depreciacion_ppe": self._DEP_PPE,
        }
        inputs = inputs_from_json(body)
        self.assertEqual([ln.gasto_depreciacion for ln in inputs.er_mensual],
                         [11_666.67, 11_666.67, 11_666.67])

    def test_sin_bloque_manda_el_valor_manual(self):
        from motor.json_io import inputs_from_json

        body = {
            "periodo": {"tipo": "A", "mes_inicial": "2026-01", "mes_final": "2026-01", "tasa_cambio": 36.6},
            "datos": {"nombre_completo": "X", "cedula": "001", "empleados": 1,
                      "fecha_certificacion": "2026-02-01"},
            "er_mensual": [{"mes": "2026-01", "ingresos": 100, "gasto_depreciacion": 999}],
            "saldos_iniciales": self._SALDOS,
        }
        inputs = inputs_from_json(body)
        self.assertEqual(inputs.er_mensual[0].gasto_depreciacion, 999.0)

    def test_e2e_tipo_b_con_depreciacion_ppe_valida_ok(self):
        from motor.json_io import inputs_tipo_b_from_json
        from motor import certificar_tipo_b

        body = {
            "periodo": {"tipo": "B", "mes_inicial": "2026-01", "mes_final": "2026-12", "tasa_cambio": 36.6243},
            "datos": {"nombre_completo": "Demo Dep PPE", "cedula": "001-000000-0000X", "empleados": 1,
                      "fecha_certificacion": "2027-01-15"},
            "er_modo": "generado",
            "er_generado": {
                "ingreso_base": 200_000, "banda_ingreso_pct": 20,
                "costo_pct_sobre_venta": 60, "banda_costo_pct": 5,
                "seed": "e2e-dep",
                "gastos_fijos": {"sueldos_salarios": 15_000, "renta": 8_000},
            },
            "saldos_iniciales": {"efectivo": 20_000, "mobiliario_equipos": 240_000},
            "depreciacion_ppe": {"mobiliario_equipos": {"pct_depreciable": 100, "vida_util_anios": 5}},
            "cuentas_objetivo": [{"cuenta": "efectivo", "objetivo": 120_000, "tolerancia_pct": 20}],
            "seed": "e2e-caja",
        }
        modelo = certificar_tipo_b(inputs_tipo_b_from_json(body))
        errores = [f"inv#{h.invariante}: {h.mensaje}" for h in modelo.validacion.errores]
        self.assertTrue(modelo.ok, "Errores:\n" + "\n".join(errores))
        # 240,000 al 100% en 5 anios = 4,000/mes en el ER de todos los meses
        self.assertEqual(
            [modelo.er.depreciacion_mes[m] for m in modelo.er.meses],
            [4_000.0] * 12,
        )


if __name__ == "__main__":
    unittest.main()

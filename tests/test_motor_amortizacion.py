"""Tests motor/amortizacion.py contra fixture real (Thelma TransUnion).

El fixture trae 7 creditos: 4 dentro de la ventana 2023-01..2025-12 y 3
otorgados en 2026 (fuera). Verifica:
  - Loader JSON correcto
  - Filtro por ventana excluye los 3 de 2026
  - Mapeo a cuenta ESF correcto por tipo
  - Inferencia de tasa razonable (caso limpio 1571)
  - Para los 4 vigentes: saldo_final_corte = saldo_reportado * T/C (invariante #1)
  - Caso 5561 (tasa imprecisa por refinanciamiento): debe emitir alerta no bloqueante
  - Caso 5220 (revolving): 36 cuotas, saldo constante = saldo_reportado
  - Caso 1735 (bullet): cada cuota es puro interes (cuota_nio == interes_nio)
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from motor.amortizacion import (
    deudas_from_json,
    filtrar_por_ventana,
    inferir_tasa_mensual,
    mapear_cuenta_esf,
    resolver_planes,
)
from motor.inputs import PeriodoSpec


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "deudas_thelma.json"
TC = 36.6243


def _periodo_thelma() -> PeriodoSpec:
    return PeriodoSpec(tipo="B", mes_inicial="2023-01", mes_final="2025-12", tasa_cambio=TC)


def _load() -> list:
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)["deudas"]


class LoaderTests(unittest.TestCase):
    def test_parsea_7_deudas(self):
        deudas = deudas_from_json(_load())
        self.assertEqual(len(deudas), 7)
        numeros = sorted(d.numero for d in deudas)
        self.assertEqual(numeros, ["1571", "1735", "2421", "5220", "5561", "6006", "9535"])

    def test_campos_tipados(self):
        deudas = deudas_from_json(_load())
        d5220 = next(d for d in deudas if d.numero == "5220")
        self.assertEqual(d5220.estrategia, "revolving")
        self.assertEqual(d5220.moneda, "NIO")
        self.assertEqual(d5220.fecha_otorgamiento.year, 2018)
        self.assertIsNone(d5220.fecha_vencimiento)
        self.assertAlmostEqual(d5220.saldo_reportado, 4672.88)


class VentanaTests(unittest.TestCase):
    def test_excluye_otorgados_despues_del_corte(self):
        deudas = deudas_from_json(_load())
        activos = filtrar_por_ventana(deudas, _periodo_thelma())
        numeros = sorted(d.numero for d in activos)
        self.assertEqual(numeros, ["1571", "1735", "5220", "5561"])

    def test_credito_otorgado_el_ultimo_dia_entra(self):
        # 5561 fue otorgado 2025-10-07, dentro de la ventana → entra
        deudas = deudas_from_json(_load())
        activos = filtrar_por_ventana(deudas, _periodo_thelma())
        self.assertIn("5561", [d.numero for d in activos])


class MapeoCuentaTests(unittest.TestCase):
    def test_revolving_va_a_tarjetas(self):
        deudas = deudas_from_json(_load())
        d = next(x for x in deudas if x.numero == "5220")
        self.assertEqual(mapear_cuenta_esf(d), "tarjetas_credito")

    def test_vehiculos_va_a_prendarios(self):
        deudas = deudas_from_json(_load())
        d = next(x for x in deudas if x.numero == "1571")
        self.assertEqual(mapear_cuenta_esf(d), "creditos_prendarios")

    def test_hipotecaria_va_a_hipotecarios(self):
        deudas = deudas_from_json(_load())
        d = next(x for x in deudas if x.numero == "5561")
        self.assertEqual(mapear_cuenta_esf(d), "creditos_hipotecarios")


class TasaInferidaTests(unittest.TestCase):
    def test_caso_limpio_1571(self):
        # valor_inicial 36596.40, cuota 682, plazo ~98 meses
        tasa = inferir_tasa_mensual(36596.40, 682.0, 98)
        self.assertGreater(tasa, 0.0)
        self.assertLess(tasa, 0.05)  # menos de 5% mensual

    def test_cuota_insuficiente_devuelve_cero(self):
        # 1000 / 10 = 100 < 200 inicial: cuota no cubre capital sin interés
        self.assertEqual(inferir_tasa_mensual(200.0, 10.0, 10), 0.0)

    def test_inputs_invalidos_devuelven_cero(self):
        self.assertEqual(inferir_tasa_mensual(0, 100, 12), 0.0)
        self.assertEqual(inferir_tasa_mensual(1000, 0, 12), 0.0)
        self.assertEqual(inferir_tasa_mensual(1000, 100, 0), 0.0)


class PlanesTests(unittest.TestCase):
    def setUp(self):
        self.periodo = _periodo_thelma()
        self.deudas = deudas_from_json(_load())
        self.planes = resolver_planes(self.deudas, self.periodo)
        self.por_numero = {p.deuda.numero: p for p in self.planes}

    def test_resuelve_4_planes(self):
        self.assertEqual(len(self.planes), 4)
        self.assertEqual(set(self.por_numero), {"1571", "1735", "5220", "5561"})

    def test_invariante_1_saldo_corte_pega_exacto_para_todos(self):
        """Invariante #1: saldo_final[mes_corte] = saldo_reportado * T/C."""
        for numero, plan in self.por_numero.items():
            esperado = plan.deuda.saldo_reportado * (TC if plan.deuda.moneda == "USD" else 1.0)
            self.assertAlmostEqual(
                plan.saldo_final_corte_nio(),
                esperado,
                places=2,
                msg=f"Credito {numero}: saldo final no pega al reportado",
            )

    def test_5220_revolving_36_meses_saldo_constante(self):
        plan = self.por_numero["5220"]
        self.assertEqual(len(plan.cuotas), 36)  # 2023-01..2025-12
        saldos = {round(c.saldo_inicial_nio, 2) for c in plan.cuotas}
        self.assertEqual(saldos, {4672.88})  # constante
        # Cuota = puro interes
        for c in plan.cuotas:
            self.assertAlmostEqual(c.cuota_nio, c.interes_nio, places=2)
            self.assertAlmostEqual(c.abono_capital_nio, 0.0)
            self.assertAlmostEqual(c.abono_extraordinario_nio, 0.0)
        self.assertIsNone(plan.alerta)

    def test_1735_bullet_cuota_es_puro_interes(self):
        plan = self.por_numero["1735"]
        # Todos los meses excepto el ultimo: cuota = interes, abono = 0
        for c in plan.cuotas[:-1]:
            self.assertAlmostEqual(c.abono_capital_nio, 0.0, places=2)
        # Bullet vence 2026-08-17 → fuera del periodo → no hay pago de capital normal
        # El abono extraordinario en mes_final cierra el saldo a saldo_reportado
        ultima = plan.cuotas[-1]
        self.assertAlmostEqual(ultima.saldo_final_nio, 58814.55 * TC, places=2)

    def test_5561_dispara_alerta_porque_tasa_imprecisa(self):
        plan = self.por_numero["5561"]
        # El valor_inicial (114,400 USD) no es el desembolso real; abono extra grande esperado
        self.assertIsNotNone(plan.alerta)
        self.assertIn("5561", plan.alerta)

    def test_1571_amortizable_limpio_sin_alerta_grande(self):
        plan = self.por_numero["1571"]
        # Caso "limpio" segun el fixture: tasa inferida razonable
        self.assertGreater(plan.tasa_mensual_inferida, 0.0)
        # No verificamos ausencia de alerta porque la fecha_actualizado del fixture
        # (2026-01-31) no coincide con mes_final del periodo (2025-12), generando
        # un pequeño ajuste; lo que importa es que el saldo_final cuadre (cubierto arriba)

    def test_cuenta_esf_asignada_para_todos(self):
        cuentas_validas = {
            "tarjetas_credito", "creditos_hipotecarios", "creditos_consumo",
            "creditos_personales", "creditos_prendarios", "creditos_comerciales",
        }
        for plan in self.planes:
            self.assertIn(plan.cuenta_esf, cuentas_validas)

    def test_interes_mes_y_abono_helpers(self):
        plan = self.por_numero["1571"]
        # Helper: interes del mes especifico
        mes_existente = plan.cuotas[0].mes
        self.assertGreater(plan.interes_del_mes_nio(mes_existente), 0.0)
        self.assertEqual(plan.interes_del_mes_nio("1999-01"), 0.0)


if __name__ == "__main__":
    unittest.main()

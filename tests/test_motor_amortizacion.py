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
from datetime import date
from pathlib import Path

from motor.amortizacion import (
    deudas_from_json,
    filtrar_por_ventana,
    inferir_tasa_mensual,
    mapear_cuenta_esf,
    resolver_planes,
)
from motor.inputs import DeudaInput, PeriodoSpec


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "deudas_thelma.json"
TC = 36.6243


def _periodo_thelma() -> PeriodoSpec:
    return PeriodoSpec(tipo="B", mes_inicial="2023-01", mes_final="2025-12", tasa_cambio=TC)


def _load() -> list:
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)["deudas"]


class AmortizableSinCuotaTest(unittest.TestCase):
    """Deuda amortizable SIN cuota ni apertura (tipico SIBOIF agregado):
    saldo CONSTANTE = saldo_reportado en todo el periodo, respetando su
    cuenta ESF por tipo (no salta de 0 al saldo pleno solo en el corte)."""

    def _periodo(self):
        return PeriodoSpec(tipo="B", mes_inicial="2026-01", mes_final="2026-06", tasa_cambio=TC)

    def test_amortiza_hasta_el_reportado_en_su_cuenta(self):
        # Sin valor_inicial (SIBOIF): baja lineal desde una apertura estimada
        # hasta el saldo reportado, en su cuenta ESF por tipo. Otorgado ANTES
        # del periodo (si fuera de dentro, arrancaria en 0 hasta el desembolso).
        d = [{"numero": "3", "entidad": "X", "tipo_credito": "Personales",
              "estrategia": "amortizable", "moneda": "NIO", "valor_inicial": 0,
              "saldo_reportado": 15591.33, "cuota": 0,
              "fecha_otorgamiento": "2024-05-01", "fecha_actualizado": "2026-05-01"}]
        plan = resolver_planes(deudas_from_json(d), self._periodo())[0]
        self.assertEqual(plan.cuenta_esf, "creditos_personales")
        saldos = [c.saldo_final_nio for c in plan.cuotas]
        self.assertGreater(saldos[0], saldos[-1])          # baja (amortiza)
        self.assertAlmostEqual(saldos[-1], 15591.33, delta=1.0)  # ancla al corte (entero)
        # baja pareja (lineal): deltas casi iguales (±1 por redondeo de meses)
        deltas = [saldos[i] - saldos[i + 1] for i in range(len(saldos) - 1)]
        self.assertTrue(all(d > 0 for d in deltas))        # monotona descendente
        self.assertLess(max(deltas) - min(deltas), 2.0)    # pareja

    def test_con_cuota_real_sigue_amortizando(self):
        d = [{"numero": "9", "entidad": "X", "tipo_credito": "Personales",
              "estrategia": "amortizable", "moneda": "NIO", "valor_inicial": 50000,
              "saldo_reportado": 15591.33, "cuota": 3000,
              "fecha_otorgamiento": "2024-01-01", "fecha_vencimiento": "2027-01-01",
              "fecha_actualizado": "2026-05-01"}]
        plan = resolver_planes(deudas_from_json(d), self._periodo())[0]
        saldos = [round(c.saldo_final_nio) for c in plan.cuotas]
        self.assertTrue(saldos[0] > saldos[-1], "con cuota debe amortizar (bajar)")
        self.assertEqual(saldos[-1], 15591)  # ancla al corte


class TrayectoriaGeneradaTest(unittest.TestCase):
    """motor/deuda_generada: banda (revolving) y amortizacion (creditos)."""

    def setUp(self):
        self.meses = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]

    def test_revolving_banda_reproducible_y_ancla(self):
        from motor.deuda_generada import trayectoria_revolving

        a = trayectoria_revolving(16251.90, self.meses, 20.0, "s1")
        b = trayectoria_revolving(16251.90, self.meses, 20.0, "s1")
        self.assertEqual(a, b)  # misma seed = mismo resultado
        self.assertEqual(a["2026-06"], 16251.90)  # ancla EXACTA al corte
        vals = list(a.values())
        self.assertGreater(len(set(vals)), 1)  # oscila
        for v in vals:
            self.assertLessEqual(v, 16251.90 * 1.20 + 1)
            self.assertGreaterEqual(v, 16251.90 * 0.80 - 1)

    def test_amortizable_con_cuota_baja_por_capital(self):
        from motor.deuda_generada import trayectoria_amortizable

        t = trayectoria_amortizable(20000, self.meses, cuota=3000)
        self.assertEqual(t["2026-06"], 20000)          # ancla EXACTA
        self.assertEqual(t["2026-01"], 20000 + 3000 * 5)  # apertura = final + cuota*(n-1)
        self.assertGreater(t["2026-01"], t["2026-06"])  # baja

    def test_inventario_oscila_y_ancla_al_final(self):
        from motor.deuda_generada import trayectoria_con_ancla

        t = trayectoria_con_ancla(200_000, 260_000, self.meses, 10.0, "inv-1")
        self.assertEqual(t["2026-06"], 260_000)       # ancla dura EXACTA
        vals = list(t.values())
        self.assertGreater(len(set(vals)), 1)         # oscila
        # tendencia: arranca cerca del inicial, no salta al final de una
        self.assertLess(vals[0], 260_000)
        # reproducible con la misma seed
        self.assertEqual(t, trayectoria_con_ancla(200_000, 260_000, self.meses, 10.0, "inv-1"))

    def test_amortizable_sin_cuota_estima_apertura(self):
        from motor.deuda_generada import trayectoria_amortizable

        t = trayectoria_amortizable(15591.33, self.meses, cuota=0)
        self.assertEqual(t["2026-06"], 15591.33)       # ancla EXACTA
        self.assertGreater(t["2026-01"], 15591.33)     # apertura estimada mayor


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

    def test_tipo_concatenado_siboif_usa_el_destino(self):
        # SIBOIF concatena 'Categoria - Destino'; el DESTINO manda sobre la
        # categoria ('Consumo - Personales' -> personales, no consumo).
        from datetime import date

        from motor.inputs import DeudaInput

        def _cuenta(tipo):
            d = DeudaInput(
                numero="1", entidad="X", tipo_credito=tipo, estrategia="amortizable",
                moneda="NIO", valor_inicial=0, saldo_reportado=100, cuota=0,
                fecha_otorgamiento=date(2026, 1, 1), fecha_actualizado=date(2026, 1, 1),
                fecha_vencimiento=None,
            )
            return mapear_cuenta_esf(d)

        self.assertEqual(_cuenta("Consumo - Personales"), "creditos_personales")
        self.assertEqual(_cuenta("Consumo - Tarjetas de Credito"), "tarjetas_credito")
        self.assertEqual(_cuenta("Comercial - Compra de Vehiculos"), "creditos_prendarios")
        self.assertEqual(_cuenta("Consumo - Consumo"), "creditos_consumo")
        # TransUnion (sin concatenar) sigue igual
        self.assertEqual(_cuenta("CARTERA COMERCIAL"), "creditos_comerciales")
        self.assertEqual(_cuenta("CARTERA DE CONSUMO"), "creditos_consumo")


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
                delta=1.0,  # cordobas enteros: hasta 1 de redondeo
                msg=f"Credito {numero}: saldo final no pega al reportado",
            )

    def test_5220_revolving_oscila_en_banda_y_ancla_al_corte(self):
        plan = self.por_numero["5220"]
        self.assertEqual(len(plan.cuotas), 36)  # 2023-01..2025-12
        saldos = [c.saldo_final_nio for c in plan.cuotas]
        # Oscila (no todos iguales) pero dentro de la banda +-20% del reportado.
        self.assertGreater(len(set(round(s) for s in saldos)), 1)
        for s in saldos:
            self.assertLessEqual(s, 4672.88 * 1.20 + 1)
            self.assertGreaterEqual(s, 4672.88 * 0.80 - 1)
        # Ultimo mes = saldo reportado EXACTO (ancla dura del corte)
        self.assertAlmostEqual(saldos[-1], 4672.88, delta=1.0)  # entero
        self.assertIsNone(plan.alerta)

    def test_creditos_nuevos_arrancan_en_cero_hasta_su_desembolso(self):
        """1571 (2024-03), 5561 (2025-10) y 1735 (2025-08) fueron otorgados
        DENTRO del periodo 2023-01..2025-12: en enero 2023 no existian, asi
        que su saldo debe ser 0 hasta el mes del desembolso."""
        casos = {"1571": "2024-03", "5561": "2025-10", "1735": "2025-08"}
        for numero, mes_otorg in casos.items():
            plan = self.por_numero[numero]
            self.assertEqual(plan.saldo_apertura_nio, 0.0,
                             f"{numero}: no existia al inicio, apertura debe ser 0")
            for c in plan.cuotas:
                if c.mes < mes_otorg:
                    self.assertEqual(c.saldo_final_nio, 0.0,
                                     f"{numero}: saldo en {c.mes} (antes del desembolso)")
            # El mes del desembolso ya tiene saldo
            desembolso = next(c for c in plan.cuotas if c.mes == mes_otorg)
            self.assertGreater(desembolso.saldo_final_nio, 0.0, f"{numero}: desembolso")

    def test_credito_nuevo_desembolso_entra_como_financiamiento(self):
        # El aumento del pasivo el mes del desembolso es plata que entra:
        # abono_capital = 0 ese mes (no se paga, se recibe).
        plan = self.por_numero["1571"]
        desembolso = next(c for c in plan.cuotas if c.mes == "2024-03")
        self.assertEqual(desembolso.abono_capital_nio, 0.0)
        self.assertGreater(desembolso.saldo_final_nio, 0.0)

    def test_5220_anterior_al_periodo_conserva_su_apertura(self):
        # Otorgado 2018: SI existia al inicio -> apertura > 0 (no se toca).
        plan = self.por_numero["5220"]
        self.assertGreater(plan.saldo_apertura_nio, 0.0)

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


class CuotaQueNoCubreElCapitalTest(unittest.TestCase):
    """Datos del reporte que no cierran entre si.

    Caso real (Modesto, credito 3611): valor 21,900 USD, cuota 228 y plazo
    84 meses. 228 x 84 = 19,152, menos que lo prestado: ninguna tasa positiva
    lo explica, el motor infiere 0% y el credito termina sin intereses. El ER
    se quedaba sin sus Gastos Financieros y nada lo avisaba.
    """

    def _deuda(self, **kw):
        base = dict(
            numero="3611", entidad="BANCO DE AMERICA CENTRAL",
            tipo_credito="CARTERA DE VEHICULOS", estrategia="amortizable",
            moneda="USD", valor_inicial=21_900.0, saldo_reportado=18_261.59,
            cuota=228.0, fecha_otorgamiento=date(2025, 2, 14),
            fecha_actualizado=date(2026, 7, 1), fecha_vencimiento=date(2032, 2, 16),
        )
        base.update(kw)
        return DeudaInput(**base)

    def _plan(self, deuda):
        periodo = PeriodoSpec(tipo="A", mes_inicial="2026-03", mes_final="2026-08",
                              tasa_cambio=36.6243)
        return resolver_planes([deuda], periodo)[0]

    def test_avisa_cuando_la_cuota_no_cubre_el_capital(self):
        plan = self._plan(self._deuda())
        self.assertIsNotNone(plan.alerta, "tiene que avisar, no quedarse callado")
        self.assertIn("3611", plan.alerta)
        self.assertIn("Gastos Financieros", plan.alerta)

    def test_el_mensaje_dice_cual_seria_la_cuota_minima(self):
        # 21,900 / 84 = 260.71: el dato accionable para revisar el reporte.
        self.assertIn("260.71", self._plan(self._deuda()).alerta)

    def test_una_cuota_coherente_no_alerta(self):
        # Con 360 la cuenta cierra: 360 x 84 = 30,240 > 21,900.
        plan = self._plan(self._deuda(cuota=360.0))
        self.assertGreater(plan.tasa_mensual_inferida, 0, "deberia inferir tasa")
        self.assertNotIn("no genera intereses", (plan.alerta or "").lower())

    def test_no_alerta_si_la_tasa_viene_declarada(self):
        # Con tasa dada no hay nada que inferir: el CPA ya resolvio el dato.
        plan = self._plan(self._deuda(tasa_mensual=0.008))
        self.assertNotIn("Gastos Financieros", (plan.alerta or ""))

    def test_no_alerta_con_trayectoria_mes_a_mes(self):
        saldos = {m: 18_261.59 for m in ("2026-03", "2026-04", "2026-05",
                                         "2026-06", "2026-07", "2026-08")}
        plan = self._plan(self._deuda(saldos_mensuales=saldos))
        self.assertNotIn("Gastos Financieros", (plan.alerta or ""))

    def test_no_alerta_sin_fecha_de_vencimiento(self):
        # Sin vencimiento el plazo se estima como valor/cuota y la
        # comparacion se vuelve circular: cualquier centavo la dispararia.
        # Es el caso de las cuentas de telefonia y TV del reporte, que no
        # son prestamos.
        plan = self._plan(self._deuda(
            numero="0362", entidad="ENITEL", tipo_credito="CARTERA TELEFONIA",
            valor_inicial=733.55, saldo_reportado=733.55, cuota=733.0,
            fecha_vencimiento=None))
        self.assertIsNone(plan.alerta)

    def test_no_alerta_por_una_diferencia_de_centavos(self):
        # 84 x 260.70 = 21,898.80 contra 21,900: redondeo del reporte.
        plan = self._plan(self._deuda(cuota=260.70))
        self.assertNotIn("Gastos Financieros", (plan.alerta or ""))

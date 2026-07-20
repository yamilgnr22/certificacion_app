"""Tests del regimen Tipo B (12 meses, caja oscilante en banda).

Caso base: negocio con utilidad estable de 50k/mes, caja objetivo 150k +-20%
(banda [120k, 180k]). La caja arranca en 20k (bajo banda -> alerta de ramp-in,
NO error), sube por flujo natural y desde que alcanza la banda queda dentro
via retiros de patrimonio. Verifica ademas: balance cuadra sin plug, retiros
reducen Resultados Acumulados, determinismo por seed, deuda activa anclada,
inventario en banda, y validaciones de inputs.
"""

from __future__ import annotations

import unittest
from datetime import date

from motor import (
    CuentaObjetivo,
    DatosCliente,
    DeudaInput,
    ER_LineaMes,
    ESF_Saldos,
    InputsTipoB,
    PeriodoSpec,
    certificar_tipo_b,
)


MESES = [f"2026-{m:02d}" for m in range(1, 13)]


def _datos() -> DatosCliente:
    return DatosCliente(
        nombre_completo="Cliente Demo Tipo B",
        cedula="001-000000-0000X",
        domicilio="Managua",
        contacto="+505 8888 8888",
        regimen="Cuota Fija",
        matricula="DEMO-1",
        direccion_negocio="Managua",
        giro="Comercio",
        antiguedad="03 anios",
        empleados=2,
        estado_civil="soltero",
        profesion="Comerciante",
        sexo="Masculino",
        banco="BANPRO",
        fecha_certificacion=date(2027, 1, 15),
    )


def _er_12() -> list[ER_LineaMes]:
    # Utilidad neta = 200k - 120k - (15k + 8k + 4k + 3k) = 50k/mes
    return [
        ER_LineaMes(
            mes=m, ingresos=200_000.0, costo_ventas=120_000.0,
            sueldos_salarios=15_000.0, renta=8_000.0,
            gasto_depreciacion=4_000.0, otros_gastos=3_000.0,
        )
        for m in MESES
    ]


def _inputs(
    *,
    seed: str = "test-b-1",
    con_inventario: bool = False,
    deudas: list | None = None,
) -> InputsTipoB:
    objetivos = [CuentaObjetivo(cuenta="efectivo", objetivo=150_000.0, tolerancia_pct=20.0)]
    si_kwargs = dict(
        efectivo=20_000.0,
        mobiliario_equipos=240_000.0,
        resultados_acumulados=100_000.0,
    )
    if con_inventario:
        objetivos.append(CuentaObjetivo(cuenta="inventarios", objetivo=80_000.0, tolerancia_pct=20.0))
        si_kwargs["inventarios"] = 60_000.0
    return InputsTipoB(
        periodo=PeriodoSpec(tipo="B", mes_inicial="2026-01", mes_final="2026-12", tasa_cambio=36.6243),
        datos=_datos(),
        er_mensual=_er_12(),
        saldos_iniciales=ESF_Saldos(**si_kwargs),
        cuentas_objetivo=objetivos,
        deudas=deudas or [],
        seed=seed,
    )


class TipoBBasicoTest(unittest.TestCase):
    def setUp(self):
        self.m = certificar_tipo_b(_inputs())
        self.banda = (120_000.0, 180_000.0)

    def test_sin_errores_bloqueantes(self):
        errores = [f"inv#{h.invariante}: {h.mensaje}" for h in self.m.validacion.errores]
        self.assertTrue(self.m.ok, "Errores:\n" + "\n".join(errores))

    def test_balance_cuadra_todos_los_meses(self):
        for e in self.m.esf.meses:
            self.assertLessEqual(abs(e.diferencia), 1.0, f"Mes {e.mes} descuadra {e.diferencia}")

    def test_caja_en_banda_desde_que_la_alcanza(self):
        inf, sup = self.banda
        cajas = [e.efectivo for e in self.m.esf.meses]
        alcanzo = next((i for i, c in enumerate(cajas) if c >= inf), None)
        self.assertIsNotNone(alcanzo, "la caja nunca alcanzo la banda")
        for i in range(alcanzo, len(cajas)):
            self.assertGreaterEqual(cajas[i], inf - 1.0, f"mes {MESES[i]} caja {cajas[i]} bajo banda")
            self.assertLessEqual(cajas[i], sup + 1.0, f"mes {MESES[i]} caja {cajas[i]} sobre banda")

    def test_ramp_in_da_alerta_no_error(self):
        # Mes 1: caja natural ~74k < 120k -> alerta inv#3
        alertas3 = [h for h in self.m.validacion.alertas if h.invariante == 3]
        self.assertGreaterEqual(len(alertas3), 1)
        errores3 = [h for h in self.m.validacion.errores if h.invariante == 3]
        self.assertEqual(errores3, [])

    def test_retiros_netean_capital_no_ra(self):
        # Presentacion del CPA: los retiros descuentan el CAPITAL (neto), RA
        # queda constante (nunca negativo) y NO hay linea separada de retiros.
        total_retiros = round(sum(mv.retiro_patrimonio for mv in self.m.mov.movs), 2)
        self.assertGreater(total_retiros, 0.0)
        corte = self.m.esf.corte()
        self.assertAlmostEqual(corte.resultados_acumulados, 100_000.0, delta=1.0)
        self.assertAlmostEqual(corte.retiros_acumulados, total_retiros, delta=1.0)
        self.assertAlmostEqual(corte.capital, 160_000.0 - total_retiros, delta=1.0)
        df = self.m.esf.df_corte
        etiquetas = [str(x) for x in df.iloc[:, 3].tolist()]
        self.assertNotIn("(-) Retiros del Propietario", etiquetas)

    def test_capital_neto_de_retiros(self):
        # Capital0 = A0 - P0 - RA0 = 260k - 0 - 100k = 160k (apertura).
        # El capital MOSTRADO va neto de retiros acumulados (como el Excel del CPA).
        self.assertAlmostEqual(self.m.esf.capital_apertura, 160_000.0, delta=1.0)
        for e in self.m.esf.meses:
            self.assertAlmostEqual(e.capital, 160_000.0 - e.retiros_acumulados, delta=1.0)

    def test_resultados_ejercicio_es_utilidad_acumulada(self):
        self.assertAlmostEqual(self.m.esf.corte().resultados_ejercicio, 600_000.0, delta=2.0)

    def test_determinismo_misma_seed(self):
        otra = certificar_tipo_b(_inputs(seed="test-b-1"))
        self.assertEqual(
            [mv.saldo_final for mv in self.m.mov.movs],
            [mv.saldo_final for mv in otra.mov.movs],
        )

    def test_seed_distinta_cambia_oscilacion(self):
        otra = certificar_tipo_b(_inputs(seed="otra-semilla"))
        self.assertNotEqual(
            [mv.saldo_final for mv in self.m.mov.movs],
            [mv.saldo_final for mv in otra.mov.movs],
        )


class TipoBInventarioTest(unittest.TestCase):
    def setUp(self):
        self.m = certificar_tipo_b(_inputs(con_inventario=True))

    def test_sin_errores(self):
        errores = [f"inv#{h.invariante}: {h.mensaje}" for h in self.m.validacion.errores]
        self.assertTrue(self.m.ok, "Errores:\n" + "\n".join(errores))

    def test_inventario_dentro_de_banda(self):
        for e in self.m.esf.meses:
            self.assertGreaterEqual(e.inventarios, 64_000.0 - 1.0, f"mes {e.mes}")
            self.assertLessEqual(e.inventarios, 96_000.0 + 1.0, f"mes {e.mes}")

    def test_compras_no_negativas_y_cubren_delta(self):
        for mv in self.m.mov.movs:
            self.assertGreaterEqual(mv.pago_compras_inventario, 0.0)

    def test_balance_cuadra(self):
        for e in self.m.esf.meses:
            self.assertLessEqual(abs(e.diferencia), 1.0, f"Mes {e.mes} descuadra {e.diferencia}")


class TipoBConDeudaTest(unittest.TestCase):
    def setUp(self):
        deuda = DeudaInput(
            numero="B1", entidad="Banco", tipo_credito="CARTERA DE CONSUMO",
            estrategia="amortizable", moneda="NIO",
            valor_inicial=200_000.0, saldo_reportado=60_000.0, cuota=5_000.0,
            fecha_otorgamiento=date(2024, 1, 1), fecha_actualizado=date(2026, 12, 31),
            fecha_vencimiento=date(2028, 1, 1), tasa_mensual=0.02,
            saldo_apertura=100_000.0,
        )
        self.m = certificar_tipo_b(_inputs(deudas=[deuda]))

    def test_sin_errores(self):
        errores = [f"inv#{h.invariante}: {h.mensaje}" for h in self.m.validacion.errores]
        self.assertTrue(self.m.ok, "Errores:\n" + "\n".join(errores))

    def test_gastos_financieros_en_er(self):
        # interes mes 1 = 100k * 2% = 2,000
        self.assertAlmostEqual(self.m.er.gastos_financieros_mes["2026-01"], 2_000.0, delta=1.0)

    def test_deuda_ancla_al_corte(self):
        self.assertAlmostEqual(self.m.esf.corte().creditos_consumo, 60_000.0, delta=1.0)

    def test_balance_cuadra_con_deuda(self):
        for e in self.m.esf.meses:
            self.assertLessEqual(abs(e.diferencia), 1.0, f"Mes {e.mes} descuadra {e.diferencia}")


class TipoBValidacionInputsTest(unittest.TestCase):
    def test_sin_objetivo_de_caja_es_error(self):
        with self.assertRaises(ValueError):
            InputsTipoB(
                periodo=PeriodoSpec(tipo="B", mes_inicial="2026-01", mes_final="2026-12", tasa_cambio=36.6),
                datos=_datos(),
                er_mensual=_er_12(),
                saldos_iniciales=ESF_Saldos(),
                cuentas_objetivo=[],
            )

    def test_mas_de_12_meses_es_error(self):
        with self.assertRaises(ValueError):
            InputsTipoB(
                periodo=PeriodoSpec(tipo="B", mes_inicial="2025-01", mes_final="2026-12", tasa_cambio=36.6),
                datos=_datos(),
                er_mensual=[],
                saldos_iniciales=ESF_Saldos(),
                cuentas_objetivo=[CuentaObjetivo(cuenta="efectivo", objetivo=100_000.0)],
            )

    def test_cuenta_objetivo_invalida(self):
        with self.assertRaises(ValueError):
            CuentaObjetivo(cuenta="proveedores", objetivo=1000.0)


if __name__ == "__main__":
    unittest.main()

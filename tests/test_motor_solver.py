"""Solver de caja: el efectivo nunca baja del piso, sin romper nada.

El test que protege todo lo demas es `SolverNoTocaLoFactibleTest`: cuando la
trayectoria deseada ya cumple, el solver debe devolver las MISMAS cifras. Si
alguna vez falla, el solver esta interviniendo donde no debe.
"""

from __future__ import annotations

import unittest
from datetime import date

from motor import (
    Bandas,
    CuentaObjetivo,
    DatosCliente,
    ER_LineaMes,
    ESF_Saldos,
    InputsTipoA,
    InputsTipoB,
    Minimos,
    PeriodoSpec,
    certificar_tipo_a,
    certificar_tipo_b,
)
from motor.solver import Palanca, repartir, resolver_caja

MESES = [f"2026-{m:02d}" for m in range(1, 7)]


def _datos() -> DatosCliente:
    return DatosCliente(
        nombre_completo="Ana Maria Lopez Ruiz", cedula="001-010190-0001A",
        domicilio="Managua", contacto="+505 8888 8888", regimen="Cuota Fija",
        matricula="RNVD-000001", direccion_negocio="Managua", giro="Pulperia",
        antiguedad="05 anios", empleados=1, estado_civil="soltera",
        profesion="Comerciante", sexo="Femenino", banco="BAC",
        fecha_certificacion=date(2026, 7, 1),
    )


# ------------------------------------------------------------------ reparto

class RepartoOptimoTest(unittest.TestCase):
    def _palancas(self):
        return [
            Palanca("inventarios", 26_000_000, -1, escala=2_600_000, minimo=20_000_000),
            Palanca("proveedores", 3_500_000, +1, escala=350_000),
            Palanca("cuentas_por_cobrar", 1_900_000, -1, escala=190_000),
        ]

    def test_cubre_el_deficit_exacto(self):
        mov, faltante = repartir(900_000, self._palancas())
        self.assertEqual(faltante, 0.0)
        self.assertAlmostEqual(sum(mov.values()), 900_000, delta=1.0)

    def test_la_cuenta_de_banda_mas_ancha_absorbe_mas(self):
        # El costo se normaliza por la banda: mover una cuenta que ya oscila
        # mucho se nota menos, asi que se la mueve mas.
        mov, _ = repartir(900_000, self._palancas())
        self.assertGreater(mov["inventarios"], mov["proveedores"])
        self.assertGreater(mov["proveedores"], mov["cuentas_por_cobrar"])

    def test_reparte_en_proporcion_al_cuadrado_de_la_banda(self):
        mov, _ = repartir(900_000, self._palancas())
        # bandas 2.6M : 350k => aportes en relacion (2.6/0.35)^2
        esperado = (2_600_000 / 350_000) ** 2
        self.assertAlmostEqual(mov["inventarios"] / mov["proveedores"], esperado, delta=0.5)

    def test_respeta_el_minimo_y_redistribuye(self):
        palancas = self._palancas()
        palancas[0] = Palanca("inventarios", 26_000_000, -1,
                              escala=2_600_000, minimo=25_900_000)  # solo 100k
        mov, faltante = repartir(900_000, palancas)
        self.assertEqual(mov["inventarios"], 100_000)
        self.assertEqual(faltante, 0.0)
        self.assertAlmostEqual(sum(mov.values()), 900_000, delta=1.0)

    def test_reporta_faltante_cuando_todo_topa(self):
        p = Palanca("inventarios", 100.0, -1, escala=10.0, minimo=100.0)
        mov, faltante = repartir(5_000, [p])
        self.assertEqual(mov, {})
        self.assertEqual(faltante, 5_000)

    def test_sin_deficit_no_mueve_nada(self):
        self.assertEqual(repartir(0.0, self._palancas()), ({}, 0.0))
        self.assertEqual(repartir(-50_000, self._palancas()), ({}, 0.0))

    def test_es_reproducible(self):
        a, _ = repartir(900_000, self._palancas())
        b, _ = repartir(900_000, self._palancas())
        self.assertEqual(a, b)


class ResolverCajaTest(unittest.TestCase):
    def test_el_aporte_es_el_ultimo_recurso(self):
        # Palanca sin capacidad: todo el deficit cae en el aporte.
        palancas = {"2026-01": [Palanca("inventarios", 100.0, -1, escala=10.0, minimo=100.0)]}
        r = resolver_caja(["2026-01"], {"2026-01": -50_000}, 0.0, palancas,
                          permite_aporte=True)
        self.assertEqual(r.aporte_total, 50_000)
        self.assertEqual(r.faltante_total, 0.0)

    def test_el_tope_de_aporte_deja_faltante(self):
        palancas = {"2026-01": [Palanca("inventarios", 100.0, -1, escala=10.0, minimo=100.0)]}
        r = resolver_caja(["2026-01"], {"2026-01": -50_000}, 0.0, palancas,
                          aporte_maximo=30_000, permite_aporte=True)
        self.assertEqual(r.aporte_total, 30_000)
        self.assertEqual(r.faltante_total, 20_000)

    def test_sin_permiso_no_aporta_y_reporta_faltante(self):
        palancas = {"2026-01": [Palanca("inventarios", 100.0, -1, escala=10.0, minimo=100.0)]}
        r = resolver_caja(["2026-01"], {"2026-01": -50_000}, 0.0, palancas,
                          permite_aporte=False)
        self.assertEqual(r.aporte_total, 0.0)
        self.assertEqual(r.faltante_total, 50_000)

    def test_el_tope_de_aporte_es_del_periodo_no_del_mes(self):
        palancas = {m: [] for m in ("2026-01", "2026-02")}
        r = resolver_caja(["2026-01", "2026-02"],
                          {"2026-01": -30_000, "2026-02": -30_000}, 0.0, palancas,
                          aporte_maximo=40_000, permite_aporte=True)
        self.assertEqual(r.aporte_total, 40_000)
        self.assertEqual(r.faltante_total, 20_000)


# -------------------------------------------------------------- integracion

def _inputs_tipo_a(minimos: Minimos, banda_inv: float = 40.0) -> InputsTipoA:
    """Caja que se hunde a mitad del periodo: el inventario oscila fuerte
    (banda 40%) contra un efectivo inicial chico."""
    er = [ER_LineaMes(mes=m, ingresos=500_000, costo_ventas=300_000) for m in MESES]
    return InputsTipoA(
        periodo=PeriodoSpec(tipo="A", mes_inicial=MESES[0], mes_final=MESES[-1], tasa_cambio=36.6),
        datos=_datos(), er_mensual=er,
        saldos_iniciales=ESF_Saldos(efectivo=500_000, inventarios=5_000_000),
        saldos_finales=ESF_Saldos(efectivo=1_700_000, inventarios=5_000_000),
        bandas=Bandas(inventario_pct=banda_inv), minimos=minimos,
    )


class SolverNoTocaLoFactibleTest(unittest.TestCase):
    """La red de seguridad: sin deficit, el solver no existe."""

    def test_no_interviene(self):
        # Banda chica => la caja nunca baja de cero => nada que corregir.
        m = certificar_tipo_a(_inputs_tipo_a(Minimos(), banda_inv=5.0))
        self.assertIsNone(m.solver)

    def test_las_cifras_son_identicas_con_y_sin_piso(self):
        sin = certificar_tipo_a(_inputs_tipo_a(Minimos(), banda_inv=5.0))
        con = certificar_tipo_a(_inputs_tipo_a(Minimos(caja=0), banda_inv=5.0))
        self.assertEqual([e.efectivo for e in sin.esf.meses],
                         [e.efectivo for e in con.esf.meses])


class SolverTipoATest(unittest.TestCase):
    def setUp(self):
        self.sin_piso = certificar_tipo_a(_inputs_tipo_a(Minimos(caja=0)))
        self.con_piso = certificar_tipo_a(_inputs_tipo_a(Minimos(caja=200_000)))

    def test_la_caja_nunca_baja_de_cero(self):
        self.assertTrue(all(e.efectivo >= 0 for e in self.sin_piso.esf.meses),
                        [e.efectivo for e in self.sin_piso.esf.meses])

    def test_la_caja_respeta_el_piso_configurado(self):
        self.assertTrue(all(e.efectivo >= 200_000 for e in self.con_piso.esf.meses),
                        [e.efectivo for e in self.con_piso.esf.meses])

    def test_corrige_lo_justo_y_no_de_mas(self):
        # El mes ajustado queda EXACTAMENTE en el piso, no por encima.
        ajustados = [a.mes for a in self.con_piso.solver.ajustes if a.movimientos]
        self.assertTrue(ajustados)
        por_mes = {e.mes: e.efectivo for e in self.con_piso.esf.meses}
        for mes in ajustados:
            self.assertAlmostEqual(por_mes[mes], 200_000, delta=1.0)

    def test_el_corte_queda_intacto(self):
        for modelo in (self.sin_piso, self.con_piso):
            corte = modelo.esf.corte()
            self.assertAlmostEqual(corte.efectivo, 1_700_000, delta=1.0)
            self.assertAlmostEqual(corte.inventarios, 5_000_000, delta=1.0)

    def test_el_balance_sigue_cuadrando(self):
        for modelo in (self.sin_piso, self.con_piso):
            for e in modelo.esf.meses:
                self.assertLessEqual(abs(e.diferencia), 1.0, f"{e.mes}: {e.diferencia}")

    def test_validacion_ok(self):
        self.assertTrue(self.con_piso.ok, [h.mensaje for h in self.con_piso.validacion.errores])

    def test_informa_lo_que_movio(self):
        alertas = " ".join(h.mensaje for h in self.con_piso.validacion.alertas)
        self.assertIn("Ajuste del solver", alertas)

    def test_es_reproducible(self):
        otro = certificar_tipo_a(_inputs_tipo_a(Minimos(caja=200_000)))
        self.assertEqual([e.efectivo for e in otro.esf.meses],
                         [e.efectivo for e in self.con_piso.esf.meses])

    def test_piso_imposible_es_error_bloqueante(self):
        # Un piso mayor que toda la caja del periodo no se puede sostener:
        # el ultimo mes esta anclado al balance final y no tiene palancas.
        m = certificar_tipo_a(_inputs_tipo_a(Minimos(caja=9_000_000)))
        self.assertFalse(m.ok)
        self.assertTrue(any("no llega" in h.mensaje for h in m.validacion.errores))


def _inputs_tipo_b(minimos: Minimos) -> InputsTipoB:
    er = [ER_LineaMes(mes=m, ingresos=300_000, costo_ventas=240_000,
                      sueldos_salarios=90_000) for m in MESES]
    return InputsTipoB(
        periodo=PeriodoSpec(tipo="B", mes_inicial=MESES[0], mes_final=MESES[-1], tasa_cambio=36.6),
        datos=_datos(), er_mensual=er,
        saldos_iniciales=ESF_Saldos(efectivo=50_000, mobiliario_equipos=400_000),
        cuentas_objetivo=[CuentaObjetivo(cuenta="efectivo", objetivo=200_000, tolerancia_pct=20.0)],
        seed="solver-b", minimos=minimos,
    )


class SolverTipoBTest(unittest.TestCase):
    """Negocio que pierde plata: sin palancas operativas, la unica salida es
    el aporte del propietario."""

    def test_con_los_defaults_la_caja_ya_no_queda_negativa(self):
        # El piso por defecto es 0: sin configurar nada, un negocio que pierde
        # plata deja de mostrar efectivo negativo.
        m = certificar_tipo_b(_inputs_tipo_b(Minimos()))
        self.assertTrue(all(e.efectivo >= 0 for e in m.esf.meses),
                        [e.efectivo for e in m.esf.meses])

    def test_con_piso_entra_el_aporte_y_la_caja_se_sostiene(self):
        m = certificar_tipo_b(_inputs_tipo_b(Minimos(caja=0)))
        self.assertTrue(all(e.efectivo >= 0 for e in m.esf.meses),
                        [e.efectivo for e in m.esf.meses])
        self.assertGreater(m.solver.aporte_total, 0)

    def test_el_aporte_sube_el_capital_y_el_balance_cuadra(self):
        m = certificar_tipo_b(_inputs_tipo_b(Minimos(caja=0)))
        for e in m.esf.meses:
            self.assertLessEqual(abs(e.diferencia), 1.0, f"{e.mes}: {e.diferencia}")
        self.assertIn("Aporte del propietario", m.mov.df["Concepto"].tolist())

    def test_el_tope_de_aporte_bloquea_con_mensaje(self):
        m = certificar_tipo_b(_inputs_tipo_b(Minimos(caja=0, aporte_maximo=1_000)))
        self.assertFalse(m.ok)
        self.assertTrue(any("faltan" in h.mensaje for h in m.validacion.errores))

    def test_aporte_prohibido_es_bloqueante(self):
        m = certificar_tipo_b(_inputs_tipo_b(Minimos(caja=0, aporte_maximo=0)))
        self.assertFalse(m.ok)


class OscilacionSinEstancarseTest(unittest.TestCase):
    """La caminata de Tipo B revierte a la media: ni se pega al tope ni
    repite el mismo saldo dos meses seguidos."""

    def test_no_repite_valores_consecutivos(self):
        from motor.tipo_b import _trayectoria

        for seed in ("a", "b", "c", "d", "e"):
            osc = _trayectoria(seed, 12, 20.0)
            repetidos = [i for i in range(1, len(osc)) if osc[i] == osc[i - 1]]
            self.assertFalse(repetidos, f"seed {seed} se estanco en {repetidos}")

    def test_se_mantiene_dentro_de_la_banda(self):
        from motor.tipo_b import _trayectoria

        for seed in ("a", "b", "c", "d", "e"):
            for o in _trayectoria(seed, 12, 20.0):
                self.assertLessEqual(abs(o), 0.85 * 0.20 + 1e-9)

    def test_vuelve_al_centro(self):
        # Reversion a la media: el promedio del camino ronda el centro.
        from motor.tipo_b import _trayectoria

        osc = _trayectoria("promedio", 60, 20.0)
        self.assertLess(abs(sum(osc) / len(osc)), 0.05)

    def test_es_reproducible(self):
        from motor.tipo_b import _trayectoria

        self.assertEqual(_trayectoria("x", 12, 20.0), _trayectoria("x", 12, 20.0))


class UtilidadObjetivoTest(unittest.TestCase):
    """El objetivo mide, no corrige: las cifras salen iguales con o sin el."""

    def _modelo(self, objetivo=None):
        from motor import UtilidadObjetivo
        er = [ER_LineaMes(mes=m, ingresos=500_000, costo_ventas=300_000) for m in MESES]
        kw = {"utilidad_objetivo": objetivo} if objetivo else {}
        return certificar_tipo_a(InputsTipoA(
            periodo=PeriodoSpec(tipo="A", mes_inicial=MESES[0], mes_final=MESES[-1], tasa_cambio=36.6),
            datos=_datos(), er_mensual=er,
            saldos_iniciales=ESF_Saldos(efectivo=500_000),
            saldos_finales=ESF_Saldos(efectivo=1_700_000), **kw))

    def _medir(self, objetivo):
        from motor.validar import medir_utilidad_objetivo
        m = self._modelo(objetivo)
        return medir_utilidad_objetivo(m.inputs, m.er)

    def test_sin_objetivo_no_mide_nada(self):
        from motor.validar import medir_utilidad_objetivo
        m = self._modelo()
        self.assertIsNone(medir_utilidad_objetivo(m.inputs, m.er))

    def test_no_cambia_ninguna_cifra(self):
        from motor import UtilidadObjetivo
        sin = self._modelo()
        con = self._modelo(UtilidadObjetivo(monto=1_000, moneda="USD"))
        self.assertEqual([e.efectivo for e in sin.esf.meses],
                         [e.efectivo for e in con.esf.meses])

    def test_dentro_del_margen(self):
        from motor import UtilidadObjetivo
        # utilidad = 6 x 200,000 = 1,200,000 => promedio 200,000 NIO
        m = self._medir(UtilidadObjetivo(monto=200_000, moneda="NIO"))
        self.assertTrue(m["dentro"])
        self.assertAlmostEqual(m["desvio_pct"], 0.0, places=1)
        self.assertAlmostEqual(m["falta_nio"], 0.0, delta=1.0)

    def test_por_debajo_informa_cuanto_falta(self):
        from motor import UtilidadObjetivo
        m = self._medir(UtilidadObjetivo(monto=250_000, moneda="NIO"))
        self.assertFalse(m["dentro"])
        self.assertAlmostEqual(m["desvio_pct"], -20.0, places=1)
        self.assertAlmostEqual(m["falta_nio"], 50_000, delta=1.0)

    def test_por_encima_da_falta_negativa(self):
        from motor import UtilidadObjetivo
        m = self._medir(UtilidadObjetivo(monto=100_000, moneda="NIO"))
        self.assertFalse(m["dentro"])
        self.assertAlmostEqual(m["desvio_pct"], 100.0, places=1)
        self.assertLess(m["falta_nio"], 0)

    def test_convierte_el_objetivo_en_usd(self):
        from motor import UtilidadObjetivo
        # 200,000 NIO / 36.6 = 5,464 USD => objetivo en USD equivalente
        m = self._medir(UtilidadObjetivo(monto=5_464.48, moneda="USD"))
        self.assertAlmostEqual(m["objetivo_nio"], 200_000, delta=50)
        self.assertTrue(m["dentro"])

    def test_el_margen_es_configurable(self):
        from motor import UtilidadObjetivo
        # 10% de desvio: fuera con margen 5, dentro con margen 15
        self.assertFalse(self._medir(UtilidadObjetivo(monto=222_222, moneda="NIO"))["dentro"])
        self.assertTrue(self._medir(
            UtilidadObjetivo(monto=222_222, moneda="NIO", tolerancia_pct=15))["dentro"])

    def test_nunca_bloquea(self):
        from motor import UtilidadObjetivo
        m = self._modelo(UtilidadObjetivo(monto=10_000_000, moneda="NIO"))
        self.assertTrue(m.ok, "el objetivo es una expectativa, no una regla contable")
        self.assertTrue(any(h.invariante == 11 for h in m.validacion.alertas))

    def test_rechaza_parametros_invalidos(self):
        from motor import UtilidadObjetivo
        with self.assertRaises(ValueError):
            UtilidadObjetivo(monto=-1)
        with self.assertRaises(ValueError):
            UtilidadObjetivo(monto=1000, tolerancia_pct=0)


if __name__ == "__main__":
    unittest.main()

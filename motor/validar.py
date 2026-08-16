"""Los 9 invariantes del motor (Decisiones_y_Reglas_Motor.md seccion C).

Cada check devuelve Hallazgos con nivel 'error' (bloqueante) o 'alerta'
(no bloqueante). Los invariantes 1 y 4-9 son comunes a ambos regimenes;
el #2 (ESF final exacto) es exclusivo de Tipo A y el #3 (bandas de
oscilacion) de Tipo B.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from motor.er import CalculoER
from motor.esf import CalculoESF
from motor.inputs import ESF_Saldos, InputsTipoA, InputsTipoB, PlanResuelto
from motor.mov import CalculoMov


TOLERANCIA = 1.0  # NIO


@dataclass(frozen=True)
class Hallazgo:
    invariante: int
    nivel: str  # 'error' | 'alerta'
    mensaje: str


@dataclass
class ResultadoValidacion:
    hallazgos: list[Hallazgo] = field(default_factory=list)

    @property
    def errores(self) -> list[Hallazgo]:
        return [h for h in self.hallazgos if h.nivel == "error"]

    @property
    def alertas(self) -> list[Hallazgo]:
        return [h for h in self.hallazgos if h.nivel == "alerta"]

    @property
    def ok(self) -> bool:
        return not self.errores

    def add(self, invariante: int, nivel: str, mensaje: str) -> None:
        self.hallazgos.append(Hallazgo(invariante, nivel, mensaje))


_CUENTAS_CORTE = [
    ("efectivo", "efectivo"),
    ("cuentas_por_cobrar", "cuentas_por_cobrar"),
    ("inventarios", "inventarios"),
    ("bienes_inmuebles", "bienes_inmuebles"),
    ("mobiliario_equipos", "mobiliario_equipos"),
    ("vehiculos", "vehiculos"),
    ("depreciacion_acumulada", "depreciacion_acumulada"),
    ("tarjetas_credito", "tarjetas_credito"),
    ("proveedores", "proveedores"),
    ("impuestos_por_pagar", "impuestos_por_pagar"),
    ("gastos_acumulados", "gastos_acumulados"),
    ("creditos_hipotecarios", "creditos_hipotecarios"),
    ("creditos_consumo", "creditos_consumo"),
    ("creditos_personales", "creditos_personales"),
    ("creditos_prendarios", "creditos_prendarios"),
    ("creditos_comerciales", "creditos_comerciales"),
    ("resultados_acumulados", "resultados_acumulados"),
]


def _invariantes_comunes(
    r: ResultadoValidacion,
    inputs: Union[InputsTipoA, InputsTipoB],
    planes: list[PlanResuelto],
    calculo_er: CalculoER,
    calculo_mov: CalculoMov,
    calculo_esf: CalculoESF,
) -> None:
    """Invariantes #1 y #4-#9, compartidos por Tipo A y Tipo B."""

    # ---- #1 Deuda: saldo credito mes de corte = saldo_reportado (via plan)
    for p in planes:
        tc = inputs.periodo.tasa_cambio if p.deuda.moneda == "USD" else 1.0
        esperado = p.deuda.saldo_reportado * tc
        real = p.saldo_final_corte_nio()
        if abs(real - esperado) > TOLERANCIA:
            r.add(1, "error", (
                f"Credito {p.deuda.numero}: saldo al corte {real:,.2f} != "
                f"saldo_reportado {esperado:,.2f} NIO (delta {real - esperado:,.2f})."
            ))
        if p.alerta:
            r.add(1, "alerta", p.alerta)

    # ---- #4 Balance cuadra cada mes
    for e in calculo_esf.meses:
        if abs(e.diferencia) > TOLERANCIA:
            r.add(4, "error", (
                f"Mes {e.mes}: Activos {e.total_activos:,.2f} != "
                f"Pasivo+Patrimonio {e.total_pasivo_patrimonio:,.2f} "
                f"(diff {e.diferencia:,.2f})."
            ))

    # ---- #5 Resultados del Ejercicio (ESF) = utilidad acumulada (ER)
    for e in calculo_esf.meses:
        util_acum = calculo_er.utilidad_acumulada_mes[e.mes]
        if abs(e.resultados_ejercicio - util_acum) > TOLERANCIA:
            r.add(5, "error", (
                f"Mes {e.mes}: Resultados del Ejercicio ESF {e.resultados_ejercicio:,.2f} "
                f"!= utilidad acumulada ER {util_acum:,.2f}."
            ))

    # ---- #6 Gastos Financieros ER = suma intereses planes vivos ese mes
    for mes in calculo_er.meses:
        gf = calculo_er.gastos_financieros_mes[mes]
        suma_int = sum(p.interes_del_mes_nio(mes) for p in planes)
        if abs(gf - suma_int) > TOLERANCIA:
            r.add(6, "error", (
                f"Mes {mes}: Gastos Financieros ER {gf:,.2f} != "
                f"suma intereses planes {suma_int:,.2f}."
            ))

    # ---- #7 Efectivo ESF = saldo final Mov
    for e in calculo_esf.meses:
        caja_mov = calculo_mov.saldo_final_mes(e.mes)
        if abs(e.efectivo - caja_mov) > TOLERANCIA:
            r.add(7, "error", (
                f"Mes {e.mes}: Efectivo ESF {e.efectivo:,.2f} != saldo Mov {caja_mov:,.2f}."
            ))

    # ---- #8 Depr. Acumulada ESF = inicial - suma depreciaciones ER
    si = inputs.saldos_iniciales
    acum = 0.0
    for e in calculo_esf.meses:
        acum += calculo_er.depreciacion_mes[e.mes]
        esperado = si.depreciacion_acumulada - acum
        if abs(e.depreciacion_acumulada - esperado) > TOLERANCIA:
            r.add(8, "error", (
                f"Mes {e.mes}: Depr. Acumulada ESF {e.depreciacion_acumulada:,.2f} != "
                f"esperado {esperado:,.2f}."
            ))

    # ---- #9 Capital NETO derivado: apertura (A0-P0-RA0) menos retiros
    # acumulados. Sigue sin ser un plug de cuadre mensual: cada movimiento
    # que lo cambia (retiro) es real y consume caja en Mov.
    cap0 = calculo_esf.capital_apertura
    for e in calculo_esf.meses:
        esperado = round(cap0 - e.retiros_acumulados, 2)
        if abs(e.capital - esperado) > TOLERANCIA:
            r.add(9, "error", (
                f"Mes {e.mes}: Capital {e.capital:,.2f} != apertura - retiros "
                f"({esperado:,.2f}). El Capital solo puede variar por retiros reales."
            ))


def medir_utilidad_objetivo(inputs, calculo_er: CalculoER) -> dict | None:
    """Que tan lejos quedo la utilidad promedio del objetivo del CPA.

    No corrige nada: mide. Devuelve None si no se fijo objetivo.
    'falta' es lo que habria que sumarle al promedio MENSUAL para llegar
    justo al objetivo (negativo = esta por encima)."""
    obj = getattr(inputs, "utilidad_objetivo", None)
    if not obj or not obj.activo:
        return None
    tc = inputs.periodo.tasa_cambio
    objetivo = obj.objetivo_nio(tc)
    promedio = calculo_er.promedio_utilidad()
    desvio = (promedio / objetivo - 1.0) * 100.0 if objetivo else 0.0
    return {
        "objetivo_nio": round(objetivo, 2),
        "objetivo_usd": round(objetivo / tc, 2),
        "promedio_nio": round(promedio, 2),
        "promedio_usd": round(promedio / tc, 2),
        "moneda": obj.moneda,
        "tolerancia_pct": obj.tolerancia_pct,
        "desvio_pct": round(desvio, 1),
        "falta_nio": round(objetivo - promedio, 2),
        "falta_usd": round((objetivo - promedio) / tc, 2),
        "dentro": abs(desvio) <= obj.tolerancia_pct,
    }


def _invariante_utilidad_objetivo(r: ResultadoValidacion, inputs, calculo_er: CalculoER) -> None:
    """#11 La utilidad promedio se parece a la que el CPA espera del negocio.

    Nunca bloquea: el objetivo es una expectativa sobre el cliente, no una
    regla contable. Pero queda registrado en la certificacion, con el desvio
    y cuanto falta por mes."""
    m = medir_utilidad_objetivo(inputs, calculo_er)
    if not m:
        return
    en_usd = m["moneda"] == "USD"
    prom = f"{m['promedio_usd']:,.0f} USD" if en_usd else f"{m['promedio_nio']:,.0f} NIO"
    obje = f"{m['objetivo_usd']:,.0f} USD" if en_usd else f"{m['objetivo_nio']:,.0f} NIO"
    falta = f"{abs(m['falta_usd']):,.0f} USD" if en_usd else f"{abs(m['falta_nio']):,.0f} NIO"
    if m["dentro"]:
        r.add(11, "alerta", (
            f"Utilidad promedio {prom}: dentro del objetivo {obje} "
            f"(desvio {m['desvio_pct']:+.1f}%, margen +-{m['tolerancia_pct']:.0f}%)."
        ))
        return
    direccion = "por DEBAJO" if m["falta_nio"] > 0 else "por ENCIMA"
    r.add(11, "alerta", (
        f"Utilidad promedio {prom}: {direccion} del objetivo {obje} "
        f"(desvio {m['desvio_pct']:+.1f}%, margen +-{m['tolerancia_pct']:.0f}%). "
        f"Faltan {falta} por mes para llegar."
    ))


def _invariantes_solver(r: ResultadoValidacion, inputs, solver) -> None:
    """#10 Caja: el efectivo nunca baja del piso configurado.

    Si el solver no alcanzo a cubrir el deficit con las palancas disponibles
    es ERROR bloqueante: el periodo, tal como esta parametrizado, no se puede
    financiar. El mensaje dice cuanto falta y por donde salir.
    Los ajustes que SI pudo hacer se informan como alerta, para que el CPA
    vea que el motor movio cuentas y cuanto."""
    if solver is None:
        return
    piso = float(getattr(inputs.minimos, "caja", 0.0))
    etiqueta = f"el piso de caja ({piso:,.0f} NIO)" if piso > 0 else "cero"
    for a in solver.ajustes:
        if a.faltante > TOLERANCIA:
            r.add(10, "error", (
                f"Mes {a.mes}: la caja no llega a {etiqueta}, faltan "
                f"{a.faltante:,.2f} NIO y las cuentas disponibles ya estan en su "
                f"limite. Salidas: subir el efectivo inicial, bajar el minimo de "
                f"inventario, ampliar las bandas o revisar los objetivos."
            ))
    if solver.aporte_total:
        r.add(10, "alerta", (
            f"El periodo necesito {solver.aporte_total:,.2f} NIO de aporte del "
            f"propietario para sostener la caja."
        ))
    for linea in solver.resumen():
        r.add(10, "alerta", f"Ajuste del solver - {linea}")


def validar_tipo_a(
    inputs: InputsTipoA,
    planes: list[PlanResuelto],
    calculo_er: CalculoER,
    calculo_mov: CalculoMov,
    calculo_esf: CalculoESF,
    solver=None,
) -> ResultadoValidacion:
    r = ResultadoValidacion()
    _invariantes_comunes(r, inputs, planes, calculo_er, calculo_mov, calculo_esf)
    _invariantes_solver(r, inputs, solver)
    _invariante_utilidad_objetivo(r, inputs, calculo_er)

    # ---- #2 Tipo A: ESF corte = saldos finales dados (por cuenta)
    corte = calculo_esf.corte()
    sf: ESF_Saldos = inputs.saldos_finales
    for attr_corte, attr_saldos in _CUENTAS_CORTE:
        real = getattr(corte, attr_corte)
        dado = getattr(sf, attr_saldos)
        if abs(real - dado) > TOLERANCIA:
            r.add(2, "error", (
                f"ESF corte cuenta '{attr_corte}': calculado {real:,.2f} != "
                f"dado {dado:,.2f} NIO (delta {real - dado:,.2f}). "
                f"Inputs del cliente inconsistentes con ER/deudas."
            ))
    return r


def validar_tipo_b(
    inputs: InputsTipoB,
    planes: list[PlanResuelto],
    calculo_er: CalculoER,
    calculo_mov: CalculoMov,
    calculo_esf: CalculoESF,
    solver=None,
) -> ResultadoValidacion:
    r = ResultadoValidacion()
    _invariantes_comunes(r, inputs, planes, calculo_er, calculo_mov, calculo_esf)
    _invariantes_solver(r, inputs, solver)
    _invariante_utilidad_objetivo(r, inputs, calculo_er)

    # ---- #3 Bandas de oscilacion
    obj_caja = inputs.objetivo("efectivo")
    inf, sup = obj_caja.banda()
    for e in calculo_esf.meses:
        if e.efectivo > sup + TOLERANCIA:
            # El motor controla los retiros: caja sobre banda = bug, bloqueante.
            r.add(3, "error", (
                f"Mes {e.mes}: caja {e.efectivo:,.2f} por ENCIMA de la banda "
                f"[{inf:,.2f}, {sup:,.2f}] — el retiro debio recortarla."
            ))
        elif e.efectivo < inf - TOLERANCIA:
            # Debajo de la banda no se maquilla (no se inyecta efectivo): alerta.
            r.add(3, "alerta", (
                f"Mes {e.mes}: caja {e.efectivo:,.2f} por debajo de la banda "
                f"[{inf:,.2f}, {sup:,.2f}] (el negocio aun no acumula suficiente efectivo)."
            ))

    obj_inv = inputs.objetivo("inventarios")
    if obj_inv:
        inf_i, sup_i = obj_inv.banda()
        for e in calculo_esf.meses:
            if e.inventarios > sup_i + TOLERANCIA or e.inventarios < inf_i - TOLERANCIA:
                # El inventario solo baja via costo de ventas (no se "descompra"):
                # apertura excedida => convergencia gradual, alerta informativa.
                r.add(3, "alerta", (
                    f"Mes {e.mes}: inventario {e.inventarios:,.2f} fuera de banda "
                    f"[{inf_i:,.2f}, {sup_i:,.2f}] (apertura/limite de compras)."
                ))

    # ---- Coherencia patrimonial de los retiros (no bloqueante)
    # Disponible en CAJA para retirar = RA inicial + utilidad + depreciacion
    # (la depreciacion es gasto no monetario: esa caja tambien la genera el
    # negocio). Solo alertar si los retiros comen capital de verdad.
    total_retiros = sum(m.retiro_patrimonio for m in calculo_mov.movs)
    depreciacion_periodo = sum(calculo_er.depreciacion_mes[m] for m in calculo_er.meses)
    disponible = (
        inputs.saldos_iniciales.resultados_acumulados
        + calculo_er.total_utilidad_neta()
        + depreciacion_periodo
    )
    if total_retiros > disponible + TOLERANCIA:
        r.add(9, "alerta", (
            f"Retiros de patrimonio ({total_retiros:,.2f}) exceden lo generado en el "
            f"periodo (utilidad + depreciacion + RA inicial = {disponible:,.2f}): "
            f"estarian comiendo capital. Revisar el objetivo de caja."
        ))
    return r

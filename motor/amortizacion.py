"""Motor de deuda: genera las tablas de amortizacion mensuales por credito.

La IA externa solo extrae parametros del PDF de TransUnion -> JSON; este
modulo toma ese JSON y produce los movimientos contables mensuales (abono
a capital + interes) que alimentan el ER (Gastos Financieros), el Mov
(pagos) y el ESF (saldo del pasivo).

Reglas (Decisiones_y_Reglas_Motor.md seccion B):
  - Ancla = saldo_reportado, NO valor_inicial.
  - Saldos intermedios son estimacion; solo el mes de corte pega exacto.
  - Cierre exacto via abono_extraordinario en mes_final (pago real → caja).
  - Alerta no bloqueante si |abono_extraordinario| > 2 x cuota (moneda original).
  - Estrategia por tipo: amortizable | bullet | revolving.
  - Filtro por ventana: solo entra si fecha_otorgamiento <= ultimo dia del mes_final.
  - USD se convierte a NIO con T/C fijo del periodo (decision #10).
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Mapping, Sequence

from motor.inputs import (
    CUENTAS_PASIVO_VALIDAS,
    CuotaPlan,
    DeudaInput,
    Estrategia,
    PeriodoSpec,
    PlanResuelto,
)


# ---------------------------------------------------------------- Loader JSON

def deudas_from_json(raw_list: Sequence[Mapping]) -> list[DeudaInput]:
    out: list[DeudaInput] = []
    for raw in raw_list:
        venc = raw.get("fecha_vencimiento")
        out.append(
            DeudaInput(
                numero=str(raw["numero"]),
                entidad=str(raw["entidad"]),
                tipo_credito=str(raw["tipo_credito"]),
                estrategia=raw["estrategia"],
                moneda=raw["moneda"],
                valor_inicial=float(raw["valor_inicial"]),
                saldo_reportado=float(raw["saldo_reportado"]),
                cuota=float(raw["cuota"]),
                fecha_otorgamiento=date.fromisoformat(raw["fecha_otorgamiento"]),
                fecha_actualizado=date.fromisoformat(raw["fecha_actualizado"]),
                fecha_vencimiento=date.fromisoformat(venc) if venc else None,
                tasa_mensual=raw.get("tasa_mensual"),
                saldo_apertura=raw.get("saldo_apertura"),
                incluir_en_er=bool(raw.get("incluir_en_er", True)),
                saldos_mensuales=raw.get("saldos_mensuales"),
                notas=raw.get("notas", ""),
            )
        )
    return out


# ---------------------------------------------------------------- Helpers mes

def _meses_del_periodo(periodo: PeriodoSpec) -> list[str]:
    y, m = int(periodo.mes_inicial[:4]), int(periodo.mes_inicial[5:7])
    end_y, end_m = int(periodo.mes_final[:4]), int(periodo.mes_final[5:7])
    out: list[str] = []
    while (y, m) <= (end_y, end_m):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _meses_entre(d1: date, d2: date) -> int:
    return (d2.year - d1.year) * 12 + (d2.month - d1.month)


def _ultimo_dia_mes(yyyy_mm: str) -> date:
    y, m = int(yyyy_mm[:4]), int(yyyy_mm[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])


def _siguiente_mes(yyyy_mm: str) -> str:
    y, m = int(yyyy_mm[:4]), int(yyyy_mm[5:7])
    m += 1
    if m > 12:
        m, y = 1, y + 1
    return f"{y:04d}-{m:02d}"


# ---------------------------------------------------------------- Filtro ventana

def filtrar_por_ventana(deudas: Sequence[DeudaInput], periodo: PeriodoSpec) -> list[DeudaInput]:
    fin = _ultimo_dia_mes(periodo.mes_final)
    return [d for d in deudas if d.fecha_otorgamiento <= fin]


# ---------------------------------------------------------------- Mapeo cuenta

def mapear_cuenta_esf(deuda: DeudaInput) -> str:
    if deuda.estrategia == "revolving":
        return "tarjetas_credito"
    t = deuda.tipo_credito.upper()
    # El DESTINO especifico manda sobre la CATEGORIA generica. SIBOIF concatena
    # 'Categoria - Destino' (p.ej. 'Consumo - Personales', 'Consumo - Tarjetas
    # de Credito'): si se chequeara "CONSUMO" primero, todo caeria en consumo.
    # Por eso las palabras de destino (hipotec/vehiculo/tarjeta/personal) van
    # ANTES que las de categoria (comercial/consumo).
    if "HIPOTEC" in t:
        return "creditos_hipotecarios"
    if "VEHICUL" in t or "PRENDARIO" in t or "PRENDA" in t:
        return "creditos_prendarios"
    if "TARJETA" in t:
        return "tarjetas_credito"
    if "PERSONAL" in t:
        return "creditos_personales"
    if "COMERCIAL" in t:
        return "creditos_comerciales"
    if "CONSUMO" in t:
        return "creditos_consumo"
    return "creditos_consumo"


# ---------------------------------------------------------------- Tasa inferida

def _cuota_francesa(saldo: float, i: float, n: int) -> float:
    if i <= 0:
        return saldo / n
    factor = (1 + i) ** n
    return saldo * i * factor / (factor - 1)


def inferir_tasa_mensual(valor_inicial: float, cuota: float, plazo_meses: int) -> float:
    """Bisección sobre amortización francesa: encontrar i tal que cuota(saldo,i,n)=cuota.

    Si la cuota no alcanza para cubrir el capital (cuota*n < valor_inicial),
    la tasa implícita es <= 0 y devolvemos 0.0; el abono extraordinario
    absorberá la diferencia al cierre.
    """
    if plazo_meses <= 0 or valor_inicial <= 0 or cuota <= 0:
        return 0.0
    if cuota * plazo_meses < valor_inicial:
        return 0.0
    lo, hi = 0.0, 0.10
    for _ in range(80):
        mid = (lo + hi) / 2
        c = _cuota_francesa(valor_inicial, mid, plazo_meses)
        if c < cuota:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2


# ---------------------------------------------------------------- Tasa y apertura

def _plazo_meses(deuda: DeudaInput) -> int:
    if deuda.fecha_vencimiento:
        return max(1, _meses_entre(deuda.fecha_otorgamiento, deuda.fecha_vencimiento))
    return max(1, int(round(deuda.valor_inicial / deuda.cuota))) if deuda.cuota else 1


def _tasa_amortizable(deuda: DeudaInput) -> float:
    if deuda.tasa_mensual is not None:
        return deuda.tasa_mensual
    return inferir_tasa_mensual(deuda.valor_inicial, deuda.cuota, _plazo_meses(deuda))


def _tasa_bullet(deuda: DeudaInput) -> float:
    if deuda.tasa_mensual is not None:
        return deuda.tasa_mensual
    return deuda.cuota / deuda.valor_inicial if deuda.valor_inicial > 0 else 0.0


def _apertura_derivada_amortizable(deuda: DeudaInput, periodo: PeriodoSpec, tasa: float) -> float:
    """Legacy (sin saldo_apertura): amortiza desde otorgamiento hasta el mes
    ANTERIOR al primer mes del periodo, para estimar el saldo de apertura."""
    primer_mes = periodo.mes_inicial
    mes_sim = _siguiente_mes(f"{deuda.fecha_otorgamiento.year:04d}-{deuda.fecha_otorgamiento.month:02d}")
    saldo = deuda.valor_inicial
    while mes_sim < primer_mes and saldo > 0.01:
        interes = saldo * tasa
        abono = min(deuda.cuota - interes, saldo)
        saldo = max(0.0, saldo - max(0.0, abono))
        mes_sim = _siguiente_mes(mes_sim)
    return saldo


# ---------------------------------------------------------------- Materializar

def _materializar(
    deuda: DeudaInput,
    periodo: PeriodoSpec,
    cuenta_esf: str,
    tasa: float,
    saldo_apertura: float,
    filas: list[tuple],  # (no, mes, s_ini, cuota, interes, abono_normal)
) -> PlanResuelto:
    """Ancla el ultimo mes a saldo_reportado via abono extraordinario y
    convierte a NIO. filas ya viene dentro del periodo, en moneda original."""
    tc = periodo.tasa_cambio if deuda.moneda == "USD" else 1.0

    if not filas or filas[-1][1] != periodo.mes_final:
        s_prev = filas[-1][2] - filas[-1][5] if filas else saldo_apertura
        filas = list(filas) + [(len(filas) + 1, periodo.mes_final, s_prev, 0.0, 0.0, 0.0)]

    ult_idx = len(filas) - 1
    s_ini_ult, abono_normal_ult = filas[ult_idx][2], filas[ult_idx][5]
    saldo_post_normal = max(0.0, s_ini_ult - abono_normal_ult)
    abono_extra = saldo_post_normal - deuda.saldo_reportado

    alerta = None
    if abs(abono_extra) > 2.0 * deuda.cuota and deuda.cuota > 0:
        alerta = (
            f"Credito {deuda.numero} ({deuda.entidad}): abono extraordinario de cierre "
            f"= {abono_extra:,.2f} {deuda.moneda} (>2x cuota {deuda.cuota:,.2f}). "
            f"Revisar parametros extraidos (tasa/cuota/vencimiento)."
        )

    cuotas: list[CuotaPlan] = []
    for i, (no, mes, s_ini, c, intr, ab) in enumerate(filas):
        es_ultimo = i == ult_idx
        ab_extra = abono_extra if es_ultimo else 0.0
        s_fin = deuda.saldo_reportado if es_ultimo else max(0.0, s_ini - ab - ab_extra)
        cuotas.append(CuotaPlan(
            no_cuota=no,
            mes=mes,
            saldo_inicial_nio=s_ini * tc,
            cuota_nio=c * tc,
            interes_nio=intr * tc,
            abono_capital_nio=ab * tc,
            abono_extraordinario_nio=ab_extra * tc,
            saldo_final_nio=s_fin * tc,
        ))

    return PlanResuelto(
        deuda=deuda,
        cuenta_esf=cuenta_esf,
        cuotas=cuotas,
        tasa_mensual_inferida=tasa,
        saldo_apertura_nio=saldo_apertura * tc,
        alerta=alerta,
    )


# ---------------------------------------------------------------- Plan amortizable

def _resolver_amortizable(deuda: DeudaInput, periodo: PeriodoSpec) -> PlanResuelto:
    meses_periodo = _meses_del_periodo(periodo)

    # Si el CPA da la trayectoria a mano, se respeta.
    if deuda.saldos_mensuales:
        saldos = {m: float(v) for m, v in deuda.saldos_mensuales.items()}
        return _resolver_desde_saldos_mensuales(deuda, periodo, mapear_cuenta_esf(deuda), saldos)

    # Sin valor_inicial no se puede inferir la tasa para el plan frances
    # (tipico de reportes agregados SIBOIF, que no traen monto original). Se
    # GENERA una amortizacion lineal descendente hasta el saldo reportado: con
    # cuota, la baja mensual es la cuota de capital; sin cuota, se estima una
    # amortizacion del periodo. Respeta la cuenta ESF por tipo.
    if deuda.valor_inicial <= 0 and deuda.saldo_apertura is None:
        from motor.deuda_generada import trayectoria_amortizable

        saldos = trayectoria_amortizable(
            deuda.saldo_reportado, meses_periodo, cuota=deuda.cuota,
        )
        return _resolver_desde_saldos_mensuales(
            deuda, periodo, mapear_cuenta_esf(deuda), saldos
        )

    tasa = _tasa_amortizable(deuda)
    if deuda.saldo_apertura is not None:
        saldo_apertura = deuda.saldo_apertura
    else:
        saldo_apertura = _apertura_derivada_amortizable(deuda, periodo, tasa)

    saldo = saldo_apertura
    filas: list[tuple] = []
    for no, mes in enumerate(meses_periodo, start=1):
        interes = saldo * tasa
        abono = min(deuda.cuota - interes, saldo)
        if abono < 0:
            abono = 0.0
        filas.append((no, mes, saldo, deuda.cuota, interes, abono))
        saldo = max(0.0, saldo - abono)

    return _materializar(deuda, periodo, mapear_cuenta_esf(deuda), tasa, saldo_apertura, filas)


# ---------------------------------------------------------------- Plan bullet

def _resolver_bullet(deuda: DeudaInput, periodo: PeriodoSpec) -> PlanResuelto:
    """Interes-only mes a mes; capital al vencimiento (si cae en el periodo)."""
    tasa = _tasa_bullet(deuda)
    meses_periodo = _meses_del_periodo(periodo)
    saldo_apertura = deuda.saldo_apertura if deuda.saldo_apertura is not None else deuda.valor_inicial

    saldo = saldo_apertura
    filas: list[tuple] = []
    for no, mes in enumerate(meses_periodo, start=1):
        interes = saldo * tasa
        vence_este_mes = (
            deuda.fecha_vencimiento is not None
            and f"{deuda.fecha_vencimiento.year:04d}-{deuda.fecha_vencimiento.month:02d}" == mes
        )
        abono = saldo if vence_este_mes else 0.0
        filas.append((no, mes, saldo, interes + abono, interes, abono))
        saldo = max(0.0, saldo - abono)

    return _materializar(deuda, periodo, mapear_cuenta_esf(deuda), tasa, saldo_apertura, filas)


# ---------------------------------------------------------------- Saldos dados

def _seed_deuda(deuda: DeudaInput, periodo: PeriodoSpec) -> str:
    """Seed determinista por deuda (banda reproducible mes a mes)."""
    return f"{deuda.numero}|{deuda.entidad}|{deuda.saldo_reportado}|{periodo.mes_final}"


def _resolver_desde_saldos_mensuales(
    deuda: DeudaInput, periodo: PeriodoSpec, cuenta_esf: str, saldos: Mapping[str, float]
) -> PlanResuelto:
    """Materializa un plan a partir de una trayectoria mensual de saldos (dada
    a mano o GENERADA). El flujo de caja lo deriva Mov del delta de saldo. El
    ultimo mes queda anclado al saldo dado para ese mes (= saldo_reportado en
    las trayectorias generadas). Respeta la cuenta ESF del tipo de credito."""
    tc = periodo.tasa_cambio if deuda.moneda == "USD" else 1.0
    meses_periodo = _meses_del_periodo(periodo)
    apertura = deuda.saldo_apertura if deuda.saldo_apertura is not None else (
        float(saldos.get(meses_periodo[0], deuda.saldo_reportado)) if meses_periodo else deuda.saldo_reportado
    )
    cuotas: list[CuotaPlan] = []
    prev = apertura
    for no, mes in enumerate(meses_periodo, start=1):
        s_fin = float(saldos.get(mes, prev))
        delta = s_fin - prev  # >0 consumo/nuevo credito (entra caja), <0 pago (sale)
        abono = max(0.0, -delta)
        cuotas.append(CuotaPlan(
            no_cuota=no,
            mes=mes,
            saldo_inicial_nio=prev * tc,
            cuota_nio=deuda.cuota * tc,
            interes_nio=deuda.cuota * tc,
            abono_capital_nio=abono * tc,
            abono_extraordinario_nio=0.0,
            saldo_final_nio=s_fin * tc,
        ))
        prev = s_fin
    return PlanResuelto(
        deuda=deuda,
        cuenta_esf=cuenta_esf,
        cuotas=cuotas,
        tasa_mensual_inferida=0.0,
        saldo_apertura_nio=apertura * tc,
        alerta=None,
    )


# ---------------------------------------------------------------- Plan revolving

def _resolver_revolving(deuda: DeudaInput, periodo: PeriodoSpec) -> PlanResuelto:
    """Tarjeta/linea revolving. El saldo OSCILA en banda +-20% alrededor del
    saldo reportado (como caja/inventario en Tipo B), terminando en el saldo
    reportado (ancla dura). Si el CPA da saldos_mensuales, se respetan."""
    from motor.deuda_generada import trayectoria_revolving

    meses_periodo = _meses_del_periodo(periodo)
    if deuda.saldos_mensuales:
        saldos = {m: float(v) for m, v in deuda.saldos_mensuales.items()}
    else:
        saldos = trayectoria_revolving(
            deuda.saldo_reportado, meses_periodo, banda_pct=20.0,
            seed=_seed_deuda(deuda, periodo),
        )
    return _resolver_desde_saldos_mensuales(deuda, periodo, "tarjetas_credito", saldos)


# ---------------------------------------------------------------- Orquestador

_RESOLVERS: dict[Estrategia, callable] = {
    "amortizable": _resolver_amortizable,
    "bullet": _resolver_bullet,
    "revolving": _resolver_revolving,
}


def resolver_planes(deudas: Sequence[DeudaInput], periodo: PeriodoSpec) -> list[PlanResuelto]:
    """Filtra por ventana y resuelve TODOS los planes vigentes (activos +
    documentales). Usar planes_activos() para los que impactan ER/Mov/ESF."""
    activos = filtrar_por_ventana(deudas, periodo)
    out: list[PlanResuelto] = []
    for d in activos:
        resolver = _RESOLVERS.get(d.estrategia)
        if resolver is None:
            raise ValueError(f"Estrategia desconocida {d.estrategia!r} en credito {d.numero}")
        plan = resolver(d, periodo)
        if plan.cuenta_esf not in CUENTAS_PASIVO_VALIDAS:
            raise ValueError(
                f"Credito {d.numero}: cuenta_esf {plan.cuenta_esf!r} no es pasivo valido"
            )
        out.append(plan)
    return out


def planes_activos(planes: Sequence[PlanResuelto]) -> list[PlanResuelto]:
    """Planes que impactan ER/Mov/ESF (incluir_en_er=True)."""
    return [p for p in planes if p.deuda.incluir_en_er]


def planes_documentales(planes: Sequence[PlanResuelto]) -> list[PlanResuelto]:
    """Planes solo de soporte/anexo (incluir_en_er=False)."""
    return [p for p in planes if not p.deuda.incluir_en_er]

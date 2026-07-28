"""Motor de caja: deriva el saldo de Efectivo mes a mes desde cobros y pagos.

Regla de oro (Decisiones_y_Reglas_Motor.md C.7): la caja se DERIVA, no se inventa.
  caja[t] = caja[t-1] + cobros[t] - pagos[t]

V1 (decision #4 default = todo contado, CxC = 0):
  cobros[t] = ingresos[t]
  pagos[t] = cogs[t] + gastos_operativos_no_depr[t] + intereses_planes[t] + abonos_planes[t]

Depreciacion NUNCA sale como pago (invariante #8).
Sin eventos discretos, sin capex, sin retiros de patrimonio en V1.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from motor.er import CalculoER
from motor.inputs import ESF_Saldos, PeriodoSpec, PlanResuelto


@dataclass(frozen=True)
class MovMes:
    mes: str
    saldo_inicial: float
    # Cobros
    ventas_contado: float
    financiamiento_credito: float  # consumos netos de credito que ENTRAN a caja (delta>0)
    # Pagos (positivos; salen de caja)
    pago_costo_ventas: float
    pago_gastos_operativos: float  # excluye depreciacion y financieros (estos van separados)
    pago_gastos_financieros: float  # suma intereses planes activos
    pago_abonos_creditos: float  # amortizacion neta que SALE de caja (delta<0)
    # Totales y cierre
    total_cobros: float
    total_pagos: float
    saldo_final: float
    # Solo Tipo B (en Tipo A quedan en 0)
    pago_compras_inventario: float = 0.0  # compras totales pagadas (incluyen cogs)
    retiro_patrimonio: float = 0.0  # excedente de caja retirado (contra Result. Acumulados)


@dataclass(frozen=True)
class CalculoMov:
    movs: list[MovMes]
    df: pd.DataFrame  # tabla "Mov" para DOCX/diagnostico

    def saldo_final_mes(self, mes: str) -> float:
        for m in self.movs:
            if m.mes == mes:
                return m.saldo_final
        raise KeyError(f"Mes {mes} no esta en Mov")

    def saldos_finales_por_mes(self) -> dict[str, float]:
        return {m.mes: m.saldo_final for m in self.movs}


_GASTOS_OPER_NO_DEPR_LABELS = [
    "Sueldos y Salarios",
    "Servicios Publicos",
    "Alcaldia y DGI",
    "Combustible",
    "Publicidad",
    "Mantenimientos",
    "Renta",
    "Seguros",
    "Otros Gastos",
]


def _redondear(x: float) -> float:
    # Cordobas enteros (ver motor/er._redondear): cuadre exacto para el banco.
    return round(float(x), 0)


def _delta_principal_por_mes(planes: list[PlanResuelto], meses: list[str]) -> dict[str, float]:
    """Delta de saldo de principal por mes, sumando planes activos.

    delta>0 => el pasivo subio (consumo) => financia caja (entra).
    delta<0 => el pasivo bajo (pago) => consume caja (sale).
    El primer mes se mide contra saldo_apertura_nio del plan.
    """
    out: dict[str, float] = {m: 0.0 for m in meses}
    for p in planes:
        prev = p.saldo_apertura_nio
        saldo_por_mes = {c.mes: c.saldo_final_nio for c in p.cuotas}
        for m in meses:
            s = saldo_por_mes.get(m, prev)
            out[m] = out[m] + (s - prev)
            prev = s
    return {m: _redondear(v) for m, v in out.items()}


def construir_mov(
    calculo_er: CalculoER,
    planes: list[PlanResuelto],
    periodo: PeriodoSpec,
    saldos_iniciales: ESF_Saldos,
    inventario_mensual: dict[str, float] | None = None,
    proveedores_mensual: dict[str, float] | None = None,
) -> CalculoMov:
    """inventario_mensual (opcional): trayectoria del inventario. Si se da, las
    COMPRAS del mes = costo de ventas + variacion del inventario (subir stock
    consume caja, bajarlo la libera), de modo que el ESF cuadre con esa
    trayectoria. Sin el, el inventario es constante (compras = costo).

    proveedores_mensual (opcional): trayectoria del pasivo con proveedores. El
    PAGO en efectivo = compras - variacion de proveedores: si el pasivo sube
    (compre a credito) sale menos caja; si baja (pague deuda vieja) sale mas.
    Debe ser la MISMA trayectoria que reciba construir_esf o el balance
    descuadra."""
    # planes ya vienen filtrados a activos por el orquestador
    delta_principal = _delta_principal_por_mes(planes, calculo_er.meses)

    movs: list[MovMes] = []
    saldo = _redondear(saldos_iniciales.efectivo)
    inv_prev = _redondear(saldos_iniciales.inventarios)
    prov_prev = _redondear(saldos_iniciales.proveedores)

    for mes in calculo_er.meses:
        cobros_ventas = _redondear(calculo_er.ingresos_mes[mes])
        pago_cogs = _redondear(calculo_er.costo_ventas_mes[mes])
        if inventario_mensual:
            inv_mes = _redondear(inventario_mensual.get(mes, inv_prev))
            # Compras = costo de ventas + lo que crecio el stock.
            pago_cogs = _redondear(pago_cogs + (inv_mes - inv_prev))
            inv_prev = inv_mes
        if proveedores_mensual:
            prov_mes = _redondear(proveedores_mensual.get(mes, prov_prev))
            # Lo comprado y no pagado queda como pasivo: sale menos efectivo.
            pago_cogs = _redondear(pago_cogs - (prov_mes - prov_prev))
            prov_prev = prov_mes
        pago_gastos_oper = _redondear(
            sum(calculo_er.gastos_por_label_mes[lbl][mes] for lbl in _GASTOS_OPER_NO_DEPR_LABELS)
        )
        pago_financieros = _redondear(calculo_er.gastos_financieros_mes[mes])
        # Efecto del principal de creditos en caja = delta de saldo.
        delta = delta_principal[mes]
        financiamiento = _redondear(max(0.0, delta))   # sube pasivo -> entra caja
        pago_abonos = _redondear(max(0.0, -delta))      # baja pasivo -> sale caja

        total_cobros = _redondear(cobros_ventas + financiamiento)
        total_pagos = _redondear(pago_cogs + pago_gastos_oper + pago_financieros + pago_abonos)
        nuevo_saldo = _redondear(saldo + total_cobros - total_pagos)

        movs.append(MovMes(
            mes=mes,
            saldo_inicial=saldo,
            ventas_contado=cobros_ventas,
            financiamiento_credito=financiamiento,
            pago_costo_ventas=pago_cogs,
            pago_gastos_operativos=pago_gastos_oper,
            pago_gastos_financieros=pago_financieros,
            pago_abonos_creditos=pago_abonos,
            total_cobros=total_cobros,
            total_pagos=total_pagos,
            saldo_final=nuevo_saldo,
        ))
        saldo = nuevo_saldo

    columnas = ["Concepto", *[m.mes for m in movs]]

    def _fila(label: str, getter) -> list:
        return [label, *[_redondear(getter(m)) for m in movs]]

    rows = [
        _fila("Saldo inicial de caja", lambda x: x.saldo_inicial),
        _fila("Ventas de contado (cobros)", lambda x: x.ventas_contado),
        _fila("Financiamiento de creditos", lambda x: x.financiamiento_credito),
        _fila("Total entradas de efectivo", lambda x: x.total_cobros),
        _fila("Pago costo de ventas", lambda x: -x.pago_costo_ventas),
        _fila("Pago gastos operativos", lambda x: -x.pago_gastos_operativos),
        _fila("Pago intereses creditos", lambda x: -x.pago_gastos_financieros),
        _fila("Abonos a creditos (principal)", lambda x: -x.pago_abonos_creditos),
        _fila("Total salidas de efectivo", lambda x: -x.total_pagos),
        _fila("Saldo final de caja", lambda x: x.saldo_final),
    ]
    df = pd.DataFrame(rows, columns=columnas)

    return CalculoMov(movs=movs, df=df)

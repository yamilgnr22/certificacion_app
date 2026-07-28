"""Construye el ER mensual del periodo.

Inputs: lineas ER por mes (input del cliente, NIO directo) + planes resueltos
(de motor/amortizacion) + spec del periodo.

Cambio de nomenclatura vs Excel viejo:
  - La linea "(=) Ingresos Brutos" del ER se renombra a "(=) UTILIDAD BRUTA"
    porque es Ingresos - Costo de ventas, que es utilidad bruta tecnicamente.
  - "Ingresos Brutos" en la Certificacion/DOCX sigue siendo el total de Ingresos
    del periodo (no la utilidad bruta) — eso vive en motor/certificacion.py.

Gastos Financieros del mes = suma de intereses de todos los planes vivos
ese mes (no es input del cliente).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from motor.amortizacion import _meses_del_periodo
from motor.inputs import ER_LineaMes, PeriodoSpec, PlanResuelto


# Orden y etiquetas exactas (debe respetarse para el DOCX)
LABEL_INGRESOS = "Ingresos"
LABEL_COSTO_VENTAS = "(-) Costo de ventas"
LABEL_UTILIDAD_BRUTA = "(=) Utilidad Bruta"
LABEL_GASTOS_OPER_HEADER = "(-) Gastos operativos"
LABEL_TOTAL_GASTOS = "Total gastos operativos"
LABEL_UTILIDAD_NETA = "Ingresos/Utilidad Neta"

LABEL_SUELDOS = "Sueldos y Salarios"
LABEL_SERVICIOS = "Servicios Publicos"
LABEL_ALCALDIA = "Alcaldia y DGI"
LABEL_COMBUSTIBLE = "Combustible"
LABEL_PUBLICIDAD = "Publicidad"
LABEL_FINANCIEROS = "Gastos Financieros"
LABEL_MANTENIMIENTOS = "Mantenimientos"
LABEL_RENTA = "Renta"
LABEL_DEPRECIACION = "Gasto por Depreciacion"
LABEL_SEGUROS = "Seguros"
LABEL_OTROS = "Otros Gastos"

ORDEN_GASTOS = [
    LABEL_SUELDOS,
    LABEL_SERVICIOS,
    LABEL_ALCALDIA,
    LABEL_COMBUSTIBLE,
    LABEL_PUBLICIDAD,
    LABEL_FINANCIEROS,
    LABEL_MANTENIMIENTOS,
    LABEL_RENTA,
    LABEL_DEPRECIACION,
    LABEL_SEGUROS,
    LABEL_OTROS,
]


@dataclass(frozen=True)
class CalculoER:
    """Output del modulo: DataFrame para DOCX + cifras estructuradas para el resto del motor."""

    df: pd.DataFrame
    meses: list[str]  # 'YYYY-MM' en orden
    ingresos_mes: dict[str, float]
    costo_ventas_mes: dict[str, float]
    utilidad_bruta_mes: dict[str, float]
    gastos_por_label_mes: dict[str, dict[str, float]]  # label -> mes -> monto
    total_gastos_mes: dict[str, float]
    depreciacion_mes: dict[str, float]
    gastos_financieros_mes: dict[str, float]
    utilidad_neta_mes: dict[str, float]
    utilidad_acumulada_mes: dict[str, float]

    def total_ingresos(self) -> float:
        return sum(self.ingresos_mes.values())

    def total_utilidad_neta(self) -> float:
        return sum(self.utilidad_neta_mes.values())

    def promedio_ingresos(self) -> float:
        n = len(self.meses)
        return self.total_ingresos() / n if n else 0.0

    def promedio_utilidad(self) -> float:
        n = len(self.meses)
        return self.total_utilidad_neta() / n if n else 0.0

    def gasto_cash_del_mes(self, mes: str) -> float:
        """Gastos que SI consumen caja: todos menos depreciacion (no monetaria)."""
        return self.total_gastos_mes.get(mes, 0.0) - self.depreciacion_mes.get(mes, 0.0)


def _meses_a_timestamps(meses_str: Iterable[str]) -> list[pd.Timestamp]:
    out: list[pd.Timestamp] = []
    for m in meses_str:
        y, mo = int(m[:4]), int(m[5:7])
        day = calendar.monthrange(y, mo)[1]
        out.append(pd.Timestamp(year=y, month=mo, day=day))
    return out


def _indexar_lineas(er_lineas: list[ER_LineaMes]) -> dict[str, ER_LineaMes]:
    out: dict[str, ER_LineaMes] = {}
    for ln in er_lineas:
        if ln.mes in out:
            raise ValueError(f"ER_LineaMes duplicada para mes {ln.mes}")
        out[ln.mes] = ln
    return out


def _redondear(x: float) -> float:
    # Certificacion en cordobas ENTEROS: los bancos exigen cuadre exacto sin
    # tolerancia, asi que se redondea a entero en cada calculo (como el Excel
    # del CPA). El balance cuadra por construccion (el motor no usa plug).
    return round(float(x), 0)


def construir_er(
    er_lineas: list[ER_LineaMes],
    planes: list[PlanResuelto],
    periodo: PeriodoSpec,
) -> CalculoER:
    meses = _meses_del_periodo(periodo)
    por_mes = _indexar_lineas(er_lineas)
    faltantes = [m for m in meses if m not in por_mes]
    if faltantes:
        raise ValueError(f"ER_LineaMes faltante para meses {faltantes}")

    ingresos_mes: dict[str, float] = {}
    costo_ventas_mes: dict[str, float] = {}
    utilidad_bruta_mes: dict[str, float] = {}
    gastos_por_label_mes: dict[str, dict[str, float]] = {lbl: {} for lbl in ORDEN_GASTOS}
    total_gastos_mes: dict[str, float] = {}
    depreciacion_mes: dict[str, float] = {}
    gastos_financieros_mes: dict[str, float] = {}
    utilidad_neta_mes: dict[str, float] = {}
    utilidad_acumulada_mes: dict[str, float] = {}

    acumulada = 0.0
    for mes in meses:
        ln = por_mes[mes]
        ingresos = _redondear(ln.ingresos)
        cogs = _redondear(ln.costo_ventas)
        bruta = _redondear(ingresos - cogs)

        gf_mes = _redondear(sum(p.interes_del_mes_nio(mes) for p in planes))

        gastos: dict[str, float] = {
            LABEL_SUELDOS: _redondear(ln.sueldos_salarios),
            LABEL_SERVICIOS: _redondear(ln.servicios_publicos),
            LABEL_ALCALDIA: _redondear(ln.alcaldia_dgi),
            LABEL_COMBUSTIBLE: _redondear(ln.combustible),
            LABEL_PUBLICIDAD: _redondear(ln.publicidad),
            LABEL_FINANCIEROS: gf_mes,
            LABEL_MANTENIMIENTOS: _redondear(ln.mantenimientos),
            LABEL_RENTA: _redondear(ln.renta),
            LABEL_DEPRECIACION: _redondear(ln.gasto_depreciacion),
            LABEL_SEGUROS: _redondear(ln.seguros),
            LABEL_OTROS: _redondear(ln.otros_gastos),
        }
        total = _redondear(sum(gastos.values()))
        neta = _redondear(bruta - total)
        acumulada = _redondear(acumulada + neta)

        ingresos_mes[mes] = ingresos
        costo_ventas_mes[mes] = cogs
        utilidad_bruta_mes[mes] = bruta
        for lbl, v in gastos.items():
            gastos_por_label_mes[lbl][mes] = v
        total_gastos_mes[mes] = total
        depreciacion_mes[mes] = gastos[LABEL_DEPRECIACION]
        gastos_financieros_mes[mes] = gf_mes
        utilidad_neta_mes[mes] = neta
        utilidad_acumulada_mes[mes] = acumulada

    # ---- DataFrame para DOCX (shape: ["Descripcion", "Base", *meses_ts, "Acumulado", "Promedio"])
    meses_ts = _meses_a_timestamps(meses)
    columns = ["Descripcion", "Base", *meses_ts, "Acumulado del periodo", "Promedio Mensual"]
    n = len(meses)

    def _fila(label: str, valores: dict[str, float], base: float | str = "") -> list:
        vals = [_redondear(valores.get(m, 0.0)) for m in meses]
        acc = _redondear(sum(vals))
        prom = _redondear(acc / n) if n else 0.0
        return [label, base, *vals, acc, prom]

    def _fila_vacia() -> list:
        return ["", "", *["" for _ in meses], "", ""]

    rows: list[list] = []
    # Filas de relleno: generar_tabla_er (DOCX) descarta las primeras 5 filas
    # del df (en el Excel legado eran factores/T.C./ventas de contado). Sin
    # este relleno, el drop se comeria Ingresos y Costo de ventas.
    for _ in range(5):
        rows.append(_fila_vacia())
    rows.append(_fila(LABEL_INGRESOS, ingresos_mes))
    rows.append(_fila_vacia())
    rows.append(_fila(LABEL_COSTO_VENTAS, costo_ventas_mes))
    rows.append(_fila_vacia())
    rows.append(_fila(LABEL_UTILIDAD_BRUTA, utilidad_bruta_mes))
    rows.append(_fila_vacia())
    rows.append([LABEL_GASTOS_OPER_HEADER, "", *["" for _ in meses], "", ""])
    for label in ORDEN_GASTOS:
        rows.append(_fila(label, gastos_por_label_mes[label]))
    rows.append(_fila(LABEL_TOTAL_GASTOS, total_gastos_mes))
    rows.append(_fila_vacia())
    rows.append(_fila(LABEL_UTILIDAD_NETA, utilidad_neta_mes))
    df = pd.DataFrame(rows, columns=columns)

    return CalculoER(
        df=df,
        meses=meses,
        ingresos_mes=ingresos_mes,
        costo_ventas_mes=costo_ventas_mes,
        utilidad_bruta_mes=utilidad_bruta_mes,
        gastos_por_label_mes=gastos_por_label_mes,
        total_gastos_mes=total_gastos_mes,
        depreciacion_mes=depreciacion_mes,
        gastos_financieros_mes=gastos_financieros_mes,
        utilidad_neta_mes=utilidad_neta_mes,
        utilidad_acumulada_mes=utilidad_acumulada_mes,
    )

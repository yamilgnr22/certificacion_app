"""Notas integradoras (anexos del DOCX) — ESPEC seccion 8, decision #11.

Cuatro notas estandar, SIEMPRE cuadradas contra el ESF de corte:
  1. Integracion del Efectivo        -> ESF_Corte Efectivo
  2. Integracion de los Inventarios  -> ESF_Corte Inventarios
  3. Integracion de PPE              -> ESF_Corte No Corrientes
  4. Integracion de Pasivos          -> ESF_Corte Total Pasivos

V1: sin desglose custom (una linea por concepto). La depreciacion acumulada
de la nota 3 se prorratea por costo de adquisicion entre los activos, con
ajuste de redondeo en la ultima fila para que el total pegue EXACTO al ESF.
(El desglose real por activo depende de vidas utiles que V1 no modela; si
el cliente lo da, sera input opcional en V2.)
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Nota:
    numero: int
    titulo: str
    columnas: list[str]
    filas: list[list]  # [descripcion, *montos]
    total: list        # fila de total (label + montos)


class NotasError(ValueError):
    """Error bloqueante al construir las notas (p.ej. caja negativa)."""


def _redondear(x: float) -> float:
    # Cordobas enteros (ver motor/er._redondear): cuadre exacto para el banco.
    return round(float(x), 0)


def _fecha_corte(mes_final: str) -> str:
    y, m = int(mes_final[:4]), int(mes_final[5:7])
    d = calendar.monthrange(y, m)[1]
    return f"{d:02d}/{m:02d}/{y}"


def _filas_efectivo(
    efectivo_corte: float,
    cuentas: Sequence[Mapping[str, Any]],
    tasa_cambio: float,
) -> list[list]:
    """Desglose de la Nota 1: cuentas bancarias + 'Efectivo en Caja' residuo.

    Regla del CPA: la caja NO se teclea — es la diferencia entre el efectivo
    del ESF y la suma de las cuentas (en NIO, enteros). Si da negativa, las
    cuentas suman mas que el efectivo reportado: error BLOQUEANTE (una caja
    negativa nunca debe llegar al banco)."""
    filas_cuentas: list[list] = []
    suma = 0.0
    for c in cuentas:
        saldo = float(c.get("saldo") or 0)
        moneda = str(c.get("moneda") or "NIO").upper()
        saldo_nio = _redondear(saldo * tasa_cambio if moneda == "USD" else saldo)
        banco = str(c.get("banco") or "").strip()
        tipo = str(c.get("tipo") or "").strip()
        numero = str(c.get("numero") or "").strip()
        partes = [p for p in (banco, tipo, moneda) if p]
        desc = " ".join(partes) + (f" No. {numero}" if numero else "")
        filas_cuentas.append([desc or "Cuenta bancaria", saldo_nio])
        suma = _redondear(suma + saldo_nio)

    caja = _redondear(_redondear(efectivo_corte) - suma)
    if caja < 0:
        raise NotasError(
            f"Las cuentas bancarias suman {suma:,.0f} NIO pero el efectivo del "
            f"ESF al corte es {_redondear(efectivo_corte):,.0f} NIO: el 'Efectivo en Caja' "
            f"daria {caja:,.0f} (negativo). Revisa los saldos de las cuentas o el "
            "efectivo del balance antes de generar la nota."
        )
    filas: list[list] = []
    if caja > 0:
        filas.append(["Efectivo en Caja", caja])
    filas.extend(filas_cuentas)
    return filas or [["Efectivo en Caja y Bancos", _redondear(efectivo_corte)]]


def construir_notas(modelo, cuentas_bancarias: Sequence[Mapping[str, Any]] | None = None) -> list[Nota]:
    """modelo: ModeloCertificacion (Tipo A o B).

    cuentas_bancarias (opcional): [{banco, tipo, moneda, numero, saldo}] para
    desglosar la Nota 1; sin ellas, una sola linea como siempre."""
    corte = modelo.esf.corte()
    fecha = _fecha_corte(modelo.inputs.periodo.mes_final)

    # ---- 1. Efectivo (desglose por cuenta si el CPA cargo las cuentas)
    if cuentas_bancarias:
        filas_efectivo = _filas_efectivo(
            corte.efectivo, cuentas_bancarias, modelo.inputs.periodo.tasa_cambio
        )
    else:
        filas_efectivo = [["Efectivo en Caja y Bancos", _redondear(corte.efectivo)]]
    nota1 = Nota(
        numero=1,
        titulo="INTEGRACION DEL EFECTIVO Y EQUIVALENTES DE EFECTIVO",
        columnas=["DESCRIPCION", "SALDO NIO"],
        filas=filas_efectivo,
        total=[f"Total Efectivo y Equivalentes al {fecha}", _redondear(corte.efectivo)],
    )

    # ---- 2. Inventarios
    nota2 = Nota(
        numero=2,
        titulo="INTEGRACION DE LOS INVENTARIOS",
        columnas=["DESCRIPCION", "SALDO NIO"],
        filas=[["Inventarios de mercaderia", _redondear(corte.inventarios)]],
        total=[f"Total Inventarios al {fecha}", _redondear(corte.inventarios)],
    )

    # ---- 3. PPE (costo, depreciacion prorrateada por costo, valor en libros)
    activos = [
        ("Bienes Inmuebles", _redondear(corte.bienes_inmuebles)),
        ("Mobiliario y Equipos", _redondear(corte.mobiliario_equipos)),
        ("Vehiculos", _redondear(corte.vehiculos)),
    ]
    costo_total = _redondear(sum(c for _, c in activos))
    depr_total = _redondear(corte.depreciacion_acumulada)  # negativa
    filas_ppe: list[list] = []
    depr_asignada = 0.0
    idx_ultimo_con_costo = max(
        (i for i, (_, c) in enumerate(activos) if c > 0), default=-1
    )
    for i, (nombre, costo) in enumerate(activos):
        if costo_total > 0 and costo > 0:
            if i == idx_ultimo_con_costo:
                depr_i = _redondear(depr_total - depr_asignada)  # ajuste de redondeo
            else:
                depr_i = _redondear(depr_total * (costo / costo_total))
                depr_asignada = _redondear(depr_asignada + depr_i)
        else:
            depr_i = 0.0
        filas_ppe.append([nombre, costo, depr_i, _redondear(costo + depr_i)])
    valor_libros_total = _redondear(costo_total + depr_total)
    nota3 = Nota(
        numero=3,
        titulo="INTEGRACION DE LA PROPIEDAD PLANTA Y EQUIPO",
        columnas=["DESCRIPCION", "COSTO DE ADQUISICION NIO", "DEPRECIACION ACUMULADA", "VALOR EN LIBROS NIO"],
        filas=filas_ppe,
        total=[f"Total Propiedad Planta y Equipo, Neto al {fecha}", costo_total, depr_total, valor_libros_total],
    )

    # ---- 4. Pasivos (solo cuentas con saldo; si no hay, una linea en 0)
    cuentas_pasivo = [
        ("Tarjetas de Credito", corte.tarjetas_credito),
        ("Proveedores", corte.proveedores),
        ("Impuestos por Pagar", corte.impuestos_por_pagar),
        ("Gastos Acumulados por pagar", corte.gastos_acumulados),
        ("Creditos Hipotecarios", corte.creditos_hipotecarios),
        ("Creditos Consumo", corte.creditos_consumo),
        ("Creditos Personales", corte.creditos_personales),
        ("Creditos Prendarios", corte.creditos_prendarios),
        ("Creditos Comerciales", corte.creditos_comerciales),
    ]
    filas_pasivo = [[n, _redondear(v)] for n, v in cuentas_pasivo if abs(v) > 0.005]
    if not filas_pasivo:
        filas_pasivo = [["Sin pasivos al corte", 0.0]]
    nota4 = Nota(
        numero=4,
        titulo="INTEGRACION DE PASIVOS",
        columnas=["DESCRIPCION", "SALDO NIO"],
        filas=filas_pasivo,
        total=[f"Total Pasivos al {fecha}", _redondear(corte.total_pasivos)],
    )

    return [nota1, nota2, nota3, nota4]

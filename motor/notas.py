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


@dataclass(frozen=True)
class Nota:
    numero: int
    titulo: str
    columnas: list[str]
    filas: list[list]  # [descripcion, *montos]
    total: list        # fila de total (label + montos)


def _redondear(x: float) -> float:
    return round(float(x), 2)


def _fecha_corte(mes_final: str) -> str:
    y, m = int(mes_final[:4]), int(mes_final[5:7])
    d = calendar.monthrange(y, m)[1]
    return f"{d:02d}/{m:02d}/{y}"


def construir_notas(modelo) -> list[Nota]:
    """modelo: ModeloCertificacion (Tipo A o B)."""
    corte = modelo.esf.corte()
    fecha = _fecha_corte(modelo.inputs.periodo.mes_final)

    # ---- 1. Efectivo
    nota1 = Nota(
        numero=1,
        titulo="INTEGRACION DEL EFECTIVO Y EQUIVALENTES DE EFECTIVO",
        columnas=["DESCRIPCION", "SALDO NIO"],
        filas=[["Efectivo en Caja y Bancos", _redondear(corte.efectivo)]],
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

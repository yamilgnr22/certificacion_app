"""Campos calculados de la hoja Certificacion + snapshot de Datos → DOCX.

OJO nomenclatura (ESPEC seccion 3):
  - "Ingresos Brutos" de la Certificacion = TOTAL de Ingresos del periodo
    (NO la linea "(=) UTILIDAD BRUTA" del ER). Es la practica actual.
  - "Utilidad del Periodo" = utilidad neta acumulada del ER.
  - Promedios = acumulado / numero de meses.
"""

from __future__ import annotations

import calendar

import pandas as pd

from motor.er import CalculoER
from motor.inputs import DatosCliente, PeriodoSpec


def _ultimo_dia(yyyy_mm: str) -> str:
    y, m = int(yyyy_mm[:4]), int(yyyy_mm[5:7])
    d = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-{d:02d}"


def _primer_dia(yyyy_mm: str) -> str:
    return f"{yyyy_mm}-01"


def construir_certificacion(
    datos: DatosCliente,
    periodo: PeriodoSpec,
    calculo_er: CalculoER,
) -> pd.DataFrame:
    ingresos_brutos = round(calculo_er.total_ingresos(), 2)
    ingresos_promedio = round(calculo_er.promedio_ingresos(), 2)
    utilidad_periodo = round(calculo_er.total_utilidad_neta(), 2)
    utilidad_promedio = round(calculo_er.promedio_utilidad(), 2)

    filas = [
        ("Nombre completo", datos.nombre_completo, True),
        ("Cedula", datos.cedula, True),
        ("Fecha Inicio", _primer_dia(periodo.mes_inicial), True),
        ("Fecha Fin", _ultimo_dia(periodo.mes_final), True),
        ("Estado Civil", datos.estado_civil, bool(datos.estado_civil)),
        ("Profesion", datos.profesion, bool(datos.profesion)),
        ("Sexo", datos.sexo, bool(datos.sexo)),
        ("Domicilio", datos.domicilio, bool(datos.domicilio)),
        ("Direccion Negocio", datos.direccion_negocio, bool(datos.direccion_negocio)),
        ("Primer Apellido", datos.primer_apellido, bool(datos.primer_apellido)),
        ("Ingresos Brutos", ingresos_brutos, True),
        ("Ingresos Promedio", ingresos_promedio, True),
        ("Utilidad del Periodo", utilidad_periodo, True),
        ("Utilidad Promedio", utilidad_promedio, True),
        ("Banco", datos.banco, bool(datos.banco)),
        ("Fecha Certificacion", datos.fecha_certificacion.isoformat(), True),
        ("Contacto", datos.contacto, bool(datos.contacto)),
        ("Regimen", datos.regimen, bool(datos.regimen)),
        ("Matricula", datos.matricula, bool(datos.matricula)),
        ("Giro", datos.giro, bool(datos.giro)),
        ("Antiguedad", datos.antiguedad, bool(datos.antiguedad)),
        ("Empleados", datos.empleados, True),
    ]
    return pd.DataFrame(
        [[desc, val, chk] for desc, val, chk in filas],
        columns=["Descripcion", "Datos", "Check List"],
    )


def construir_datos(datos: DatosCliente) -> pd.DataFrame:
    """Tabla 'Datos' del DOCX: ficha COMPLETA del cliente, como en los
    documentos reales del CPA (10 filas, separador ':'). 3 columnas sin
    encabezado: generar_tabla_datos asigna anchos por indice (4|1|13.5 cm)."""
    filas = [
        ["Nombre", ":", datos.nombre_completo or ""],
        ["Domicilio personal", ":", datos.domicilio or ""],
        ["Contacto", ":", datos.contacto or ""],
        ["Cedula de identidad", ":", datos.cedula or ""],
        ["Regimen", ":", datos.regimen or ""],
        ["Matricula Alcaldia No.", ":", datos.matricula or ""],
        ["Direccion del negocio", ":", datos.direccion_negocio or ""],
        ["Giro del Negocio", ":", datos.giro or ""],
        ["Antiguedad", ":", datos.antiguedad or ""],
        ["Empleados", ":", datos.empleados or ""],
    ]
    return pd.DataFrame(filas)

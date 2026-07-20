"""Orquestador del motor: InputsTipoA/InputsTipoB -> ModeloCertificacion.

Encadena: amortizacion -> er -> mov -> esf -> validar -> certificacion.
No genera el DOCX (eso lo hace el endpoint reusando generar_documento_completo).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

import pandas as pd

from motor.amortizacion import planes_activos, planes_documentales, resolver_planes
from motor.certificacion import construir_certificacion, construir_datos
from motor.er import CalculoER, construir_er
from motor.esf import CalculoESF, construir_esf
from motor.inputs import InputsTipoA, InputsTipoB, PlanResuelto
from motor.mov import CalculoMov, construir_mov
from motor.tipo_b import construir_tipo_b
from motor.validar import ResultadoValidacion, validar_tipo_a, validar_tipo_b


@dataclass(frozen=True)
class ModeloCertificacion:
    inputs: Union[InputsTipoA, InputsTipoB]
    planes: list[PlanResuelto]  # activos (impactan ER/Mov/ESF)
    planes_soporte: list[PlanResuelto]  # documentales (solo anexos)
    er: CalculoER
    mov: CalculoMov
    esf: CalculoESF
    validacion: ResultadoValidacion
    df_certificacion: pd.DataFrame
    df_datos: pd.DataFrame

    @property
    def df_er(self) -> pd.DataFrame:
        return self.er.df

    @property
    def df_esf_mensual(self) -> pd.DataFrame:
        return self.esf.df_mensual

    @property
    def df_esf_corte(self) -> pd.DataFrame:
        return self.esf.df_corte

    @property
    def ok(self) -> bool:
        return self.validacion.ok


def certificar_tipo_a(inputs: InputsTipoA) -> ModeloCertificacion:
    todos = resolver_planes(inputs.deudas, inputs.periodo)
    activos = planes_activos(todos)
    soporte = planes_documentales(todos)
    er = construir_er(inputs.er_mensual, activos, inputs.periodo)
    mov = construir_mov(er, activos, inputs.periodo, inputs.saldos_iniciales)
    esf = construir_esf(inputs, er, mov, activos)
    validacion = validar_tipo_a(inputs, activos, er, mov, esf)
    df_cert = construir_certificacion(inputs.datos, inputs.periodo, er)
    df_datos = construir_datos(inputs.datos)
    return ModeloCertificacion(
        inputs=inputs,
        planes=activos,
        planes_soporte=soporte,
        er=er,
        mov=mov,
        esf=esf,
        validacion=validacion,
        df_certificacion=df_cert,
        df_datos=df_datos,
    )


def certificar_tipo_b(inputs: InputsTipoB) -> ModeloCertificacion:
    todos = resolver_planes(inputs.deudas, inputs.periodo)
    activos = planes_activos(todos)
    soporte = planes_documentales(todos)
    er = construir_er(inputs.er_mensual, activos, inputs.periodo)
    mov, esf = construir_tipo_b(inputs, er, activos)
    validacion = validar_tipo_b(inputs, activos, er, mov, esf)
    df_cert = construir_certificacion(inputs.datos, inputs.periodo, er)
    df_datos = construir_datos(inputs.datos)
    return ModeloCertificacion(
        inputs=inputs,
        planes=activos,
        planes_soporte=soporte,
        er=er,
        mov=mov,
        esf=esf,
        validacion=validacion,
        df_certificacion=df_cert,
        df_datos=df_datos,
    )

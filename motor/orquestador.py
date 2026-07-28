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

# Las amplitudes de banda viven en inputs.Bandas (configurables desde la UI).


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


def _creditos_sin_plan_tipo_a(
    inputs: InputsTipoA, meses: list[str], planes: list[PlanResuelto]
) -> dict[str, dict[str, float]]:
    """Cuentas de credito DECLARADAS en el balance que ningun credito del
    reporte alimenta (p.ej. una tarjeta que el reporte de deuda no lista).

    Sin esto quedan planas todo el periodo. Como cualquier otro pasivo,
    oscilan en banda entre el saldo inicial y el final declarados y anclan en
    el final; el movimiento pasa por la caja (sube el pasivo = entra
    efectivo; baja = se pago)."""
    from motor.deuda_generada import trayectoria_con_ancla
    from motor.esf import _CUENTAS_CREDITO

    con_plan = {p.cuenta_esf for p in planes}
    out: dict[str, dict[str, float]] = {}
    for cuenta in sorted(_CUENTAS_CREDITO):
        if cuenta in con_plan:
            continue  # el modulo de deuda ya la mueve
        inicial = float(getattr(inputs.saldos_iniciales, cuenta) or 0.0)
        final = float(getattr(inputs.saldos_finales, cuenta) or 0.0)
        if inicial <= 0 and final <= 0:
            continue
        seed = (
            f"{inputs.datos.cedula}|{inputs.periodo.mes_inicial}|"
            f"{inputs.periodo.mes_final}|{cuenta}"
        )
        out[cuenta] = trayectoria_con_ancla(
            inicial, final, meses, banda_pct=inputs.bandas.creditos_pct, seed=seed
        )
    return out


def _trayectoria_cuenta_tipo_a(
    inputs: InputsTipoA, meses: list[str], cuenta: str, sufijo_seed: str
) -> dict[str, float] | None:
    """Trayectoria de una cuenta operativa (inventarios / proveedores / CxC) en
    Tipo A: oscila en banda alrededor de la tendencia inicial->final y ANCLA
    en el saldo final del balance. None si la cuenta esta en cero (nada que
    mover)."""
    from motor.deuda_generada import trayectoria_con_ancla

    inicial = float(getattr(inputs.saldos_iniciales, cuenta) or 0.0)
    final = float(getattr(inputs.saldos_finales, cuenta) or 0.0)
    if inicial <= 0 and final <= 0:
        return None
    seed = (
        f"{inputs.datos.cedula}|{inputs.periodo.mes_inicial}|"
        f"{inputs.periodo.mes_final}|{sufijo_seed}"
    )
    banda = {"proveedores": inputs.bandas.proveedores_pct,
             "cuentas_por_cobrar": inputs.bandas.cxc_pct}.get(
                 cuenta, inputs.bandas.inventario_pct)
    return trayectoria_con_ancla(inicial, final, meses, banda_pct=banda, seed=seed)


def certificar_tipo_a(inputs: InputsTipoA) -> ModeloCertificacion:
    # Los saldos iniciales declarados (vienen del ESF de la certificacion
    # anterior) son el ancla de apertura de las cuentas de credito.
    todos = resolver_planes(inputs.deudas, inputs.periodo, inputs.saldos_iniciales, inputs.bandas)
    activos = planes_activos(todos)
    soporte = planes_documentales(todos)
    er = construir_er(inputs.er_mensual, activos, inputs.periodo)
    inv_mensual = _trayectoria_cuenta_tipo_a(inputs, er.meses, "inventarios", "inv")
    prov_mensual = _trayectoria_cuenta_tipo_a(inputs, er.meses, "proveedores", "prov")
    cxc_mensual = _trayectoria_cuenta_tipo_a(inputs, er.meses, "cuentas_por_cobrar", "cxc")
    cred_sin_plan = _creditos_sin_plan_tipo_a(inputs, er.meses, activos)
    mov = construir_mov(
        er, activos, inputs.periodo, inputs.saldos_iniciales,
        inv_mensual, prov_mensual, cred_sin_plan, cxc_mensual,
    )
    esf = construir_esf(
        inputs, er, mov, activos, inv_mensual, prov_mensual, cred_sin_plan, cxc_mensual,
    )
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
    todos = resolver_planes(inputs.deudas, inputs.periodo, inputs.saldos_iniciales, inputs.bandas)
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

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


def _palancas_mes(
    inputs, mes: str,
    inv_mensual: dict | None, prov_mensual: dict | None, cxc_mensual: dict | None,
) -> list:
    """Cuentas que el solver puede mover ese mes para salvar la caja.

    La escala de cada palanca es su banda en NIO: la cuenta que oscila mas
    es la que menos se nota si se la mueve, asi que absorbe mas del ajuste.
    Los minimos configurados son las cotas duras."""
    from motor.solver import Palanca

    b, m = inputs.bandas, inputs.minimos
    out = []
    if inv_mensual and mes in inv_mensual:
        d = inv_mensual[mes]
        out.append(Palanca("inventarios", d, -1,
                           escala=max(1.0, d * b.inventario_pct / 100.0),
                           minimo=m.inventario))
    if prov_mensual and mes in prov_mensual:
        d = prov_mensual[mes]
        out.append(Palanca("proveedores", d, +1,
                           escala=max(1.0, d * b.proveedores_pct / 100.0)))
    if cxc_mensual and mes in cxc_mensual:
        d = cxc_mensual[mes]
        out.append(Palanca("cuentas_por_cobrar", d, -1,
                           escala=max(1.0, d * b.cxc_pct / 100.0), minimo=0.0))
    return out


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
    # Correcciones que el solver tuvo que hacer para que la caja no bajara
    # del piso. None = la trayectoria deseada ya era factible.
    solver: object | None = None

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


def _aplicar(trayectoria: dict | None, correcciones: dict | None) -> dict | None:
    """Trayectoria con los meses que el solver corrigio pisados."""
    if not correcciones:
        return trayectoria
    return {**(trayectoria or {}), **correcciones}


def _resolver_caja_tipo_a(inputs, er, mov, inv_mensual, prov_mensual, cxc_mensual):
    """None si la caja ya respeta el piso (caso normal: el solver no toca nada)."""
    from motor.solver import resolver_caja

    piso = float(inputs.minimos.caja)
    caja = {m: mov.saldo_final_mes(m) for m in er.meses}
    if all(v >= piso for v in caja.values()):
        return None

    ultimo = er.meses[-1]
    palancas = {
        mes: ([] if mes == ultimo
              else _palancas_mes(inputs, mes, inv_mensual, prov_mensual, cxc_mensual))
        for mes in er.meses
    }
    # Tipo A no admite aporte del propietario: alteraria el patrimonio y el
    # balance final declarado es ancla dura.
    return resolver_caja(er.meses, caja, piso, palancas, permite_aporte=False)


def _resolver_caja_tipo_b(inputs, er, mov, esf):
    """Como el Tipo A, pero las palancas salen del ESF ya calculado (en Tipo B
    las trayectorias se derivan dentro del modelo) y se admite aporte."""
    from motor.solver import resolver_caja

    piso = float(inputs.minimos.caja)
    caja = {m.mes: m.saldo_final for m in mov.movs}
    if all(v >= piso for v in caja.values()):
        return None

    por_mes = {e.mes: e for e in esf.meses}
    palancas = {}
    for mes in er.meses:
        e = por_mes[mes]
        palancas[mes] = _palancas_mes(
            inputs, mes,
            {mes: e.inventarios} if e.inventarios else None,
            {mes: e.proveedores} if e.proveedores else None,
            {mes: e.cuentas_por_cobrar} if e.cuentas_por_cobrar else None,
        )
    return resolver_caja(
        er.meses, caja, piso, palancas,
        aporte_maximo=inputs.minimos.aporte_maximo, permite_aporte=True,
    )


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

    # Solver: si algun mes queda bajo el piso de caja, se corrigen las
    # trayectorias lo minimo posible y se recalcula. El ultimo mes no lleva
    # palancas: el balance final declarado es ancla dura y no se toca.
    solver = _resolver_caja_tipo_a(inputs, er, mov, inv_mensual, prov_mensual, cxc_mensual)
    if solver is not None and solver.toco_algo:
        inv_mensual = _aplicar(inv_mensual, solver.saldos.get("inventarios"))
        prov_mensual = _aplicar(prov_mensual, solver.saldos.get("proveedores"))
        cxc_mensual = _aplicar(cxc_mensual, solver.saldos.get("cuentas_por_cobrar"))
        mov = construir_mov(
            er, activos, inputs.periodo, inputs.saldos_iniciales,
            inv_mensual, prov_mensual, cred_sin_plan, cxc_mensual,
        )

    esf = construir_esf(
        inputs, er, mov, activos, inv_mensual, prov_mensual, cred_sin_plan, cxc_mensual,
    )
    validacion = validar_tipo_a(inputs, activos, er, mov, esf, solver)
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
        solver=solver,
    )


def certificar_tipo_b(inputs: InputsTipoB) -> ModeloCertificacion:
    todos = resolver_planes(inputs.deudas, inputs.periodo, inputs.saldos_iniciales, inputs.bandas)
    activos = planes_activos(todos)
    soporte = planes_documentales(todos)
    er = construir_er(inputs.er_mensual, activos, inputs.periodo)
    mov, esf = construir_tipo_b(inputs, er, activos)

    # Solver: Tipo B no tiene balance final que anclar, asi que ademas de las
    # palancas operativas puede usar el aporte del propietario como ultimo
    # recurso (con el tope configurado en minimos).
    solver = _resolver_caja_tipo_b(inputs, er, mov, esf)
    if solver is not None and solver.toco_algo:
        aportes = {a.mes: a.aporte_propietario for a in solver.ajustes if a.aporte_propietario}
        mov, esf = construir_tipo_b(inputs, er, activos, solver.saldos, aportes)

    validacion = validar_tipo_b(inputs, activos, er, mov, esf, solver)
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
        solver=solver,
    )

"""Regimen Tipo B: 12 meses, caja oscilante dentro de una banda (ESPEC 12).

No hay balance final dado: el motor PROYECTA manteniendo coherencia mensual
y las cuentas objetivo dentro de su banda (invariante #3).

Mecanica (sin plugs, cuadra por construccion contable):
- Caja: cada mes se calcula el flujo natural
    caja_natural = caja_prev + ventas + financiamiento - compras
                   - gastos_cash - intereses - abonos
  y si excede la trayectoria deseada (objetivo * (1 + osc)), el excedente
  sale como RETIRO DE PATRIMONIO (pago real en Mov, reduce Resultados
  Acumulados en el ESF). Nunca se inyecta efectivo ficticio: si la caja
  esta debajo de la banda (arranque), se queda ahi y validar emite alerta.
- Inventario (objetivo opcional): sigue su trayectoria en banda;
    compras = costo_ventas + (inventario_mes - inventario_prev)
  pagadas de contado. Si compras daria negativo se recorta a 0 y el
  inventario baja solo por el costo de ventas (alerta en validar).
- Oscilacion DETERMINISTA: random.Random(seed) -> camino suave dentro del
  85% de la banda (paso maximo 35% de la tolerancia => sin saltos).
  Mismo input + misma seed = mismo resultado, siempre.

Cuadre (demostracion):
  Δcaja = utilidad + depreciacion - Δinventario + Δcredito - retiro
  ΔActivos = Δcaja + Δinventario - depreciacion = utilidad + Δcredito - retiro
  ΔPasivos = Δcredito ; ΔPatrimonio = utilidad - retiro   ✓ cuadra
"""

from __future__ import annotations

import random

import pandas as pd

from motor.er import CalculoER
from motor.esf import (
    CalculoESF,
    ESFMes,
    _aperturas_credito,
    _build_df_corte,
    _build_df_mensual,
    _capital_apertura,
    _saldos_iniciales_efectivos,
)
from motor.inputs import InputsTipoB, PlanResuelto
from motor.mov import _GASTOS_OPER_NO_DEPR_LABELS, CalculoMov, MovMes, _delta_principal_por_mes


def _redondear(x: float) -> float:
    # Cordobas enteros (ver motor/er._redondear): cuadre exacto para el banco.
    return round(float(x), 0)


def _trayectoria(seed: str, n: int, tolerancia_pct: float) -> list[float]:
    """Camino de oscilacion determinista dentro de la banda.

    Devuelve fracciones osc[t] con |osc| <= 0.85 * tolerancia y paso maximo
    0.35 * tolerancia entre meses (coherencia, sin saltos)."""
    tol = tolerancia_pct / 100.0
    rng = random.Random(seed)
    max_amp = 0.85 * tol
    step = 0.35 * tol
    osc = [rng.uniform(-0.5 * tol, 0.5 * tol)]
    for _ in range(1, n):
        siguiente = osc[-1] + rng.uniform(-step, step)
        osc.append(max(-max_amp, min(max_amp, siguiente)))
    return osc


def _saldo_credito_mes(planes: list[PlanResuelto], si, cuenta: str, mes: str) -> float:
    total = 0.0
    encontrado = False
    for p in planes:
        if p.cuenta_esf == cuenta:
            for c in p.cuotas:
                if c.mes == mes:
                    total += c.saldo_final_nio
                    encontrado = True
    if not encontrado:
        return _redondear(getattr(si, cuenta))
    return _redondear(total)


def construir_tipo_b(
    inputs: InputsTipoB,
    calculo_er: CalculoER,
    planes: list[PlanResuelto],
) -> tuple[CalculoMov, CalculoESF]:
    meses = calculo_er.meses
    aperturas = _aperturas_credito(planes)
    si = _saldos_iniciales_efectivos(inputs.saldos_iniciales, aperturas)
    capital = _capital_apertura(si)

    seed_base = inputs.seed or f"{inputs.datos.cedula}|{inputs.periodo.mes_inicial}|{inputs.periodo.mes_final}"
    obj_caja = inputs.objetivo("efectivo")
    obj_inv = inputs.objetivo("inventarios")
    osc_caja = _trayectoria(seed_base + "|caja", len(meses), obj_caja.tolerancia_pct)
    osc_inv = (
        _trayectoria(seed_base + "|inv", len(meses), obj_inv.tolerancia_pct) if obj_inv else None
    )
    delta_principal = _delta_principal_por_mes(planes, meses)

    # Proveedores oscila en banda alrededor de su saldo de apertura (Tipo B no
    # tiene balance final que anclar). Lo comprado y no pagado queda como
    # pasivo: sale menos efectivo ese mes; cuando el pasivo baja, se paga mas.
    prov_mensual = None
    if _redondear(si.proveedores) > 0:
        from motor.deuda_generada import trayectoria_con_ancla

        prov_mensual = trayectoria_con_ancla(
            si.proveedores, si.proveedores, list(meses),
            banda_pct=inputs.bandas.proveedores_pct, seed=seed_base + "|prov",
        )

    # Cuentas por cobrar: misma logica que proveedores, del otro lado del
    # balance. Cartera que sube = venta no cobrada (entra menos efectivo).
    cxc_mensual = None
    if _redondear(si.cuentas_por_cobrar) > 0:
        from motor.deuda_generada import trayectoria_con_ancla

        cxc_mensual = trayectoria_con_ancla(
            si.cuentas_por_cobrar, si.cuentas_por_cobrar, list(meses),
            banda_pct=inputs.bandas.cxc_pct, seed=seed_base + "|cxc",
        )

    # Cuentas de credito declaradas que ningun credito del reporte alimenta
    # (p.ej. una tarjeta que el reporte no lista): oscilan alrededor de su
    # apertura en vez de quedar planas. Tipo B no tiene balance final que
    # anclar, asi que la banda gira sobre el saldo inicial.
    from motor.deuda_generada import trayectoria_con_ancla as _tray
    from motor.esf import _CUENTAS_CREDITO

    con_plan = {p.cuenta_esf for p in planes}
    cred_sin_plan: dict[str, dict[str, float]] = {}
    for cuenta in sorted(_CUENTAS_CREDITO):
        if cuenta in con_plan:
            continue
        saldo = _redondear(getattr(si, cuenta, 0.0) or 0.0)
        if saldo <= 0:
            continue
        cred_sin_plan[cuenta] = _tray(
            saldo, saldo, list(meses),
            banda_pct=inputs.bandas.creditos_pct, seed=f"{seed_base}|{cuenta}",
        )
    cred_prev = {c: _redondear(getattr(si, c, 0.0) or 0.0) for c in cred_sin_plan}

    movs: list[MovMes] = []
    esf_meses: list[ESFMes] = []
    caja = _redondear(si.efectivo)
    inv_prev = _redondear(si.inventarios)
    prov_prev = _redondear(si.proveedores)
    cxc_prev = _redondear(si.cuentas_por_cobrar)
    ra0 = _redondear(si.resultados_acumulados)
    retiros_acum = 0.0
    depr_acum_periodo = 0.0

    for idx, mes in enumerate(meses):
        ventas = _redondear(calculo_er.ingresos_mes[mes])
        cogs = _redondear(calculo_er.costo_ventas_mes[mes])

        # Inventario objetivo: compras cubren el costo + delta hacia la banda.
        if obj_inv:
            inv_deseado = _redondear(obj_inv.objetivo * (1.0 + osc_inv[idx]))
            compras = _redondear(cogs + (inv_deseado - inv_prev))
            if compras < 0:
                # No se puede "descomprar": el inventario baja solo via cogs.
                compras = 0.0
                inv_mes = _redondear(inv_prev - cogs)
            else:
                inv_mes = inv_deseado
        else:
            compras = cogs
            inv_mes = inv_prev

        gastos_oper = _redondear(
            sum(calculo_er.gastos_por_label_mes[lbl][mes] for lbl in _GASTOS_OPER_NO_DEPR_LABELS)
        )
        financieros = _redondear(calculo_er.gastos_financieros_mes[mes])
        delta = delta_principal[mes]
        # Las cuentas de credito sin plan tambien mueven la caja: si el pasivo
        # sube entra efectivo, si baja se pago.
        cred_mes: dict[str, float] = {}
        for cuenta, saldos in cred_sin_plan.items():
            s = _redondear(saldos.get(mes, cred_prev[cuenta]))
            delta = _redondear(delta + (s - cred_prev[cuenta]))
            cred_prev[cuenta] = s
            cred_mes[cuenta] = s
        financiamiento = _redondear(max(0.0, delta))
        abonos = _redondear(max(0.0, -delta))

        # Pago efectivo de las compras = compras - lo que quedo a credito.
        prov_mes = _redondear(prov_mensual.get(mes, prov_prev)) if prov_mensual else prov_prev
        pago_compras = _redondear(compras - (prov_mes - prov_prev))
        prov_prev = prov_mes

        # Cobranza neta: la venta que quedo en cartera no entra a caja.
        cxc_mes = _redondear(cxc_mensual.get(mes, cxc_prev)) if cxc_mensual else cxc_prev
        cobro_cartera = _redondear(-(cxc_mes - cxc_prev))
        cxc_prev = cxc_mes

        caja_natural = _redondear(
            caja + ventas + cobro_cartera + financiamiento
            - pago_compras - gastos_oper - financieros - abonos
        )
        caja_deseada = _redondear(obj_caja.objetivo * (1.0 + osc_caja[idx]))
        retiro = _redondear(max(0.0, caja_natural - caja_deseada))
        caja_fin = _redondear(caja_natural - retiro)
        retiros_acum = _redondear(retiros_acum + retiro)

        total_cobros = _redondear(ventas + financiamiento + cobro_cartera)
        total_pagos = _redondear(pago_compras + gastos_oper + financieros + abonos + retiro)

        movs.append(MovMes(
            mes=mes,
            saldo_inicial=caja,
            ventas_contado=ventas,
            financiamiento_credito=financiamiento,
            pago_costo_ventas=0.0,  # incluido en pago_compras_inventario
            pago_gastos_operativos=gastos_oper,
            pago_gastos_financieros=financieros,
            pago_abonos_creditos=abonos,
            total_cobros=total_cobros,
            total_pagos=total_pagos,
            saldo_final=caja_fin,
            cobro_cartera=cobro_cartera,
            pago_compras_inventario=pago_compras,
            retiro_patrimonio=retiro,
        ))

        # ----- ESF del mes
        depr_acum_periodo = _redondear(depr_acum_periodo + calculo_er.depreciacion_mes[mes])
        cxc = cxc_mes
        bienes = _redondear(si.bienes_inmuebles)
        mobiliario = _redondear(si.mobiliario_equipos)
        vehiculos = _redondear(si.vehiculos)
        depr_acumulada = _redondear(si.depreciacion_acumulada - depr_acum_periodo)

        def _cred(cuenta: str) -> float:
            # La trayectoria generada manda para las cuentas sin plan (la
            # misma que ya movio la caja arriba).
            if cuenta in cred_mes:
                return cred_mes[cuenta]
            return _saldo_credito_mes(planes, si, cuenta, mes)

        tarjetas = _cred("tarjetas_credito")
        hipotecarios = _cred("creditos_hipotecarios")
        consumo = _cred("creditos_consumo")
        personales = _cred("creditos_personales")
        prendarios = _cred("creditos_prendarios")
        comerciales = _cred("creditos_comerciales")
        proveedores = prov_mes
        impuestos = _redondear(si.impuestos_por_pagar)
        gastos_acum = _redondear(si.gastos_acumulados)

        total_pasivos = _redondear(
            tarjetas + proveedores + impuestos + gastos_acum
            + hipotecarios + consumo + personales + prendarios + comerciales
        )
        resultados_ejercicio = _redondear(calculo_er.utilidad_acumulada_mes[mes])
        # Presentacion confirmada por el CPA (como su Excel): RA constante
        # (= apertura, nunca negativo) y CAPITAL NETO de retiros. El capital
        # mostrado = capital de apertura - retiros acumulados; no hay linea
        # separada de retiros en el ESF.
        resultados_acum = ra0
        capital_neto = _redondear(capital - retiros_acum)
        total_patrimonio = _redondear(
            capital_neto + resultados_acum + resultados_ejercicio
        )
        total_activos = _redondear(
            caja_fin + cxc + inv_mes + bienes + mobiliario + vehiculos + depr_acumulada
        )
        total_pp = _redondear(total_pasivos + total_patrimonio)

        esf_meses.append(ESFMes(
            mes=mes,
            efectivo=caja_fin,
            cuentas_por_cobrar=cxc,
            inventarios=inv_mes,
            bienes_inmuebles=bienes,
            mobiliario_equipos=mobiliario,
            vehiculos=vehiculos,
            depreciacion_acumulada=depr_acumulada,
            total_activos=total_activos,
            tarjetas_credito=tarjetas,
            proveedores=proveedores,
            impuestos_por_pagar=impuestos,
            gastos_acumulados=gastos_acum,
            creditos_hipotecarios=hipotecarios,
            creditos_consumo=consumo,
            creditos_personales=personales,
            creditos_prendarios=prendarios,
            creditos_comerciales=comerciales,
            total_pasivos=total_pasivos,
            capital=capital_neto,
            resultados_acumulados=resultados_acum,
            resultados_ejercicio=resultados_ejercicio,
            total_patrimonio=total_patrimonio,
            total_pasivo_patrimonio=total_pp,
            diferencia=_redondear(total_activos - total_pp),
            retiros_acumulados=retiros_acum,
        ))

        caja = caja_fin
        inv_prev = inv_mes

    # ----- DataFrames
    columnas = ["Concepto", *[m.mes for m in movs]]

    def _fila(label: str, getter) -> list:
        return [label, *[_redondear(getter(m)) for m in movs]]

    rows = [
        _fila("Saldo inicial de caja", lambda x: x.saldo_inicial),
        _fila("Ventas de contado (cobros)", lambda x: x.ventas_contado),
        _fila("Financiamiento de creditos", lambda x: x.financiamiento_credito),
    ]
    # Solo si hay cartera en movimiento (ver motor/mov.construir_mov).
    if any(m.cobro_cartera for m in movs):
        rows.append(_fila("Cobranza neta de cartera", lambda x: x.cobro_cartera))
    rows += [
        _fila("Total entradas de efectivo", lambda x: x.total_cobros),
        _fila("Compras de inventario (incluye costo de ventas)", lambda x: -x.pago_compras_inventario),
        _fila("Pago gastos operativos", lambda x: -x.pago_gastos_operativos),
        _fila("Pago intereses creditos", lambda x: -x.pago_gastos_financieros),
        _fila("Abonos a creditos (principal)", lambda x: -x.pago_abonos_creditos),
        _fila("Retiros de patrimonio", lambda x: -x.retiro_patrimonio),
        _fila("Total salidas de efectivo", lambda x: -x.total_pagos),
        _fila("Saldo final de caja", lambda x: x.saldo_final),
    ]
    df_mov = pd.DataFrame(rows, columns=columnas)

    calculo_mov = CalculoMov(movs=movs, df=df_mov)
    calculo_esf = CalculoESF(
        meses=esf_meses,
        df_mensual=_build_df_mensual(esf_meses, meses),
        df_corte=_build_df_corte(esf_meses[-1]),
        capital_apertura=capital,
    )
    return calculo_mov, calculo_esf

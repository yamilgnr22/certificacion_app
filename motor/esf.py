"""Construye el ESF mensual (Tipo A) con caja derivada y anclas duras.

Reglas (Decisiones_y_Reglas_Motor.md C):
  #4 cada mes Total Activos = Total Pasivo + Patrimonio, SIN plug en Capital.
  #7 Efectivo del ESF = saldo final de Mov (no inventado).
  #8 Depreciacion Acumulada = Depr.Acum0 - suma depreciaciones del ER.
  #9 Capital constante = Activos0 - Pasivos0 (plug de APERTURA, una vez).
  #2 Tipo A: ESF_Corte = saldos finales dados por el cliente (exacto).

Como cuadra el balance mes a mes SIN ningun plug:
  En el modelo V1 (todo contado, sin compras ni capex) el balance cuadra
  por construccion contable. Prueba con ingresos I, cogs C, interes T, abono A:
    ER   -> utilidad = I - C - T          => patrimonio += (I-C-T)
    Mov  -> caja += I - (C + T + A)
    ESF  -> activo caja += I-C-T-A ; pasivo credito -= A
            activos(+I-C-T-A) = pasivos(-A) + patrimonio(+I-C-T)  ✓
  La depreciacion baja patrimonio (utilidad) y baja activo neto (depr. acum.)
  por el mismo monto, sin tocar caja: tambien cuadra.

  Por eso NO hay cuenta de ajuste ni plug. CxC e Inventarios quedan en su
  saldo inicial (V1 no los mueve). La 'diferencia' del ESF es un CHECK real
  (invariante #4): si no da ~0, hay un error de modelado, no se maquilla.

Para Tipo A el cliente da los saldos finales; motor/validar.py compara el
ESF calculado en el mes de corte contra ellos (invariante #2) y reporta las
diferencias por cuenta. No se fuerzan los saldos: si no coinciden, los
inputs del cliente son inconsistentes y hay que corregirlos.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from motor.er import CalculoER
from motor.inputs import ESF_Saldos, InputsTipoA, PeriodoSpec, PlanResuelto
from motor.mov import CalculoMov


TOLERANCIA = 1.0  # NIO


class ESFError(ValueError):
    pass


@dataclass(frozen=True)
class ESFMes:
    mes: str
    # Activos
    efectivo: float
    cuentas_por_cobrar: float
    inventarios: float
    bienes_inmuebles: float
    mobiliario_equipos: float
    vehiculos: float
    depreciacion_acumulada: float
    total_activos: float
    # Pasivos
    tarjetas_credito: float
    proveedores: float
    impuestos_por_pagar: float
    gastos_acumulados: float
    creditos_hipotecarios: float
    creditos_consumo: float
    creditos_personales: float
    creditos_prendarios: float
    creditos_comerciales: float
    total_pasivos: float
    # Patrimonio
    capital: float
    resultados_acumulados: float
    resultados_ejercicio: float
    total_patrimonio: float
    total_pasivo_patrimonio: float
    diferencia: float  # total_activos - total_pasivo_patrimonio
    # Contra-patrimonio: retiros del propietario acumulados (Tipo B). Se
    # presentan en su propia linea "(-) Retiros del Propietario"; NUNCA se
    # netean contra Resultados Acumulados (dejaria RA negativo en el ESF).
    retiros_acumulados: float = 0.0


@dataclass(frozen=True)
class CalculoESF:
    meses: list[ESFMes]
    df_mensual: pd.DataFrame
    df_corte: pd.DataFrame
    capital_apertura: float

    def corte(self) -> ESFMes:
        return self.meses[-1]


def _redondear(x: float) -> float:
    # Cordobas enteros (ver motor/er._redondear): cuadre exacto para el banco.
    return round(float(x), 0)


# Cuentas de pasivo alimentadas por planes; el resto queda en su saldo inicial (V1 no las mueve)
_CUENTAS_CREDITO = {
    "tarjetas_credito",
    "creditos_hipotecarios",
    "creditos_consumo",
    "creditos_personales",
    "creditos_prendarios",
    "creditos_comerciales",
}


def _aperturas_credito(planes: list[PlanResuelto]) -> dict[str, float]:
    """Suma del saldo de apertura (NIO) por cuenta de credito del ESF.

    La fuente de verdad de las cuentas de credito es el modulo de deuda, no
    saldos_iniciales del cliente: la apertura de cada cuenta = suma de las
    aperturas de los creditos que mapean ahi (decision: apertura por credito)."""
    out: dict[str, float] = {c: 0.0 for c in _CUENTAS_CREDITO}
    for p in planes:
        out[p.cuenta_esf] = _redondear(out.get(p.cuenta_esf, 0.0) + p.saldo_apertura_nio)
    return out


def _saldos_iniciales_efectivos(si: ESF_Saldos, aperturas: dict[str, float]) -> ESF_Saldos:
    """ESF_Saldos con las cuentas de credito resueltas.

    Prioridad: lo que DECLARA el cliente manda. El saldo de apertura de una
    cuenta de credito suele venir de una certificacion anterior (un ESF ya
    emitido y firmado), asi que es un hecho, no algo a estimar; el modulo de
    deuda ya recalibro las aperturas de los creditos contra el (ver
    amortizacion._calibrar_aperturas). Solo cuando el cliente NO declara nada
    (cuenta en 0) se usa la apertura derivada de los planes."""
    import dataclasses

    overrides: dict[str, float] = {}
    for cuenta, apertura in aperturas.items():
        if apertura > 0.0 and abs(_redondear(getattr(si, cuenta))) <= TOLERANCIA:
            overrides[cuenta] = apertura
    return dataclasses.replace(si, **overrides) if overrides else si


def _capital_apertura(saldos_iniciales: ESF_Saldos) -> float:
    """Capital0 = Activos0 - Pasivos0 - ResultAcum0 (plug de APERTURA, una vez).

    Se resta Resultados Acumulados porque es la otra cuenta de patrimonio de
    apertura: sin restarla el balance del mes 0 no cuadra cuando RA0 != 0.
    Si el cliente envia Capital explicito, se valida (bloqueante)."""
    calculado = _redondear(
        saldos_iniciales.total_activos()
        - saldos_iniciales.total_pasivos()
        - saldos_iniciales.resultados_acumulados
    )
    enviado = saldos_iniciales.capital
    if enviado is not None:
        if abs(_redondear(enviado) - calculado) > TOLERANCIA:
            raise ESFError(
                f"Capital inicial enviado ({enviado:,.2f}) no cuadra con "
                f"Activos0 - Pasivos0 - ResultAcum0 ({calculado:,.2f}). Diferencia "
                f"{enviado - calculado:,.2f} NIO. El balance de apertura debe "
                f"cuadrar; corregir saldos iniciales (no se maquilla)."
            )
    return calculado


def construir_esf(
    inputs: InputsTipoA,
    calculo_er: CalculoER,
    calculo_mov: CalculoMov,
    planes: list[PlanResuelto],
    inventario_mensual: dict[str, float] | None = None,
    proveedores_mensual: dict[str, float] | None = None,
) -> CalculoESF:
    """inventario_mensual / proveedores_mensual (opcionales): trayectorias mes
    a mes (banda oscilante que ancla en el saldo final). Deben ser las MISMAS
    que se pasaron a construir_mov, para que la caja refleje esas compras y
    pagos y el balance cuadre. Sin ellas, ambas cuentas quedan constantes en
    su saldo inicial."""
    aperturas = _aperturas_credito(planes)
    si = _saldos_iniciales_efectivos(inputs.saldos_iniciales, aperturas)
    capital = _capital_apertura(si)

    # Saldos de credito por mes desde los planes (agregados por cuenta_esf).
    # Si ningun plan toca la cuenta ese mes, queda en su saldo inicial.
    def saldo_credito_cuenta_mes(cuenta: str, mes: str) -> float:
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

    meses_esf: list[ESFMes] = []
    for mes in calculo_er.meses:
        efectivo = _redondear(calculo_mov.saldo_final_mes(mes))
        # CxC no se mueve (todo contado). El inventario sigue su trayectoria
        # si se dio (banda que ancla en el saldo final); si no, constante.
        cxc = _redondear(si.cuentas_por_cobrar)
        inventarios = _redondear(
            inventario_mensual.get(mes, si.inventarios) if inventario_mensual
            else si.inventarios
        )
        # PPE constante (no capex); depreciacion acumulada crece linealmente.
        bienes = _redondear(si.bienes_inmuebles)
        mobiliario = _redondear(si.mobiliario_equipos)
        vehiculos = _redondear(si.vehiculos)
        depr_acum_periodo = sum(
            calculo_er.depreciacion_mes[m] for m in calculo_er.meses[: calculo_er.meses.index(mes) + 1]
        )
        depr_acumulada = _redondear(si.depreciacion_acumulada - depr_acum_periodo)

        # Pasivos de credito desde planes; el resto en saldo inicial (V1).
        tarjetas = saldo_credito_cuenta_mes("tarjetas_credito", mes)
        hipotecarios = saldo_credito_cuenta_mes("creditos_hipotecarios", mes)
        consumo = saldo_credito_cuenta_mes("creditos_consumo", mes)
        personales = saldo_credito_cuenta_mes("creditos_personales", mes)
        prendarios = saldo_credito_cuenta_mes("creditos_prendarios", mes)
        comerciales = saldo_credito_cuenta_mes("creditos_comerciales", mes)
        proveedores = _redondear(
            proveedores_mensual.get(mes, si.proveedores) if proveedores_mensual
            else si.proveedores
        )
        impuestos = _redondear(si.impuestos_por_pagar)
        gastos_acum = _redondear(si.gastos_acumulados)

        total_pasivos = _redondear(
            tarjetas + proveedores + impuestos + gastos_acum
            + hipotecarios + consumo + personales + prendarios + comerciales
        )

        resultados_ejercicio = _redondear(calculo_er.utilidad_acumulada_mes[mes])
        resultados_acum = _redondear(si.resultados_acumulados)
        total_patrimonio = _redondear(capital + resultados_acum + resultados_ejercicio)

        total_activos = _redondear(
            efectivo + cxc + inventarios + bienes + mobiliario + vehiculos + depr_acumulada
        )
        total_pp = _redondear(total_pasivos + total_patrimonio)
        diferencia = _redondear(total_activos - total_pp)

        meses_esf.append(ESFMes(
            mes=mes,
            efectivo=efectivo,
            cuentas_por_cobrar=cxc,
            inventarios=inventarios,
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
            capital=capital,
            resultados_acumulados=resultados_acum,
            resultados_ejercicio=resultados_ejercicio,
            total_patrimonio=total_patrimonio,
            total_pasivo_patrimonio=total_pp,
            diferencia=diferencia,
        ))

    df_mensual = _build_df_mensual(meses_esf, calculo_er.meses)
    df_corte = _build_df_corte(meses_esf[-1])

    return CalculoESF(
        meses=meses_esf,
        df_mensual=df_mensual,
        df_corte=df_corte,
        capital_apertura=capital,
    )


def _ts(mes: str) -> pd.Timestamp:
    import calendar
    y, mo = int(mes[:4]), int(mes[5:7])
    return pd.Timestamp(year=y, month=mo, day=calendar.monthrange(y, mo)[1])


def _build_df_mensual(meses_esf: list[ESFMes], meses: list[str]) -> pd.DataFrame:
    cols = ["Descripcion", *[_ts(m) for m in meses]]

    def fila(label: str, attr: str) -> list:
        return [label, *[_redondear(getattr(e, attr)) for e in meses_esf]]

    def header(label: str) -> list:
        return [label, *["" for _ in meses_esf]]

    rows = [
        header("Activos"),
        header("Corrientes"),
        fila("Efectivo y Equivalentes de Efectivo", "efectivo"),
        fila("Cuentas por Cobrar Clientes", "cuentas_por_cobrar"),
        fila("Inventarios", "inventarios"),
        header("No Corrientes"),
        fila("Bienes Inmuebles", "bienes_inmuebles"),
        fila("Mobiliario y Equipos", "mobiliario_equipos"),
        fila("Vehiculos", "vehiculos"),
        fila("(-) Depreciacion Acumulada", "depreciacion_acumulada"),
        fila("Total Activos", "total_activos"),
        header("Pasivos"),
        header("Corrientes"),
        fila("Tarjetas de Credito", "tarjetas_credito"),
        fila("Proveedores", "proveedores"),
        fila("Impuestos por Pagar", "impuestos_por_pagar"),
        fila("Gastos Acumulados por pagar", "gastos_acumulados"),
        header("No Corrientes"),
        fila("Creditos Hipotecarios", "creditos_hipotecarios"),
        fila("Creditos Consumo", "creditos_consumo"),
        fila("Creditos Personales", "creditos_personales"),
        fila("Creditos Prendarios", "creditos_prendarios"),
        fila("Creditos Comerciales", "creditos_comerciales"),
        fila("Total Pasivos", "total_pasivos"),
        header("Patrimonio"),
        # Capital NETO de retiros (presentacion del CPA, como su Excel):
        # ESFMes.capital ya viene descontado; no hay linea de retiros.
        fila("Capital", "capital"),
        fila("Resultados Acumulados", "resultados_acumulados"),
        fila("Resultados del Ejercicio", "resultados_ejercicio"),
        fila("Total Patrimonio", "total_patrimonio"),
        fila("Total Pasivo + Patrimonio", "total_pasivo_patrimonio"),
    ]
    return pd.DataFrame(rows, columns=cols)


def _build_df_corte(e: ESFMes) -> pd.DataFrame:
    """ESF al corte con el layout del Excel/DOCX real (caso Gloria): dos
    columnas paralelas con subdivisiones Corrientes / No Corrientes,
    subtotales y bloque de Patrimonio. generar_tabla_esf bold-ea los
    agrupadores por etiqueta, asi que los textos deben coincidir."""
    total_act_corr = _redondear(e.efectivo + e.cuentas_por_cobrar + e.inventarios)
    total_act_nc = _redondear(
        e.bienes_inmuebles + e.mobiliario_equipos + e.vehiculos + e.depreciacion_acumulada
    )
    total_pas_corr = _redondear(
        e.tarjetas_credito + e.proveedores + e.impuestos_por_pagar + e.gastos_acumulados
    )
    total_pas_nc = _redondear(
        e.creditos_hipotecarios + e.creditos_consumo + e.creditos_personales
        + e.creditos_prendarios + e.creditos_comerciales
    )

    izq: list[tuple] = [
        ("Activos", ""),
        ("Corrientes", ""),
        ("Efectivo y Equivalentes de Efectivo", e.efectivo),
        ("Cuentas por Cobrar", e.cuentas_por_cobrar),
        ("Inventarios", e.inventarios),
        ("Total Corrientes", total_act_corr),
        ("", ""),
        ("No Corrientes", ""),
        ("Propiedad Planta y Equipos", ""),
        ("Bienes Inmuebles", e.bienes_inmuebles),
        ("Mobiliario y Equipos", e.mobiliario_equipos),
        ("Vehiculos", e.vehiculos),
        ("(-) Depreciacion Acumulada", e.depreciacion_acumulada),
        ("Total No Corrientes", total_act_nc),
    ]
    der: list[tuple] = [
        ("Pasivos", ""),
        ("Corrientes", ""),
        ("Tarjetas de Credito", e.tarjetas_credito),
        ("Proveedores", e.proveedores),
        ("Impuestos por Pagar", e.impuestos_por_pagar),
        ("Gastos Acumulados por pagar", e.gastos_acumulados),
        ("Total Corrientes", total_pas_corr),
        ("", ""),
        ("No Corrientes", ""),
        ("Creditos Hipotecarios", e.creditos_hipotecarios),
        ("Creditos Consumo", e.creditos_consumo),
        ("Creditos Personales", e.creditos_personales),
        ("Creditos Prendarios", e.creditos_prendarios),
        ("Creditos Comerciales", e.creditos_comerciales),
        ("Total No Corrientes", total_pas_nc),
        ("Total Pasivos", e.total_pasivos),
        ("", ""),
        ("Patrimonio", ""),
        ("Capital", e.capital),
        ("Resultados Acumulados", e.resultados_acumulados),
        ("Resultados del Ejercicio", e.resultados_ejercicio),
    ]
    # Capital ya viene NETO de retiros (presentacion del CPA): sin linea aparte.
    der.append(("Total Patrimonio", e.total_patrimonio))

    # Emparejar alturas y cerrar con los totales generales en la misma fila.
    n = max(len(izq), len(der))
    izq += [("", "")] * (n - len(izq))
    der += [("", "")] * (n - len(der))
    izq.append(("Total Activos", e.total_activos))
    der.append(("Total Pasivo + Patrimonio", e.total_pasivo_patrimonio))

    rows = [[la, va, "", lp, vp] for (la, va), (lp, vp) in zip(izq, der)]
    # Columnas 'Unnamed' => el generador imprime el encabezado vacio (como el Excel real).
    return pd.DataFrame(rows, columns=["Unnamed: 0", "Unnamed: 1", "Unnamed: 2", "Unnamed: 3", "Unnamed: 4"])

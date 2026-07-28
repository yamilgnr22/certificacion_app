"""Trayectoria mensual generada de una deuda (banda / amortizacion).

Los reportes de credito (SIBOIF, TransUnion) dan el saldo AL CORTE, no el de
apertura ni la trayectoria del periodo. Sin esos datos las deudas quedaban
planas en el ESF mensual. Este modulo GENERA una trayectoria plausible y
determinista (mismo input + misma seed => mismo resultado), terminando SIEMPRE
en el saldo reportado (ancla dura del corte):

- Tarjetas (revolving): el saldo OSCILA en banda +-banda_pct alrededor del
  saldo reportado (como la caja/inventario en Tipo B).
- Creditos (amortizable): el saldo BAJA parejo (lineal) desde una apertura
  estimada hasta el saldo reportado. Si hay cuota, la baja mensual es la
  cuota de capital (cuota - interes); si no, se estima una amortizacion del
  periodo. NO es banda: es una curva descendente.

El motor consume estos saldos como `saldos_mensuales` (misma via que una
tarjeta con saldo variable dado a mano). El CPA puede sobreescribir dando
`saldos_mensuales` explicito.
"""

from __future__ import annotations

import random

# Fraccion del saldo que se asume amortizada en TODO el periodo cuando no hay
# cuota (apertura estimada = reportado * (1 + PCT)). ~15% en 6 meses.
_AMORT_PCT_SIN_CUOTA = 0.15


def _redondear(x: float) -> float:
    return round(float(x), 0)


def _oscilacion(seed: str, n: int, banda_pct: float) -> list[float]:
    """Camino suave de fracciones dentro de la banda (mismo patron que
    motor/tipo_b._trayectoria): |osc| <= 0.85*tol, paso <= 0.35*tol."""
    tol = banda_pct / 100.0
    rng = random.Random(seed)
    max_amp = 0.85 * tol
    step = 0.35 * tol
    osc = [rng.uniform(-0.5 * tol, 0.5 * tol)]
    for _ in range(1, n):
        osc.append(max(-max_amp, min(max_amp, osc[-1] + rng.uniform(-step, step))))
    return osc


def trayectoria_revolving(
    saldo_reportado: float, meses: list[str], banda_pct: float, seed: str
) -> dict[str, float]:
    """Banda oscilante alrededor del saldo reportado; ultimo mes = reportado."""
    n = len(meses)
    if n == 0:
        return {}
    osc = _oscilacion(seed, n, banda_pct)
    saldos: dict[str, float] = {}
    for t, mes in enumerate(meses):
        if t == n - 1:
            saldos[mes] = saldo_reportado  # ancla dura EXACTA (invariante #1)
        else:
            saldos[mes] = max(0.0, _redondear(saldo_reportado * (1 + osc[t])))
    return saldos


def trayectoria_con_ancla(
    inicial: float, final: float, meses: list[str], banda_pct: float, seed: str
) -> dict[str, float]:
    """Cuenta que OSCILA en banda alrededor de la tendencia inicial->final,
    terminando en el saldo final (ancla dura del corte).

    La usan el INVENTARIO y PROVEEDORES: ambos estan atados a la caja via las
    compras (el inventario sube al comprar; proveedores sube por lo comprado
    y no pagado). Por eso la banda default es mas conservadora que la de
    tarjetas: una oscilacion amplia arrastra el efectivo."""
    n = len(meses)
    if n == 0:
        return {}
    if n == 1:
        return {meses[0]: final}
    osc = _oscilacion(seed, n, banda_pct)
    saldos: dict[str, float] = {}
    for t, mes in enumerate(meses):
        if t == n - 1:
            saldos[mes] = final  # ancla dura EXACTA (pega con el saldo final)
        else:
            # Tendencia lineal inicial -> final, con la oscilacion encima.
            base = inicial + (final - inicial) * (t + 1) / n
            saldos[mes] = max(0.0, _redondear(base * (1 + osc[t])))
    return saldos


def trayectoria_credito_nuevo(
    valor_inicial: float,
    saldo_reportado: float,
    meses: list[str],
    mes_otorgamiento: str,
    ) -> dict[str, float]:
    """Credito OTORGADO DENTRO del periodo certificado.

    Antes del desembolso el credito NO EXISTE (saldo 0, no contamina el
    balance de apertura); el mes del otorgamiento aparece con el monto
    desembolsado (valor_inicial) y de ahi amortiza hasta anclar en el saldo
    reportado al corte. El aumento del pasivo el mes del desembolso lo toma
    Mov como financiamiento: entra efectivo, que es plata que el banco
    deposito al cliente."""
    n = len(meses)
    if n == 0:
        return {}
    idx = next((i for i, m in enumerate(meses) if m >= mes_otorgamiento), None)
    if idx is None:  # otorgado despues del corte (filtrar_por_ventana ya lo excluye)
        return {m: 0.0 for m in meses}

    monto = valor_inicial if valor_inicial > 0 else saldo_reportado
    ultimo = n - 1
    tramo = ultimo - idx  # meses entre el desembolso y el corte
    saldos: dict[str, float] = {}
    for i, mes in enumerate(meses):
        if i < idx:
            saldos[mes] = 0.0            # aun no existia
        elif i == ultimo:
            saldos[mes] = saldo_reportado  # ancla dura EXACTA del corte
        elif i == idx:
            saldos[mes] = _redondear(monto)  # desembolso
        else:
            # Amortiza parejo entre el desembolso y el saldo del corte.
            saldos[mes] = _redondear(monto + (saldo_reportado - monto) * (i - idx) / tramo)
    return saldos


def trayectoria_amortizable(
    saldo_reportado: float,
    meses: list[str],
    cuota: float = 0.0,
    interes_mensual: float = 0.0,
) -> dict[str, float]:
    """Baja lineal desde una apertura estimada hasta el saldo reportado.

    Con cuota: la baja mensual es la cuota de capital (cuota - interes). Sin
    cuota: apertura estimada = reportado * (1 + _AMORT_PCT_SIN_CUOTA)."""
    n = len(meses)
    if n == 0:
        return {}
    if n == 1:
        return {meses[0]: _redondear(saldo_reportado)}

    if cuota and cuota > 0:
        cuota_capital = max(0.0, cuota - max(0.0, interes_mensual))
    else:
        # Apertura estimada -> capital que baja parejo cada mes.
        cuota_capital = saldo_reportado * _AMORT_PCT_SIN_CUOTA / (n - 1)

    saldos: dict[str, float] = {}
    for t, mes in enumerate(meses):
        if t == n - 1:
            saldos[mes] = saldo_reportado  # ancla dura EXACTA (invariante #1)
        else:
            # saldo del mes = reportado + capital que aun falta amortizar
            saldos[mes] = _redondear(saldo_reportado + cuota_capital * (n - 1 - t))
    return saldos

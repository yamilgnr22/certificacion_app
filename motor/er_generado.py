"""ER modo 'generado': construye ER_LineaMes desde base + bandas centradas.

Es un PRE-PROCESADOR de inputs, no parte del motor: produce la misma
list[ER_LineaMes] que el modo manual y el resto del motor (Mov, ESF, deuda,
invariantes) no sabe de que modo vino. NO confundir con el simulador v1:
aqui el azar solo llena ingresos y costo de ventas del ER de entrada, de
forma acotada, centrada y reproducible por seed.

Reglas criticas (confirmadas con el usuario):
1. Oscilacion INDEPENDIENTE por mes SOBRE LA BASE FIJA (nunca camino
   aleatorio: no se compone sobre el mes anterior).
2. Banda centrada sin sesgo, pero SIN forzar el promedio a la base exacta:
   se resta el promedio muestral de los offsets y se reintroduce un residuo
   pequeno (7.5% de la banda => ~±1.5% con banda 20) para que el promedio
   caiga natural cerca de la base. Un promedio clavado en la base se ve
   artificial.
3. Seed fija => mismo input produce siempre el mismo ER (auditable). Si no
   se da, se deriva de cedula|mes_inicial|mes_final|er.
4. Costo = % sobre la venta YA generada del mes, con su propia banda
   (default 5%: el costo es mas estable que la venta). Estructuralmente
   nunca puede superar la venta: se valida en el input (bloqueante).

Gastos operativos: columna fija aplicada a todos los meses + overrides
puntuales por mes (p.ej. DGI trimestral). Claves restringidas a los 9
operativos: la depreciacion viene como parametro aparte (decision #5,
recibida ya calculada) y los gastos financieros salen de los planes de
deuda (invariante #6), nunca de aqui.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Mapping

from motor.amortizacion import _meses_del_periodo
from motor.inputs import ER_LineaMes, PeriodoSpec


# Los 9 gastos operativos configurables (== campos de ER_LineaMes menos
# ingresos/costo_ventas/gasto_depreciacion).
GASTOS_PERMITIDOS = {
    "sueldos_salarios", "servicios_publicos", "alcaldia_dgi", "combustible",
    "publicidad", "mantenimientos", "renta", "seguros", "otros_gastos",
}

# Fraccion de la banda que se reintroduce como residuo del promedio
# (banda 20% -> promedio de la serie a ±1.5% de la base).
_RESIDUO_FRAC = 0.075
# Recorte de cada offset para que el re-centrado no saque a nadie de la banda.
_AMPLITUD_FRAC = 0.95


@dataclass(frozen=True)
class ERGeneradoParams:
    ingreso_base: float
    costo_pct_sobre_venta: float
    banda_ingreso_pct: float = 20.0
    banda_costo_pct: float = 5.0
    seed: str = ""
    gasto_depreciacion_mensual: float = 0.0
    gastos_fijos: Mapping[str, float] = field(default_factory=dict)
    gastos_overrides: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ingreso_base <= 0:
            raise ValueError(f"ingreso_base debe ser > 0, no {self.ingreso_base}")
        if not (0 <= self.banda_ingreso_pct <= 50):
            raise ValueError(f"banda_ingreso_pct debe estar en [0, 50], no {self.banda_ingreso_pct}")
        if not (0 <= self.banda_costo_pct <= 50):
            raise ValueError(f"banda_costo_pct debe estar en [0, 50], no {self.banda_costo_pct}")
        if not (0 < self.costo_pct_sobre_venta < 100):
            raise ValueError(
                f"costo_pct_sobre_venta debe estar en (0, 100), no {self.costo_pct_sobre_venta}"
            )
        tope = self.costo_pct_sobre_venta * (1 + self.banda_costo_pct / 100.0)
        if tope >= 100:
            raise ValueError(
                f"costo_pct_sobre_venta ({self.costo_pct_sobre_venta}%) con banda "
                f"±{self.banda_costo_pct}% puede llegar a {tope:.1f}% de la venta: "
                "el costo superaria la venta. Baja el porcentaje o la banda."
            )
        if self.gasto_depreciacion_mensual < 0:
            raise ValueError("gasto_depreciacion_mensual no puede ser negativo")
        self._validar_claves_gastos(dict(self.gastos_fijos), "gastos_fijos")
        for mes, gastos in dict(self.gastos_overrides).items():
            self._validar_claves_gastos(dict(gastos), f"gastos_overrides[{mes}]")

    @staticmethod
    def _validar_claves_gastos(gastos: Mapping[str, float], donde: str) -> None:
        invalidas = set(gastos) - GASTOS_PERMITIDOS
        if invalidas:
            detalle = ""
            if "gasto_depreciacion" in invalidas:
                detalle = " La depreciacion va en 'gasto_depreciacion_mensual' (input, decision #5)."
            if "gastos_financieros" in invalidas:
                detalle += " Los gastos financieros salen de los planes de deuda (invariante #6)."
            raise ValueError(
                f"Claves invalidas en {donde}: {sorted(invalidas)}. "
                f"Permitidas: {sorted(GASTOS_PERMITIDOS)}.{detalle}"
            )


def _offsets_centrados(seed: str, n: int, banda_pct: float) -> list[float]:
    """Offsets independientes por mes, centrados sin sesgo (regla 1 y 2).

    uniforme(-b, +b) por mes -> se resta el promedio muestral -> se suma un
    residuo pequeno (±7.5% de la banda) para que el promedio de la serie
    caiga cerca de la base sin quedar clavado en ella. Recorte a 95% de la
    banda para que el re-centrado no saque ningun mes de rango.
    """
    if n <= 0:
        return []
    b = banda_pct / 100.0
    if b == 0:
        return [0.0] * n
    rng = random.Random(seed)
    crudos = [rng.uniform(-b, b) for _ in range(n)]
    media = sum(crudos) / n
    residuo = rng.uniform(-_RESIDUO_FRAC * b, _RESIDUO_FRAC * b)
    tope = _AMPLITUD_FRAC * b
    return [max(-tope, min(tope, x - media + residuo)) for x in crudos]


def generar_er_mensual(
    params: ERGeneradoParams,
    periodo: PeriodoSpec,
    *,
    cedula: str = "",
) -> list[ER_LineaMes]:
    meses = _meses_del_periodo(periodo)
    meses_set = set(meses)
    for mes in params.gastos_overrides:
        if mes not in meses_set:
            raise ValueError(
                f"gastos_overrides tiene el mes '{mes}' fuera del periodo "
                f"{periodo.mes_inicial}..{periodo.mes_final}"
            )

    seed = params.seed or f"{cedula}|{periodo.mes_inicial}|{periodo.mes_final}|er"
    osc_ing = _offsets_centrados(seed + "|ing", len(meses), params.banda_ingreso_pct)
    osc_cos = _offsets_centrados(seed + "|cos", len(meses), params.banda_costo_pct)

    lineas: list[ER_LineaMes] = []
    for i, mes in enumerate(meses):
        ingreso = round(params.ingreso_base * (1.0 + osc_ing[i]), 2)
        tasa = (params.costo_pct_sobre_venta / 100.0) * (1.0 + osc_cos[i])
        costo = round(ingreso * tasa, 2)
        if costo >= ingreso:  # cinturon: estructuralmente imposible por __post_init__
            raise ValueError(f"Mes {mes}: costo generado ({costo}) >= venta ({ingreso})")
        gastos = dict(params.gastos_fijos)
        gastos.update(dict(params.gastos_overrides.get(mes, {})))
        lineas.append(ER_LineaMes(
            mes=mes,
            ingresos=ingreso,
            costo_ventas=costo,
            gasto_depreciacion=round(params.gasto_depreciacion_mensual, 2),
            **{k: round(float(gastos.get(k, 0.0)), 2) for k in GASTOS_PERMITIDOS},
        ))
    return lineas

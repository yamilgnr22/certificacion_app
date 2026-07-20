"""Depreciacion por componente de PPE (linea recta, mensual).

En lugar de teclear el gasto de depreciacion como un numero suelto, el CPA
declara por cada componente de PPE (bienes inmuebles, mobiliario y equipos,
vehiculos) que porcion del valor se deprecia y en cuantos anios:

    gasto mensual = valor * (pct_depreciable/100) / (vida_util_anios * 12)

El valor base de cada componente es su saldo INICIAL en el ESF (costo de
adquisicion; PPE es constante en el periodo, sin capex). El total mensual
reemplaza al gasto de depreciacion manual del ER (ver motor/json_io).

Ejemplo: propiedad de 1,000,000 con pct_depreciable=20 y vida de 10 anios
=> base 200,000 => 1,666.67 al mes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

CUENTAS_PPE = ("bienes_inmuebles", "mobiliario_equipos", "vehiculos")


def _redondear(v: float) -> float:
    return round(v + 0.0, 2)


@dataclass(frozen=True)
class ComponenteDepreciacion:
    cuenta: str
    valor: float  # saldo inicial de la cuenta (costo)
    pct_depreciable: float  # porcion del valor que se deprecia, (0, 100]
    vida_util_anios: float  # > 0

    def __post_init__(self):
        if self.cuenta not in CUENTAS_PPE:
            raise ValueError(
                f"Cuenta PPE invalida {self.cuenta!r}; validas: {', '.join(CUENTAS_PPE)}"
            )
        if not (0 < self.pct_depreciable <= 100):
            raise ValueError(
                f"{self.cuenta}: pct_depreciable debe estar en (0, 100], vino {self.pct_depreciable}"
            )
        if self.vida_util_anios <= 0:
            raise ValueError(
                f"{self.cuenta}: vida_util_anios debe ser > 0, vino {self.vida_util_anios}"
            )
        if self.valor < 0:
            raise ValueError(f"{self.cuenta}: el valor no puede ser negativo ({self.valor})")

    @property
    def base_depreciable(self) -> float:
        return _redondear(self.valor * self.pct_depreciable / 100.0)

    @property
    def gasto_mensual(self) -> float:
        return _redondear(self.base_depreciable / (self.vida_util_anios * 12.0))


@dataclass(frozen=True)
class DepreciacionPPE:
    componentes: tuple[ComponenteDepreciacion, ...]

    @property
    def gasto_mensual_total(self) -> float:
        return _redondear(sum(c.gasto_mensual for c in self.componentes))


def calcular_depreciacion(spec: Mapping[str, Any], saldos_iniciales) -> DepreciacionPPE:
    """Construye la depreciacion desde el bloque JSON `depreciacion_ppe`.

    spec: {"bienes_inmuebles": {"pct_depreciable": 20, "vida_util_anios": 10}, ...}
    saldos_iniciales: ESF_Saldos (o cualquier objeto/mapping con las cuentas PPE).

    Componentes sin entrada en spec no se deprecian. Un componente declarado
    con saldo inicial 0 aporta gasto 0 (no es error: el CPA puede dejar la
    config lista antes de cargar saldos).
    """
    if not isinstance(spec, Mapping):
        raise ValueError("depreciacion_ppe debe ser un objeto {cuenta: {pct_depreciable, vida_util_anios}}")

    def _saldo(cuenta: str) -> float:
        if isinstance(saldos_iniciales, Mapping):
            return float(saldos_iniciales.get(cuenta, 0.0) or 0.0)
        return float(getattr(saldos_iniciales, cuenta, 0.0) or 0.0)

    desconocidas = set(spec) - set(CUENTAS_PPE)
    if desconocidas:
        raise ValueError(
            f"depreciacion_ppe: cuentas invalidas {sorted(desconocidas)}; "
            f"validas: {', '.join(CUENTAS_PPE)}"
        )

    componentes = []
    for cuenta in CUENTAS_PPE:  # orden estable, independiente del JSON
        cfg = spec.get(cuenta)
        if not cfg:
            continue
        componentes.append(
            ComponenteDepreciacion(
                cuenta=cuenta,
                valor=_saldo(cuenta),
                pct_depreciable=float(cfg.get("pct_depreciable", 0) or 0),
                vida_util_anios=float(cfg.get("vida_util_anios", 0) or 0),
            )
        )
    return DepreciacionPPE(componentes=tuple(componentes))

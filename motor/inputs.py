"""Dataclasses tipados de entrada/salida del motor.

Todo lo que entra al motor es un objeto inmutable; las funciones del motor
son puras (no mutan los inputs ni leen estado global). Las cifras son
floats en NIO salvo que se indique 'usd' en el nombre.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional


Estrategia = Literal["amortizable", "bullet", "revolving"]
Moneda = Literal["NIO", "USD"]
TipoRegimen = Literal["A", "B"]


# ---------------------------------------------------------------- Datos cliente

@dataclass(frozen=True)
class DatosCliente:
    nombre_completo: str
    cedula: str
    domicilio: str
    contacto: str
    regimen: str
    matricula: str
    direccion_negocio: str
    giro: str
    antiguedad: str
    empleados: int
    estado_civil: str
    profesion: str
    sexo: str
    banco: str
    fecha_certificacion: date
    primer_apellido: str = ""

    def __post_init__(self) -> None:
        if not self.primer_apellido:
            partes = self.nombre_completo.strip().split()
            apellido = partes[-2] if len(partes) >= 2 else (partes[-1] if partes else "")
            object.__setattr__(self, "primer_apellido", apellido)


# ----------------------------------------------------------------- PeriodoSpec

@dataclass(frozen=True)
class PeriodoSpec:
    """Define el periodo certificado.

    mes_inicial y mes_final en formato 'YYYY-MM'. tasa_cambio NIO por USD
    es fija para todo el periodo (decision #10 C: T/C fijo del periodo).
    """

    tipo: TipoRegimen
    mes_inicial: str
    mes_final: str
    tasa_cambio: float

    def __post_init__(self) -> None:
        for campo, valor in (("mes_inicial", self.mes_inicial), ("mes_final", self.mes_final)):
            if len(valor) != 7 or valor[4] != "-":
                raise ValueError(f"{campo} debe ser 'YYYY-MM', no '{valor}'")
        if self.mes_final < self.mes_inicial:
            raise ValueError(f"mes_final ({self.mes_final}) anterior a mes_inicial ({self.mes_inicial})")
        if self.tasa_cambio <= 0:
            raise ValueError(f"tasa_cambio debe ser > 0, no {self.tasa_cambio}")


# ------------------------------------------------------------------ ER mensual

@dataclass(frozen=True)
class ER_LineaMes:
    """Una columna del ER para un mes. Todo en NIO.

    Gastos Financieros NO se incluyen aqui: los calcula motor/amortizacion
    a partir de los planes y los suma al ER.

    Gasto por Depreciacion sí es input (decision #5 default: ya calculado).
    """

    mes: str
    ingresos: float
    costo_ventas: float = 0.0
    sueldos_salarios: float = 0.0
    servicios_publicos: float = 0.0
    alcaldia_dgi: float = 0.0
    combustible: float = 0.0
    publicidad: float = 0.0
    mantenimientos: float = 0.0
    renta: float = 0.0
    seguros: float = 0.0
    otros_gastos: float = 0.0
    gasto_depreciacion: float = 0.0


# ----------------------------------------------------------------- ESF saldos

@dataclass(frozen=True)
class ESF_Saldos:
    """Saldos en NIO de las cuentas del ESF.

    capital es Optional: si el cliente lo envia, el motor lo valida contra
    Activos0 - Pasivos0 (bloqueante si no cuadra, decision B). Si lo omite,
    el motor lo calcula.
    """

    efectivo: float = 0.0
    cuentas_por_cobrar: float = 0.0
    inventarios: float = 0.0
    bienes_inmuebles: float = 0.0
    mobiliario_equipos: float = 0.0
    vehiculos: float = 0.0
    depreciacion_acumulada: float = 0.0
    tarjetas_credito: float = 0.0
    proveedores: float = 0.0
    impuestos_por_pagar: float = 0.0
    gastos_acumulados: float = 0.0
    creditos_hipotecarios: float = 0.0
    creditos_consumo: float = 0.0
    creditos_personales: float = 0.0
    creditos_prendarios: float = 0.0
    creditos_comerciales: float = 0.0
    resultados_acumulados: float = 0.0
    capital: Optional[float] = None

    def total_activos(self) -> float:
        return (
            self.efectivo
            + self.cuentas_por_cobrar
            + self.inventarios
            + self.bienes_inmuebles
            + self.mobiliario_equipos
            + self.vehiculos
            + self.depreciacion_acumulada
        )

    def total_pasivos(self) -> float:
        return (
            self.tarjetas_credito
            + self.proveedores
            + self.impuestos_por_pagar
            + self.gastos_acumulados
            + self.creditos_hipotecarios
            + self.creditos_consumo
            + self.creditos_personales
            + self.creditos_prendarios
            + self.creditos_comerciales
        )


# ------------------------------------------------------------------ Deudas

CUENTAS_PASIVO_VALIDAS = {
    "tarjetas_credito",
    "creditos_hipotecarios",
    "creditos_consumo",
    "creditos_personales",
    "creditos_prendarios",
    "creditos_comerciales",
}


@dataclass(frozen=True)
class DeudaInput:
    """Una linea del reporte TransUnion ya parseada a JSON.

    saldo_apertura: saldo del credito al INICIO del periodo certificado, en
    moneda original. Es el punto de partida de la amortizacion dentro del
    periodo (decision usuario: el plan arranca desde el saldo que doy yo,
    no desde valor_inicial amortizado). Si es None (p.ej. Tipo B / analisis
    historico), el motor reconstruye la apertura amortizando desde
    fecha_otorgamiento.
    """

    numero: str
    entidad: str
    tipo_credito: str
    estrategia: Estrategia
    moneda: Moneda
    valor_inicial: float
    saldo_reportado: float
    cuota: float
    fecha_otorgamiento: date
    fecha_actualizado: date
    fecha_vencimiento: Optional[date] = None
    tasa_mensual: Optional[float] = None
    saldo_apertura: Optional[float] = None
    # incluir_en_er=False: el credito es soporte documental (como los planes
    # 6532/8797 de Gloria); NO impacta ER, Mov ni ESF. Se resuelve solo para
    # anexos. Default True (deuda activa, como los creditos de Thelma).
    incluir_en_er: bool = True
    # saldos_mensuales: para revolving con saldo variable (tarjetas), mapa
    # 'YYYY-MM' -> saldo en moneda original. Si None, el revolving usa saldo
    # constante = saldo_apertura (o saldo_reportado).
    saldos_mensuales: Optional[dict] = None
    notas: str = ""


# --------------------------------------------------------- Plan resuelto

@dataclass(frozen=True)
class CuotaPlan:
    """Una fila de la tabla de amortizacion mensual. Cifras en NIO."""

    no_cuota: int
    mes: str
    saldo_inicial_nio: float
    cuota_nio: float
    interes_nio: float
    abono_capital_nio: float
    abono_extraordinario_nio: float
    saldo_final_nio: float


@dataclass(frozen=True)
class PlanResuelto:
    """Output de motor/amortizacion para un credito.

    cuenta_esf: nombre del atributo en ESF_Saldos donde vive el saldo del pasivo.
    saldo_apertura_nio: saldo del credito al inicio del periodo, en NIO. Es lo
    que la cuenta del ESF debe reflejar en el mes 0 para que el balance cuadre.
    """

    deuda: DeudaInput
    cuenta_esf: str
    cuotas: list[CuotaPlan]
    tasa_mensual_inferida: float
    saldo_apertura_nio: float = 0.0
    alerta: Optional[str] = None

    def saldo_final_corte_nio(self) -> float:
        return self.cuotas[-1].saldo_final_nio if self.cuotas else 0.0

    def interes_del_mes_nio(self, mes: str) -> float:
        for c in self.cuotas:
            if c.mes == mes:
                return c.interes_nio
        return 0.0

    def abono_total_del_mes_nio(self, mes: str) -> float:
        for c in self.cuotas:
            if c.mes == mes:
                return c.abono_capital_nio + c.abono_extraordinario_nio
        return 0.0


# --------------------------------------------------------- Inputs por regimen

CAMPOS_BANDA = (
    "tarjetas_pct", "creditos_pct", "inventario_pct", "proveedores_pct", "cxc_pct",
)


@dataclass(frozen=True)
class Bandas:
    """Amplitud de la oscilacion mensual de cada concepto, en %.

    Las cuentas no quedan planas mes a mes: oscilan dentro de su banda y
    anclan en el saldo del corte. Defaults conservadores salvo tarjetas (que
    por naturaleza se consumen y pagan con mas variacion); inventario,
    proveedores y creditos mueven la caja, asi que una banda amplia puede
    dejar el efectivo corto en el mes de mayor compra o pago."""

    tarjetas_pct: float = 20.0      # tarjetas del reporte de deuda (revolving)
    creditos_pct: float = 10.0      # cuentas de credito declaradas sin plan
    inventario_pct: float = 10.0    # inventario (Tipo A)
    proveedores_pct: float = 10.0   # proveedores
    cxc_pct: float = 10.0           # cuentas por cobrar clientes

    def __post_init__(self) -> None:
        for campo in CAMPOS_BANDA:
            v = getattr(self, campo)
            if not (0 <= v <= 50):
                raise ValueError(f"{campo} debe estar en [0, 50], no {v}")


@dataclass(frozen=True)
class UtilidadObjetivo:
    """Utilidad neta promedio mensual que el CPA espera del cliente.

    Es un PISO, no una banda: quedar por encima nunca es un problema (el
    negocio gano mas de lo previsto), quedar por debajo si, porque el
    documento no va a sostener lo que el cliente dice que factura.

    No cambia NINGUNA cifra: el motor calcula el ER con los parametros dados
    y despues mide contra este piso.

    monto 0 = sin objetivo (no se mide nada, comportamiento de siempre).
    La moneda es la del monto que se escribe; la comparacion se hace en NIO
    con el tipo de cambio del periodo.
    """

    monto: float = 0.0
    moneda: Moneda = "NIO"

    def __post_init__(self) -> None:
        if self.monto < 0:
            raise ValueError("La utilidad objetivo no puede ser negativa")

    def objetivo_nio(self, tasa_cambio: float) -> float:
        return self.monto * (tasa_cambio if self.moneda == "USD" else 1.0)

    @property
    def activo(self) -> bool:
        return self.monto > 0


@dataclass(frozen=True)
class Minimos:
    """Pisos y topes que el motor no puede violar al cuadrar el periodo.

    Hermanos de Bandas: la banda dice CUANTO oscila una cuenta, el minimo
    dice hasta donde se la puede empujar cuando hay que salvar la caja.

    - caja: piso del efectivo. 0 = solo se garantiza que no quede negativo.
    - inventario: sin el, recortar compras para salvar la caja puede dejar el
      stock en un nivel que ningun negocio real sostiene.
    - aporte_maximo: tope del ultimo recurso (plata que mete el dueño). None
      = sin tope; 0 = prohibido, el periodo se declara infactible y se
      informa cuanto falta en vez de inventar un aporte."""

    caja: float = 0.0
    inventario: float = 0.0
    aporte_maximo: float | None = None

    def __post_init__(self) -> None:
        for campo in ("caja", "inventario"):
            if getattr(self, campo) < 0:
                raise ValueError(f"minimo {campo} no puede ser negativo")
        if self.aporte_maximo is not None and self.aporte_maximo < 0:
            raise ValueError("aporte_maximo no puede ser negativo")


@dataclass(frozen=True)
class InputsTipoA:
    """Inputs para certificacion Tipo A (~6 meses, balance final ancla dura)."""

    periodo: PeriodoSpec
    datos: DatosCliente
    er_mensual: list[ER_LineaMes]
    saldos_iniciales: ESF_Saldos
    saldos_finales: ESF_Saldos
    deudas: list[DeudaInput] = field(default_factory=list)
    bandas: Bandas = field(default_factory=Bandas)
    minimos: Minimos = field(default_factory=Minimos)
    utilidad_objetivo: UtilidadObjetivo = field(default_factory=UtilidadObjetivo)

    def __post_init__(self) -> None:
        if self.periodo.tipo != "A":
            raise ValueError(f"InputsTipoA recibio periodo tipo {self.periodo.tipo!r}")


@dataclass(frozen=True)
class CuentaObjetivo:
    """Banda de oscilacion de una cuenta en Tipo B (decision #1: V1 solo
    'efectivo' e 'inventarios', tolerancia default +-20% configurable)."""

    cuenta: str  # 'efectivo' | 'inventarios'
    objetivo: float
    tolerancia_pct: float = 20.0

    def __post_init__(self) -> None:
        if self.cuenta not in {"efectivo", "inventarios"}:
            raise ValueError(
                f"cuenta objetivo invalida {self.cuenta!r}; V1 soporta 'efectivo' e 'inventarios'"
            )
        if self.objetivo <= 0:
            raise ValueError(f"objetivo de {self.cuenta} debe ser > 0, no {self.objetivo}")
        if not (0 < self.tolerancia_pct <= 100):
            raise ValueError(f"tolerancia_pct debe estar en (0, 100], no {self.tolerancia_pct}")

    def banda(self) -> tuple[float, float]:
        tol = self.tolerancia_pct / 100.0
        return (self.objetivo * (1.0 - tol), self.objetivo * (1.0 + tol))


@dataclass(frozen=True)
class InputsTipoB:
    """Inputs para certificacion Tipo B (12 meses, caja oscilante en banda).

    No hay saldos finales: el ESF de corte es el resultado natural del modelo.
    El excedente de caja sobre la trayectoria objetivo sale como retiros de
    patrimonio (contra Resultados Acumulados); nunca se inyecta efectivo.
    seed: hace la oscilacion reproducible; si va vacio se deriva de
    cedula|mes_inicial|mes_final.
    """

    periodo: PeriodoSpec
    datos: DatosCliente
    er_mensual: list[ER_LineaMes]
    saldos_iniciales: ESF_Saldos
    cuentas_objetivo: list[CuentaObjetivo]
    deudas: list[DeudaInput] = field(default_factory=list)
    seed: str = ""
    bandas: Bandas = field(default_factory=Bandas)
    minimos: Minimos = field(default_factory=Minimos)
    utilidad_objetivo: UtilidadObjetivo = field(default_factory=UtilidadObjetivo)

    def __post_init__(self) -> None:
        if self.periodo.tipo != "B":
            raise ValueError(f"InputsTipoB recibio periodo tipo {self.periodo.tipo!r}")
        y1, m1 = int(self.periodo.mes_inicial[:4]), int(self.periodo.mes_inicial[5:7])
        y2, m2 = int(self.periodo.mes_final[:4]), int(self.periodo.mes_final[5:7])
        n = (y2 - y1) * 12 + (m2 - m1) + 1
        if n > 12:
            raise ValueError(f"Tipo B V1 soporta hasta 12 meses (pediste {n}); multi-anio es Fase 4")
        cuentas = [c.cuenta for c in self.cuentas_objetivo]
        if len(set(cuentas)) != len(cuentas):
            raise ValueError("cuentas_objetivo duplicadas")
        if "efectivo" not in cuentas:
            raise ValueError(
                "Tipo B requiere una cuenta objetivo 'efectivo' (la caja oscila alrededor de un objetivo)"
            )

    def objetivo(self, cuenta: str) -> Optional[CuentaObjetivo]:
        for c in self.cuentas_objetivo:
            if c.cuenta == cuenta:
                return c
        return None

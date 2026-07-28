"""Solver de caja: corrige lo MINIMO indispensable para que el efectivo
nunca baje del piso, respetando anclas, bandas y minimos.

Por que un solver y no una cascada de reglas
--------------------------------------------
Una cascada agota las palancas en un orden fijo (primero inventario, luego
proveedores...), y ese orden es arbitrario. Aca el reparto es optimo: cada
palanca aporta en proporcion a su holgura, de modo que ninguna se deforma
mas de lo necesario.

Que optimiza
------------
NO "acercarse al centro de la banda": eso daria lineas planas, que es justo
lo contrario del realismo que el motor construye. El generador determinista
produce la trayectoria DESEADA (con su oscilacion) y el solver la deforma lo
menos posible:

    minimizar  SUM (desvio_i / escala_i)^2
    sujeto a   SUM desvio_i = deficit del mes
               0 <= desvio_i <= capacidad_i   (minimos, topes, anclas)

La escala es el ancho de banda de cada cuenta: asi un desvio "de una banda"
cuesta lo mismo en todas, y la cuenta con mas holgura absorbe mas. El
objetivo es estrictamente convexo => la solucion es UNICA, o sea el
resultado es reproducible (mismo input, mismas cifras, siempre).

Independencia mes a mes
-----------------------
La caja del mes t es
    caja[t] = C[t] - inventario[t] + proveedores[t] - cxc[t]
donde C[t] junta todo lo que el solver no toca (flujo del ER, deuda, saldos
de apertura). Las variaciones telescopian: solo importa el NIVEL de cada
cuenta en t, no su historia. Por eso el problema se parte en un subproblema
chico e independiente por mes, y una sola pasada basta — sin iterar hasta
converger.

El aporte del propietario NO entra al reparto: es el ultimo recurso y se usa
solo por el faltante que las palancas operativas no alcanzaron a cubrir.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _redondear(x: float) -> float:
    # Cordobas enteros (ver motor/er._redondear).
    return round(float(x), 0)


@dataclass(frozen=True)
class Palanca:
    """Una cuenta que el solver puede mover para salvar la caja de un mes.

    signo: +1 si SUBIRLA mete efectivo (proveedores: comprar a credito),
           -1 si BAJARLA lo mete (inventario: comprar menos; CxC: cobrar).
    escala: ancho de banda en NIO; normaliza el costo del desvio. Cuanto mas
            ancha la banda de una cuenta, mas se la puede mover sin que se
            note.
    """

    nombre: str
    deseado: float
    signo: int
    escala: float
    minimo: float = 0.0
    maximo: float | None = None

    def capacidad(self) -> float:
        """Cuanto efectivo puede aportar esta palanca este mes."""
        if self.signo < 0:
            return max(0.0, self.deseado - self.minimo)
        if self.maximo is None:
            return float("inf")
        return max(0.0, self.maximo - self.deseado)


@dataclass(frozen=True)
class AjusteMes:
    mes: str
    deficit: float                      # cuanto faltaba para llegar al piso
    movimientos: dict[str, float]       # cuenta -> efectivo aportado
    aporte_propietario: float = 0.0     # ultimo recurso
    faltante: float = 0.0               # lo que no se pudo cubrir
    # cuenta -> (saldo deseado, saldo corregido), para explicar el ajuste en
    # los terminos del balance y no en "cuanto efectivo aporto".
    saldos: dict[str, tuple[float, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class ResultadoSolver:
    ajustes: list[AjusteMes] = field(default_factory=list)
    # Saldos corregidos: {cuenta: {mes: saldo}}. Solo las cuentas movidas.
    saldos: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def toco_algo(self) -> bool:
        return any(a.movimientos or a.aporte_propietario for a in self.ajustes)

    @property
    def faltante_total(self) -> float:
        return _redondear(sum(a.faltante for a in self.ajustes))

    @property
    def aporte_total(self) -> float:
        return _redondear(sum(a.aporte_propietario for a in self.ajustes))

    def resumen(self) -> list[str]:
        """Lineas legibles para el CPA: que movio el motor y por que."""
        out: list[str] = []
        for a in self.ajustes:
            if not (a.movimientos or a.aporte_propietario):
                continue
            piezas = [
                f"{cuenta} {deseado:,.0f} -> {corregido:,.0f}"
                for cuenta, (deseado, corregido) in sorted(a.saldos.items())
            ]
            if a.aporte_propietario:
                piezas.append(f"aporte del propietario {a.aporte_propietario:,.0f}")
            out.append(
                f"{a.mes}: faltaban {a.deficit:,.0f} para el piso de caja -> "
                + ", ".join(piezas)
            )
        return out


def repartir(deficit: float, palancas: list[Palanca]) -> tuple[dict[str, float], float]:
    """Reparto optimo del deficit entre las palancas (water-filling).

    Sin cotas, el optimo de minimizar SUM (c_i/e_i)^2 con SUM c_i = D reparte
    en proporcion a e_i^2. Con cotas, la palanca que se pasaria de su
    capacidad se fija en el tope y el resto se vuelve a repartir entre las
    libres. Converge en tantas vueltas como palancas haya.

    Devuelve (aporte por palanca, faltante no cubierto).
    """
    restante = _redondear(deficit)
    if restante <= 0 or not palancas:
        return {}, max(0.0, restante)

    fijas: dict[str, float] = {}
    libres = {p.nombre: p for p in palancas if p.capacidad() > 0}

    while libres and restante > 0:
        peso_total = sum(p.escala ** 2 for p in libres.values())
        if peso_total <= 0:  # todas sin banda: reparto parejo
            peso_total = float(len(libres))
            pesos = {n: 1.0 for n in libres}
        else:
            pesos = {n: p.escala ** 2 for n, p in libres.items()}

        excedidas = False
        for nombre, p in list(libres.items()):
            cuota = restante * pesos[nombre] / peso_total
            if cuota > p.capacidad():
                fijas[nombre] = p.capacidad()
                restante = _redondear(restante - p.capacidad())
                del libres[nombre]
                excedidas = True
                break  # recalcular el reparto con las que quedan
        if excedidas:
            continue

        # Nadie topa: el reparto proporcional es la solucion.
        for nombre, p in libres.items():
            fijas[nombre] = _redondear(restante * pesos[nombre] / peso_total)
        # El redondeo a enteros puede dejar +-1 cordoba: se lo carga la
        # palanca de mayor banda, que es la que menos lo acusa.
        sobra = _redondear(restante - sum(fijas[n] for n in libres))
        if sobra:
            mayor = max(libres.values(), key=lambda p: p.escala)
            fijas[mayor.nombre] = _redondear(fijas[mayor.nombre] + sobra)
        restante = 0.0
        libres.clear()

    return {k: v for k, v in fijas.items() if v}, max(0.0, _redondear(restante))


def resolver_caja(
    meses: list[str],
    caja_por_mes: dict[str, float],
    piso_caja: float,
    palancas_por_mes: dict[str, list[Palanca]],
    aporte_maximo: float | None = None,
    permite_aporte: bool = False,
) -> ResultadoSolver:
    """Ajusta las trayectorias para que ningun mes quede bajo el piso.

    caja_por_mes: efectivo que resulta de las trayectorias DESEADAS. Como la
    caja del mes depende solo del nivel de las cuentas en ese mes, corregir
    el nivel corrige la caja en la misma magnitud: una pasada alcanza.

    permite_aporte: solo Tipo B. En Tipo A el balance final es ancla dura, un
    aporte lo romperia; si las palancas operativas no alcanzan, se reporta
    faltante y validar lo convierte en error bloqueante.
    """
    ajustes: list[AjusteMes] = []
    saldos: dict[str, dict[str, float]] = {}
    aporte_usado = 0.0

    for mes in meses:
        caja = _redondear(caja_por_mes.get(mes, 0.0))
        deficit = _redondear(piso_caja - caja)
        if deficit <= 0:
            ajustes.append(AjusteMes(mes=mes, deficit=0.0, movimientos={}))
            continue

        palancas = palancas_por_mes.get(mes, [])
        movimientos, faltante = repartir(deficit, palancas)

        aporte = 0.0
        if faltante > 0 and permite_aporte:
            disponible = (
                float("inf") if aporte_maximo is None
                else max(0.0, aporte_maximo - aporte_usado)
            )
            aporte = _redondear(min(faltante, disponible))
            aporte_usado = _redondear(aporte_usado + aporte)
            faltante = _redondear(faltante - aporte)

        # Los movimientos se traducen a saldos: el signo dice para que lado.
        por_nombre = {p.nombre: p for p in palancas}
        detalle: dict[str, tuple[float, float]] = {}
        for nombre, aportado in movimientos.items():
            p = por_nombre[nombre]
            corregido = _redondear(p.deseado + p.signo * aportado)
            saldos.setdefault(nombre, {})[mes] = corregido
            detalle[nombre] = (_redondear(p.deseado), corregido)

        ajustes.append(AjusteMes(
            mes=mes,
            deficit=deficit,
            movimientos=movimientos,
            aporte_propietario=aporte,
            faltante=faltante,
            saldos=detalle,
        ))

    return ResultadoSolver(ajustes=ajustes, saldos=saldos)

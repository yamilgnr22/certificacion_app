"""Deserializa el JSON del endpoint a InputsTipoA y serializa el resultado.

Formato de entrada (POST /api/motor/v2/certificar):
{
  "periodo": {"tipo": "A", "mes_inicial": "2025-12", "mes_final": "2026-05", "tasa_cambio": 36.6243},
  "datos": {...campos de DatosCliente..., "fecha_certificacion": "2026-06-05"},
  "er_mensual": [{"mes": "2025-12", "ingresos": 373202, "costo_ventas": ..., ...}, ...],
  "saldos_iniciales": {...campos de ESF_Saldos...},
  "saldos_finales": {...campos de ESF_Saldos...},
  "deudas": [{...campos de DeudaInput..., "fecha_otorgamiento": "2024-01-01"}, ...]
}
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from motor.inputs import (
    CAMPOS_BANDA,
    Bandas,
    Minimos,
    UtilidadObjetivo,
    CuentaObjetivo,
    DatosCliente,
    DeudaInput,
    ER_LineaMes,
    ESF_Saldos,
    InputsTipoA,
    InputsTipoB,
    PeriodoSpec,
)


def _fecha(v: Any) -> date | None:
    """Acepta 'AAAA-MM-DD' o 'AAAA-MM' (se asume dia 01). Vacio => None.

    En las deudas la fecha exacta no cambia el resultado (el filtro de ventana
    y el plazo trabajan a nivel de mes), asi que dejamos entrar el formato de
    mes que el resto de la app usa, sin obligar a escribir el dia."""
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()[:10]
    if len(s) == 7 and s[4] == "-":  # 'AAAA-MM' -> 'AAAA-MM-01'
        s = s + "-01"
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(
            f"Fecha invalida '{v}'. Usa el formato AAAA-MM-DD (o AAAA-MM)."
        ) from exc


def _periodo(d: Mapping) -> PeriodoSpec:
    return PeriodoSpec(
        tipo=d.get("tipo", "A"),
        mes_inicial=str(d["mes_inicial"]),
        mes_final=str(d["mes_final"]),
        tasa_cambio=float(d["tasa_cambio"]),
    )


def _datos(d: Mapping) -> DatosCliente:
    return DatosCliente(
        nombre_completo=str(d.get("nombre_completo", "")),
        cedula=str(d.get("cedula", "")),
        domicilio=str(d.get("domicilio", "")),
        contacto=str(d.get("contacto", "")),
        regimen=str(d.get("regimen", "")),
        matricula=str(d.get("matricula", "")),
        direccion_negocio=str(d.get("direccion_negocio", "")),
        giro=str(d.get("giro", "")),
        antiguedad=str(d.get("antiguedad", "")),
        empleados=int(d.get("empleados", 0) or 0),
        estado_civil=str(d.get("estado_civil", "")),
        profesion=str(d.get("profesion", "")),
        sexo=str(d.get("sexo", "")),
        banco=str(d.get("banco", "")),
        fecha_certificacion=_fecha(d.get("fecha_certificacion")) or date.today(),
        primer_apellido=str(d.get("primer_apellido", "")),
    )


_ER_CAMPOS = {
    "ingresos", "costo_ventas", "sueldos_salarios", "servicios_publicos",
    "alcaldia_dgi", "combustible", "publicidad", "mantenimientos", "renta",
    "seguros", "otros_gastos", "gasto_depreciacion",
}


def _er_linea(d: Mapping) -> ER_LineaMes:
    kwargs = {k: float(d.get(k, 0.0) or 0.0) for k in _ER_CAMPOS}
    return ER_LineaMes(mes=str(d["mes"]), **kwargs)


_ESF_CAMPOS = {
    "efectivo", "cuentas_por_cobrar", "inventarios", "bienes_inmuebles",
    "mobiliario_equipos", "vehiculos", "depreciacion_acumulada", "tarjetas_credito",
    "proveedores", "impuestos_por_pagar", "gastos_acumulados", "creditos_hipotecarios",
    "creditos_consumo", "creditos_personales", "creditos_prendarios",
    "creditos_comerciales", "resultados_acumulados",
}


def _esf(d: Mapping | None) -> ESF_Saldos:
    d = d or {}
    kwargs = {k: float(d.get(k, 0.0) or 0.0) for k in _ESF_CAMPOS}
    if d.get("capital") is not None:
        kwargs["capital"] = float(d["capital"])
    return ESF_Saldos(**kwargs)


def _deuda(d: Mapping) -> DeudaInput:
    return DeudaInput(
        numero=str(d["numero"]),
        entidad=str(d.get("entidad", "")),
        tipo_credito=str(d.get("tipo_credito", "")),
        estrategia=d.get("estrategia", "amortizable"),
        moneda=d.get("moneda", "NIO"),
        valor_inicial=float(d.get("valor_inicial", 0.0) or 0.0),
        saldo_reportado=float(d.get("saldo_reportado", 0.0) or 0.0),
        cuota=float(d.get("cuota", 0.0) or 0.0),
        fecha_otorgamiento=_fecha(d.get("fecha_otorgamiento")) or date(1970, 1, 1),
        fecha_actualizado=_fecha(d.get("fecha_actualizado")) or date(1970, 1, 1),
        fecha_vencimiento=_fecha(d.get("fecha_vencimiento")),
        tasa_mensual=(float(d["tasa_mensual"]) if d.get("tasa_mensual") is not None else None),
        saldo_apertura=(float(d["saldo_apertura"]) if d.get("saldo_apertura") is not None else None),
        incluir_en_er=bool(d.get("incluir_en_er", True)),
        saldos_mensuales=(dict(d["saldos_mensuales"]) if d.get("saldos_mensuales") else None),
        notas=str(d.get("notas", "")),
    )


def _dep_ppe_mensual(body: Mapping) -> float | None:
    """Depreciacion mensual calculada por componentes de PPE, si el body trae
    el bloque `depreciacion_ppe` (ver motor/depreciacion). None = no se pidio
    calculo y manda el valor manual del ER."""
    spec = body.get("depreciacion_ppe")
    if not spec:
        return None
    from motor.depreciacion import calcular_depreciacion

    return calcular_depreciacion(spec, body.get("saldos_iniciales") or {}).gasto_mensual_total


def _er_lineas(body: Mapping, periodo: PeriodoSpec) -> list[ER_LineaMes]:
    """Construye el ER segun er_modo: 'manual' (default, lista mes a mes tal
    cual) o 'generado' (base + bandas centradas, ver motor/er_generado).

    Si el body trae `depreciacion_ppe`, la depreciacion mensual calculada
    por componente reemplaza al valor manual en ambos modos."""
    dep_ppe = _dep_ppe_mensual(body)
    modo = str(body.get("er_modo") or "manual").lower()
    if modo == "manual":
        from dataclasses import replace

        lineas = [_er_linea(x) for x in (body.get("er_mensual") or [])]
        # Costo generado sobre ingresos DADOS: el CPA tiene la venta exacta
        # del cliente pero no el costo, solo el % que ronda (con banda).
        cg = body.get("costo_generado") or {}
        if cg and float(cg.get("pct_sobre_venta", 0) or 0) > 0:
            from motor.er_generado import generar_costo_sobre_ingresos

            meses = [ln.mes for ln in lineas]
            ingresos = {ln.mes: ln.ingresos for ln in lineas}
            seed = str(cg.get("seed") or "")
            if not seed:
                cedula = str((body.get("datos") or {}).get("cedula") or "")
                seed = f"{cedula}|{periodo.mes_inicial}|{periodo.mes_final}"
            costos = generar_costo_sobre_ingresos(
                ingresos, meses,
                pct_sobre_venta=float(cg["pct_sobre_venta"]),
                banda_pct=float(cg.get("banda_pct", 2.0) or 0.0),
                seed=seed,
            )
            lineas = [replace(ln, costo_ventas=costos[ln.mes]) for ln in lineas]
        if dep_ppe is not None:
            lineas = [replace(ln, gasto_depreciacion=dep_ppe) for ln in lineas]
        return lineas
    if modo != "generado":
        raise ValueError(f"er_modo invalido {modo!r}; usa 'manual' o 'generado'")

    from motor.er_generado import ERGeneradoParams, generar_er_mensual

    g = body.get("er_generado") or {}
    params = ERGeneradoParams(
        ingreso_base=float(g.get("ingreso_base", 0) or 0),
        costo_pct_sobre_venta=float(g.get("costo_pct_sobre_venta", 0) or 0),
        banda_ingreso_pct=float(g.get("banda_ingreso_pct", 20) or 20),
        banda_costo_pct=float(g.get("banda_costo_pct", 5) or 5),
        seed=str(g.get("seed") or ""),
        gasto_depreciacion_mensual=(
            dep_ppe if dep_ppe is not None
            else float(g.get("gasto_depreciacion_mensual", 0) or 0)
        ),
        gastos_fijos=g.get("gastos_fijos") or {},
        gastos_overrides=g.get("gastos_overrides") or {},
    )
    cedula = str((body.get("datos") or {}).get("cedula") or "")
    return generar_er_mensual(params, periodo, cedula=cedula)


def _bandas(d: Mapping | None) -> Bandas:
    """Amplitud de oscilacion por concepto; sin bloque, los defaults."""
    d = d or {}
    base = Bandas()
    return Bandas(**{
        campo: float(d[campo]) if d.get(campo) is not None else getattr(base, campo)
        for campo in CAMPOS_BANDA
    })


def _minimos(d: Mapping | None) -> Minimos:
    """Pisos/topes que el motor no puede violar. Sin bloque, los defaults
    (piso 0 = solo se garantiza caja no negativa, aporte sin tope)."""
    d = d or {}
    tope = d.get("aporte_maximo")
    return Minimos(
        caja=float(d.get("caja") or 0.0),
        inventario=float(d.get("inventario") or 0.0),
        aporte_maximo=(float(tope) if tope not in (None, "") else None),
    )


def _utilidad_objetivo(d: Mapping | None) -> UtilidadObjetivo:
    """Piso de utilidad promedio. Sin bloque o en 0, no se mide."""
    d = d or {}
    return UtilidadObjetivo(
        monto=float(d.get("monto") or 0.0),
        moneda=(str(d.get("moneda") or "NIO").upper() if d.get("moneda") else "NIO"),
    )


def inputs_from_json(body: Mapping) -> InputsTipoA:
    periodo = _periodo(body["periodo"])
    return InputsTipoA(
        periodo=periodo,
        datos=_datos(body.get("datos") or {}),
        er_mensual=_er_lineas(body, periodo),
        saldos_iniciales=_esf(body.get("saldos_iniciales")),
        saldos_finales=_esf(body.get("saldos_finales")),
        deudas=[_deuda(x) for x in (body.get("deudas") or [])],
        bandas=_bandas(body.get("bandas")),
        minimos=_minimos(body.get("minimos")),
        utilidad_objetivo=_utilidad_objetivo(body.get("utilidad_objetivo")),
    )


def _cuenta_objetivo(d: Mapping) -> CuentaObjetivo:
    return CuentaObjetivo(
        cuenta=str(d["cuenta"]),
        objetivo=float(d["objetivo"]),
        tolerancia_pct=float(d.get("tolerancia_pct", 20.0)),
    )


def inputs_tipo_b_from_json(body: Mapping) -> InputsTipoB:
    periodo = _periodo(body["periodo"])
    return InputsTipoB(
        periodo=periodo,
        datos=_datos(body.get("datos") or {}),
        er_mensual=_er_lineas(body, periodo),
        saldos_iniciales=_esf(body.get("saldos_iniciales")),
        cuentas_objetivo=[_cuenta_objetivo(x) for x in (body.get("cuentas_objetivo") or [])],
        deudas=[_deuda(x) for x in (body.get("deudas") or [])],
        seed=str(body.get("seed", "") or ""),
        bandas=_bandas(body.get("bandas")),
        minimos=_minimos(body.get("minimos")),
        utilidad_objetivo=_utilidad_objetivo(body.get("utilidad_objetivo")),
    )


def modelo_from_json(body: Mapping):
    """Despacha por periodo.tipo al regimen correcto y corre el motor."""
    from motor.orquestador import certificar_tipo_a, certificar_tipo_b

    tipo = str(((body.get("periodo") or {}).get("tipo") or "A")).upper()
    if tipo == "B":
        return certificar_tipo_b(inputs_tipo_b_from_json(body))
    return certificar_tipo_a(inputs_from_json(body))


def validacion_to_json(modelo) -> dict:
    return {
        "ok": modelo.validacion.ok,
        "errores": [
            {"invariante": h.invariante, "mensaje": h.mensaje} for h in modelo.validacion.errores
        ],
        "alertas": [
            {"invariante": h.invariante, "mensaje": h.mensaje} for h in modelo.validacion.alertas
        ],
    }

"""Continuar una certificacion ya emitida agregando meses al final.

El caso: se emitio el documento (p.ej. ene-jun), el credito no se tramito y
vencio. Hay que certificar hasta julio SIN que cambie una sola cifra de los
meses que el banco ya tiene firmados.

Por que no alcanza con mover el mes_final
-----------------------------------------
El ER generado y las trayectorias del motor derivan su semilla de
cedula|mes_inicial|mes_final. Cambiar el corte cambia la semilla y con ella
TODOS los meses: en un caso real, los seis meses del ER se movieron entre
-7% y +6%. Dos certificaciones firmadas con cifras distintas para el mismo
mes es un problema de credibilidad, no un detalle.

Que hace este modulo
--------------------
Toma el periodo ya emitido, lo recalcula (el motor es determinista, asi que
reproduce el documento al cordoba) y arma los inputs del periodo extendido:

  - misma apertura  -> el capital de apertura no se mueve y las dos
                       certificaciones son una sola cadena;
  - ER de los meses ya certificados CONGELADO cifra por cifra (pasa a modo
    manual), y los meses nuevos generados con los MISMOS parametros;
  - deuda congelada mes a mes (saldo e interes) y proyectada un mes mas
    siguiendo el plan ya certificado, porque para el periodo nuevo casi
    nunca hay un reporte de credito actualizado;
  - saldos finales derivados: se parte del ESF certificado y se le suma el
    movimiento de los meses nuevos.

Nada de esto adivina: todo sale de recalcular lo que ya estaba emitido.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from motor.json_io import modelo_from_json


def _meses_entre(desde: str, hasta: str) -> list[str]:
    y, m = int(desde[:4]), int(desde[5:7])
    fy, fm = int(hasta[:4]), int(hasta[5:7])
    out: list[str] = []
    while (y, m) <= (fy, fm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _er_congelado(modelo, meses: list[str]) -> list[dict]:
    """El ER de los meses ya certificados, cifra por cifra.

    Los Gastos Financieros NO van aca: no son input del ER, los calcula el
    motor desde las deudas. Se congelan por el lado de la deuda (ver
    _deudas_congeladas), que es de donde salen."""
    from motor.er import (
        LABEL_ALCALDIA, LABEL_COMBUSTIBLE, LABEL_DEPRECIACION, LABEL_MANTENIMIENTOS,
        LABEL_OTROS, LABEL_PUBLICIDAD, LABEL_RENTA, LABEL_SEGUROS, LABEL_SERVICIOS,
        LABEL_SUELDOS,
    )

    campos = {
        "sueldos_salarios": LABEL_SUELDOS,
        "servicios_publicos": LABEL_SERVICIOS,
        "alcaldia_dgi": LABEL_ALCALDIA,
        "combustible": LABEL_COMBUSTIBLE,
        "publicidad": LABEL_PUBLICIDAD,
        "mantenimientos": LABEL_MANTENIMIENTOS,
        "renta": LABEL_RENTA,
        "seguros": LABEL_SEGUROS,
        "otros_gastos": LABEL_OTROS,
        "gasto_depreciacion": LABEL_DEPRECIACION,
    }
    out = []
    for mes in meses:
        linea = {
            "mes": mes,
            "ingresos": modelo.er.ingresos_mes[mes],
            "costo_ventas": modelo.er.costo_ventas_mes[mes],
        }
        for campo, label in campos.items():
            linea[campo] = modelo.er.gastos_por_label_mes[label][mes]
        out.append(linea)
    return out


def _deudas_congeladas(modelo, meses_viejos: list[str], meses_nuevos: list[str]) -> list[dict]:
    """Deuda con la trayectoria ya certificada, extendida a los meses nuevos.

    Los meses viejos llevan el saldo y el interes exactos del documento
    emitido. Los nuevos continuan el plan: al saldo del ultimo mes
    certificado se le aplica un mes mas de amortizacion con la tasa que el
    motor infirio, o se lo deja oscilando si es una tarjeta.

    El saldo proyectado del ultimo mes pasa a ser saldo_reportado, que es lo
    que el invariante #1 exige. Es una PROYECCION declarada, no un dato del
    reporte: el respaldo del periodo nuevo es la certificacion anterior mas
    el plan de pagos, no un reporte de credito actualizado.
    """
    out = []
    for plan in modelo.planes + modelo.planes_soporte:
        d = plan.deuda
        tc = modelo.inputs.periodo.tasa_cambio if d.moneda == "USD" else 1.0
        saldos: dict[str, float] = {}
        intereses: dict[str, float] = {}
        for c in plan.cuotas:
            if c.mes in meses_viejos:
                saldos[c.mes] = c.saldo_final_nio / tc
                intereses[c.mes] = c.interes_nio / tc

        saldo = saldos.get(meses_viejos[-1], d.saldo_reportado) if meses_viejos else d.saldo_reportado
        tasa = plan.tasa_mensual_inferida
        for mes in meses_nuevos:
            if d.estrategia == "revolving":
                # Una tarjeta no se amortiza: el saldo sigue donde estaba.
                interes = min(d.cuota, saldo * tasa) if tasa else d.cuota
                nuevo = saldo
            else:
                interes = saldo * tasa
                abono = max(0.0, min(d.cuota - interes, saldo))
                nuevo = max(0.0, saldo - abono)
            saldos[mes] = nuevo
            intereses[mes] = interes
            saldo = nuevo

        out.append({
            "numero": d.numero,
            "entidad": d.entidad,
            "tipo_credito": d.tipo_credito,
            "estrategia": d.estrategia,
            "moneda": d.moneda,
            "valor_inicial": d.valor_inicial,
            # El corte del periodo nuevo es el saldo proyectado, no el del
            # reporte viejo: si no, el invariante #1 lo rechazaria.
            "saldo_reportado": round(saldo, 2),
            "cuota": d.cuota,
            "fecha_otorgamiento": d.fecha_otorgamiento.isoformat(),
            "fecha_actualizado": d.fecha_actualizado.isoformat(),
            "fecha_vencimiento": d.fecha_vencimiento.isoformat() if d.fecha_vencimiento else None,
            "tasa_mensual": tasa or None,
            "saldo_apertura": plan.saldo_apertura_nio / tc,
            "incluir_en_er": d.incluir_en_er,
            "saldos_mensuales": {m: round(v, 2) for m, v in saldos.items()},
            "intereses_mensuales": {m: round(v, 2) for m, v in intereses.items()},
            "notas": d.notas,
        })
    return out


def preparar_continuacion(
    inputs_emitidos: Mapping[str, Any], nuevo_mes_final: str
) -> dict[str, Any]:
    """Inputs del periodo extendido, con lo ya certificado congelado.

    inputs_emitidos: el payload del periodo finalizado (tal cual se guardo).
    nuevo_mes_final: hasta donde se extiende ('YYYY-MM').

    Devuelve un body listo para el motor. Los meses nuevos quedan con el ER
    generado por los MISMOS parametros del periodo anterior; si el CPA
    prefiere cargarlos a mano, edita esas filas.
    """
    emitido = modelo_from_json(copy.deepcopy(inputs_emitidos))
    periodo = dict(inputs_emitidos["periodo"])
    mes_inicial, viejo_final = periodo["mes_inicial"], periodo["mes_final"]
    if nuevo_mes_final <= viejo_final:
        raise ValueError(
            f"El nuevo corte ({nuevo_mes_final}) debe ser posterior al ya "
            f"certificado ({viejo_final})."
        )

    meses_viejos = _meses_entre(mes_inicial, viejo_final)
    meses_nuevos = _meses_entre(_siguiente(viejo_final), nuevo_mes_final)

    body = copy.deepcopy(dict(inputs_emitidos))
    body["periodo"] = {**periodo, "mes_final": nuevo_mes_final}

    # ER: lo certificado se congela; lo nuevo se genera con los mismos
    # parametros y queda editable.
    er = _er_congelado(emitido, meses_viejos)
    er += _er_nuevos_meses(inputs_emitidos, emitido, meses_nuevos)
    body["er_modo"] = "manual"
    body["er_mensual"] = er
    body.pop("er_generado", None)
    body.pop("costo_generado", None)  # ya viene dentro de las cifras congeladas

    body["deudas"] = _deudas_congeladas(emitido, meses_viejos, meses_nuevos)
    if str(periodo.get("tipo") or "A").upper() == "A":
        body["saldos_finales"] = _saldos_finales_derivados(body, inputs_emitidos)
    body["_continua_de"] = {
        "mes_inicial": mes_inicial,
        "mes_final_certificado": viejo_final,
        "meses_congelados": meses_viejos,
        "meses_nuevos": meses_nuevos,
    }
    return body


# Cuentas que el motor DERIVA del movimiento: su saldo al nuevo corte sale
# del calculo, no del cliente. El resto (inventario, cartera, proveedores,
# PPE bruto) se mantiene en el saldo ya certificado: el cliente no reporto
# un cierre nuevo, y sostener el ultimo dato firmado es lo defendible.
_CUENTAS_DERIVADAS = {
    "efectivo", "depreciacion_acumulada", "tarjetas_credito",
    "creditos_hipotecarios", "creditos_consumo", "creditos_personales",
    "creditos_prendarios", "creditos_comerciales",
}


def _saldos_finales_derivados(body: Mapping[str, Any], inputs_emitidos: Mapping[str, Any]) -> dict:
    """Balance de cierre del periodo extendido, sin pedirselo al cliente.

    Se corre el motor una vez con el ancla del periodo anterior y se toma el
    ESF que resulta al nuevo corte: las cuentas derivadas (caja, depreciacion,
    deuda) se actualizan y las operativas se quedan donde el documento
    emitido las dejo. Como esas ultimas conservan su ancla, la segunda
    pasada no mueve las trayectorias — converge de una."""
    tentativo = copy.deepcopy(dict(body))
    tentativo["saldos_finales"] = dict(inputs_emitidos.get("saldos_finales") or {})
    corte = modelo_from_json(tentativo).esf.corte()
    finales = dict(inputs_emitidos.get("saldos_finales") or {})
    for cuenta in _CUENTAS_DERIVADAS:
        finales[cuenta] = round(float(getattr(corte, cuenta, 0.0)), 2)
    # El capital no se declara: lo recalcula el motor desde la apertura.
    finales.pop("capital", None)
    return finales


def _siguiente(mes: str) -> str:
    y, m = int(mes[:4]), int(mes[5:7])
    m += 1
    if m > 12:
        m, y = 1, y + 1
    return f"{y:04d}-{m:02d}"


def _er_nuevos_meses(inputs_emitidos, emitido, meses_nuevos: list[str]) -> list[dict]:
    """Meses nuevos con los MISMOS parametros del periodo anterior.

    Con ER generado se reusan base, bandas y % de costo; los gastos fijos se
    copian del ultimo mes certificado, que es lo que el CPA espera (la renta
    y los servicios no cambian porque corrio un mes)."""
    ultimo = emitido.er.meses[-1]
    base = {
        "sueldos_salarios": "Sueldos y Salarios",
        "servicios_publicos": "Servicios Públicos",
        "alcaldia_dgi": "Alcaldía y DGI",
        "combustible": "Combustible",
        "publicidad": "Publicidad",
        "mantenimientos": "Mantenimientos",
        "renta": "Renta",
        "seguros": "Seguros",
        "otros_gastos": "Otros Gastos",
        "gasto_depreciacion": "Gasto por Depreciación",
    }
    gastos = {k: emitido.er.gastos_por_label_mes[v][ultimo] for k, v in base.items()}

    g = inputs_emitidos.get("er_generado") or {}
    if g:
        from motor.er_generado import ERGeneradoParams, generar_er_mensual
        from motor.inputs import PeriodoSpec

        params = ERGeneradoParams(
            ingreso_base=float(g.get("ingreso_base", 0) or 0),
            costo_pct_sobre_venta=float(g.get("costo_pct_sobre_venta", 0) or 0),
            banda_ingreso_pct=float(g.get("banda_ingreso_pct", 20) or 20),
            banda_costo_pct=float(g.get("banda_costo_pct", 5) or 5),
            seed=str(g.get("seed") or ""),
            gasto_depreciacion_mensual=gastos["gasto_depreciacion"],
            gastos_fijos=g.get("gastos_fijos") or {},
            gastos_overrides=g.get("gastos_overrides") or {},
        )
        spec = PeriodoSpec(
            tipo=inputs_emitidos["periodo"]["tipo"],
            mes_inicial=meses_nuevos[0], mes_final=meses_nuevos[-1],
            tasa_cambio=float(inputs_emitidos["periodo"]["tasa_cambio"]),
        )
        generadas = generar_er_mensual(
            params, spec, cedula=str((inputs_emitidos.get("datos") or {}).get("cedula") or "")
        )
        return [
            {"mes": ln.mes, "ingresos": ln.ingresos, "costo_ventas": ln.costo_ventas, **gastos}
            for ln in generadas
        ]

    # ER manual: se repite el ultimo mes certificado como punto de partida.
    return [
        {"mes": mes,
         "ingresos": emitido.er.ingresos_mes[ultimo],
         "costo_ventas": emitido.er.costo_ventas_mes[ultimo],
         **gastos}
        for mes in meses_nuevos
    ]


def verificar_inmutabilidad(emitido, continuado, meses_congelados: list[str]) -> list[str]:
    """Que ningun mes ya certificado haya cambiado de cifra.

    Es el guardia de todo esto: si algo se movio, el documento nuevo
    contradiria al que el banco ya tiene. Devuelve la lista de diferencias
    (vacia = todo igual)."""
    fallas: list[str] = []
    for mes in meses_congelados:
        for etiqueta, viejo, nuevo in (
            ("ingresos", emitido.er.ingresos_mes.get(mes), continuado.er.ingresos_mes.get(mes)),
            ("costo de ventas", emitido.er.costo_ventas_mes.get(mes),
             continuado.er.costo_ventas_mes.get(mes)),
            ("gastos financieros", emitido.er.gastos_financieros_mes.get(mes),
             continuado.er.gastos_financieros_mes.get(mes)),
            ("utilidad neta", emitido.er.utilidad_neta_mes.get(mes),
             continuado.er.utilidad_neta_mes.get(mes)),
        ):
            if viejo is None or nuevo is None:
                continue
            if abs(viejo - nuevo) > 1.0:
                fallas.append(
                    f"{mes} {etiqueta}: certificado {viejo:,.0f} != recalculado {nuevo:,.0f}"
                )
    return fallas

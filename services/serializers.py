from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from db.models import Cliente, GiroNegocio, PeriodoCertificacion


def parse_json_object(value: str | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if not value:
        return dict(fallback or {})
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else dict(fallback or {})
    except Exception:
        return dict(fallback or {})


def iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def giro_to_dict(giro: GiroNegocio) -> dict[str, Any]:
    return {
        "id": giro.id,
        "nombre": giro.nombre,
        "descripcion": giro.descripcion,
        "cost_pct_min": giro.cost_pct_min,
        "cost_pct_max": giro.cost_pct_max,
        "variabilidad_ingresos_pct": giro.variabilidad_ingresos_pct,
        "variabilidad_costos_pct": giro.variabilidad_costos_pct,
        "plantilla_gastos": parse_json_object(giro.plantilla_gastos_json),
        "cuentas_balance": json.loads(giro.cuentas_balance_json or "[]"),
        "activo": bool(giro.activo),
        "created_at": iso(giro.created_at),
        "updated_at": iso(giro.updated_at),
    }


def cliente_to_dict(cliente: Cliente, *, include_giro: bool = False) -> dict[str, Any]:
    data = {
        "id": cliente.id,
        "nombre_completo": cliente.nombre_completo,
        "cedula": cliente.cedula,
        "fecha_nacimiento": iso(cliente.fecha_nacimiento),
        "direccion_domicilio": cliente.direccion_domicilio,
        "telefono": cliente.telefono,
        "email": cliente.email,
        "nombre_negocio": cliente.nombre_negocio,
        "ruc": cliente.ruc,
        "matricula_roc": cliente.matricula_roc,
        "direccion_negocio": cliente.direccion_negocio,
        "giro_negocio_id": cliente.giro_negocio_id,
        "fecha_inicio_negocio": iso(cliente.fecha_inicio_negocio),
        # Campos de certificacion
        "sexo": cliente.sexo,
        "estado_civil": cliente.estado_civil,
        "profesion": cliente.profesion,
        "banco": cliente.banco,
        "regimen": cliente.regimen,
        "antiguedad": cliente.antiguedad,
        "empleados": cliente.empleados,
        "domicilio": cliente.domicilio,
        "plantilla_gastos": parse_json_object(cliente.plantilla_gastos_json) if cliente.plantilla_gastos_json else None,
        "activo": bool(cliente.activo),
        "created_at": iso(cliente.created_at),
        "updated_at": iso(cliente.updated_at),
        "created_by": cliente.created_by,
    }
    if include_giro and cliente.giro:
        data["giro"] = giro_to_dict(cliente.giro)
    return data


def periodo_to_basic_dict(periodo: PeriodoCertificacion) -> dict[str, Any]:
    return {
        "id": periodo.id,
        "cliente_id": periodo.cliente_id,
        "periodo_meses": periodo.periodo_meses,
        "mes_inicial": periodo.mes_inicial,
        "mes_final": periodo.mes_final,
        "estado": periodo.estado,
        "saldos_iniciales_origen": periodo.saldos_iniciales_origen,
        "documento_generado_at": iso(periodo.documento_generado_at),
        "created_at": iso(periodo.created_at),
        "updated_at": iso(periodo.updated_at),
        "finalized_at": iso(periodo.finalized_at),
    }

"""Adaptador entre PeriodoCertificacion + Cliente y el generador DOCX.

Toma un periodo persistente y construye los DataFrames que
`document_generator.generar_documento_completo` espera (df_esf, df_er,
df_datos, df_cert), enriqueciendo el payload con los datos del Cliente
para que `_build_cert_dataframe` los recoja.

Campos del Cliente que no estan en SQLite todavia (sexo, estado_civil,
profesion, banco, regimen, antiguedad, empleados, domicilio) se intentan
recuperar de last_cedula_extracted_json / last_matricula_extracted_json
si existen, y en su defecto quedan vacios. El contador puede editarlos
en Word despues de generar.
"""

from __future__ import annotations

import json
from typing import Any

from db.models import Cliente, GiroNegocio, PeriodoCertificacion


def periodo_to_dataframes(
    periodo: PeriodoCertificacion,
    cliente: Cliente | None,
    giro: GiroNegocio | None = None,
):
    """Construye los DataFrames del documento desde un periodo persistente.

    Devuelve una tupla (df_esf, df_er, df_datos, df_cert).

    Importa pesado dentro de la funcion para no atar al modulo si no se usa.
    """
    from financial_model import build_financial_model

    payload = _enrich_payload(periodo, cliente, giro)
    result = build_financial_model(payload)
    return (
        result.df_esf_mensual,
        result.df_er,
        result.df_datos,
        result.df_certificacion,
    )


def _enrich_payload(
    periodo: PeriodoCertificacion,
    cliente: Cliente | None,
    giro: GiroNegocio | None,
) -> dict[str, Any]:
    """Toma el payload guardado del periodo y le inyecta payload['client']."""
    try:
        payload = json.loads(periodo.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    payload["client"] = _client_block(cliente, giro)
    return payload


def _client_block(cliente: Cliente | None, giro: GiroNegocio | None) -> dict[str, Any]:
    if not cliente:
        return {}

    ced = _safe_json(cliente.last_cedula_extracted_json)
    mat = _safe_json(cliente.last_matricula_extracted_json)

    # Resolucion en cascada: valor persistido en Cliente -> fallback a vision IA -> ""
    block = {
        "nombre_completo": cliente.nombre_completo or _first_str(ced, "nombre_completo", "name"),
        "cedula": cliente.cedula,
        "direccion_personal": cliente.direccion_domicilio
            or _first_str(ced, "direccion", "domicilio"),
        "direccion_negocio": cliente.direccion_negocio
            or _first_str(mat, "direccion", "direccion_negocio"),
        "contacto": cliente.telefono or _first_str(mat, "telefono"),
        "matricula": cliente.matricula_roc or _first_str(mat, "roc", "matricula", "registro"),
        "ruc": cliente.ruc or _first_str(mat, "ruc"),
        "nombre_negocio": cliente.nombre_negocio or _first_str(mat, "nombre_negocio", "nombre"),
        # Campos de certificacion: directos del Cliente con fallback IA
        "sexo": cliente.sexo or _first_str(ced, "sexo", "genero"),
        "estado_civil": cliente.estado_civil or _first_str(ced, "estado_civil"),
        "profesion": cliente.profesion or _first_str(ced, "profesion", "profession"),
        "domicilio": cliente.domicilio or _first_str(ced, "lugar_nacimiento", "domicilio"),
        "primer_apellido": _first_str(ced, "primer_apellido", "apellido1", "apellidos"),
        "banco": cliente.banco or "",
        "regimen": cliente.regimen or "",
        "antiguedad": cliente.antiguedad or "",
        "empleados": cliente.empleados or "",
        # Giro: nombre del catalogo
        "giro_negocio": (giro.nombre if giro else "") or "",
        # Fecha certificacion: hoy. El contador puede ajustar en Word.
        "fecha_certificacion": None,
    }
    return block


def _safe_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _first_str(source: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = source.get(k)
        if v is None:
            continue
        text = str(v).strip()
        if text:
            return text
    return ""

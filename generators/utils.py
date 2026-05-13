"""
utilities for robust extraction of fields from the 'Certificacion' sheet.

Provides:
- extract_cert_fields(dfc): returns a dict with normalized keys required by generators.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Dict, Iterable, Optional

import pandas as pd


_KEY_SYNONYMS = {
    "nombre": ["nombre", "nombres"],
    "nombre_completo": [
        "nombre completo", "nombres y apellidos", "nombre y apellido",
        "nombre y apellidos", "nombre del cliente", "nombre del propietario",
    ],
    "apellido": ["apellido", "apellidos"],
    "cedula": ["cedula", "cédula", "cedula de identidad", "cédula de identidad", "ci"],
    "inicio": ["periodo inicio", "inicio", "fecha inicio", "periodo desde", "desde"],
    "fin": ["periodo fin", "fin", "fecha fin", "periodo hasta", "hasta"],
    "estado_civil": ["estado civil"],
    "profesion": ["profesion", "profesión"],
    "sexo": ["sexo", "genero", "género"],
    "domicilio": ["domicilio", "ciudad domicilio", "ciudad"],
    "direccion_personal": ["direccion personal", "dirección personal"],
    "direccion_negocio": [
        "direccion negocio",
        "dirección negocio",
        "direccion del negocio",
        "dirección del negocio",
        "direccion",
        "dirección",
    ],
    "primer_apellido": ["primer apellido"],
    "ingresos_brutos": ["ingresos brutos"],
    "ingresos_promedio": ["ingresos promedio", "promedio ingresos", "promedio mensual ingresos"],
    "utilidad_periodo": ["utilidad periodo", "utilidad del periodo", "utilidad neta periodo"],
    "utilidad_promedio": ["utilidad promedio", "promedio utilidad", "promedio mensual utilidades"],
    "banco": ["banco", "entidad bancaria"],
    "fecha_certificacion": [
        "fecha certificacion",
        "fecha de certificacion",
        "fecha de la certificacion",
        "fecha certificación",
    ],
}


def _norm(s: Any) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")  # remove accents
    s = " ".join(s.replace("_", " ").split())
    return s


def _try_pos(dfc: pd.DataFrame, idx: int) -> Optional[Any]:
    try:
        return dfc.iloc[idx, 1]
    except Exception:
        return None


def _coerce_date(val: Any) -> Optional[pd.Timestamp]:
    try:
        ts = pd.to_datetime(val, errors="coerce")
        return None if pd.isna(ts) else ts
    except Exception:
        return None


def _coerce_num(val: Any) -> Optional[float]:
    try:
        num = pd.to_numeric(val, errors="coerce")
        return None if pd.isna(num) else float(num)
    except Exception:
        return None


def _build_label_map(dfc: pd.DataFrame) -> Dict[str, Any]:
    mapping: Dict[str, Any] = {}
    try:
        for i in range(len(dfc.index)):
            key = _norm(dfc.iloc[i, 0])
            if not key:
                continue
            mapping[key] = dfc.iloc[i, 1]
    except Exception:
        # if the sheet is empty or malformed, leave mapping empty
        pass
    return mapping


def _find_in_map(mapping: Dict[str, Any], candidates: Iterable[str]) -> Optional[Any]:
    for cand in candidates:
        if cand in mapping:
            return mapping[cand]
    return None


def extract_cert_fields(dfc: pd.DataFrame) -> Dict[str, Any]:
    """
    Returns a dict with the keys required by generators, trying label-based lookup
    first and falling back to positional indices used historically.

    Keys:
      nombre, apellido, cedula, inicio, fin, estado_civil, profesion, sexo,
      domicilio, direccion_personal, direccion_negocio, primer_apellido,
      ingresos_brutos, ingresos_promedio, utilidad_periodo, utilidad_promedio,
      banco, fecha_certificacion
    """
    if dfc is None or dfc.empty:
        raise ValueError("Hoja 'Certificacion' vacía o no cargada")

    m = _build_label_map(dfc)

    # Si existe "Nombre completo" en la hoja, asumimos el formato nuevo y
    # evitamos usar fallback posicional (para no confundir celdas).
    has_fullname = _find_in_map(m, _KEY_SYNONYMS.get("nombre_completo", [])) is not None

    def pick(key: str, pos_idx: Optional[int] = None, *, is_date=False, is_num=False):
        val = _find_in_map(m, _KEY_SYNONYMS.get(key, []))
        if (val is None) and (pos_idx is not None) and (not has_fullname):
            val = _try_pos(dfc, pos_idx)
        if is_date:
            return _coerce_date(val)
        if is_num:
            return _coerce_num(val)
        return val

    data = {
        "nombre": pick("nombre", 0),
        "apellido": pick("apellido", 1),
        "nombre_completo": pick("nombre_completo", None),
        "cedula": pick("cedula", 2),
        "inicio": pick("inicio", 3, is_date=True),
        "fin": pick("fin", 4, is_date=True),
        "estado_civil": pick("estado_civil", 5),
        "profesion": pick("profesion", 6),
        "sexo": pick("sexo", 7),
        "domicilio": pick("domicilio", 8),
        "direccion_personal": pick("direccion_personal", 9),
        "direccion_negocio": pick("direccion_negocio", 10),
        "primer_apellido": pick("primer_apellido", 11),
        "ingresos_brutos": pick("ingresos_brutos", 12, is_num=True),
        "ingresos_promedio": pick("ingresos_promedio", 13, is_num=True),
        "utilidad_periodo": pick("utilidad_periodo", 14, is_num=True),
        "utilidad_promedio": pick("utilidad_promedio", 15, is_num=True),
        "banco": pick("banco", 16),
        "fecha_certificacion": pick("fecha_certificacion", 17, is_date=True),
    }

    # Completar nombre_completo si falta; derivar nombre/apellido básicos si no vienen
    full = data.get("nombre_completo")
    if not full:
        n = str(data.get("nombre") or "").strip()
        a = str(data.get("apellido") or "").strip()
        combo = (n + " " + a).strip()
        data["nombre_completo"] = combo if combo else None

    # Si tenemos nombre_completo pero faltan nombre/apellido (formato nuevo),
    # podemos derivar heurísticamente (opcional, no crítico para el flujo).
    if data.get("nombre_completo") and (not data.get("nombre") or not data.get("apellido")):
        parts = str(data.get("nombre_completo")).split()
        if len(parts) >= 2:
            # heurística: último(s) dos tokens como apellidos si hay >2 tokens
            if len(parts) >= 3:
                data.setdefault("apellido", " ".join(parts[-2:]))
                data.setdefault("nombre", " ".join(parts[:-2]))
            else:
                data.setdefault("apellido", parts[-1])
                data.setdefault("nombre", parts[0])

    return data

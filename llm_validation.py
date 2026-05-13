"""
llm_validation.py

Validador semántico opcional con LLM (OpenAI u otro compatible).
Construye un snapshot JSON de datos clave y solicita un dict estructurado
de issues (id, severidad, descripción, evidencia, sugerencia) y un veredicto.

Requiere OPENAI_API_KEY en entorno. Se puede cargar con python-dotenv.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any, Dict, Optional
from datetime import datetime, date
try:
    import numpy as _np  # type: ignore
except Exception:  # pragma: no cover
    _np = None

import pandas as pd

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    def load_dotenv(*args, **kwargs):  # fallback noop
        return False


def _df_head(df: pd.DataFrame, n: int = 10):
    """Devuelve un preview serializable en JSON: columnas y valores como str."""
    try:
        _df = df.head(n).copy()
        _df.columns = [str(c) for c in _df.columns]
        return _df.astype(str).to_dict(orient="records")
    except Exception:
        return []


def _sanitize(obj: Any) -> Any:
    """Convierte objetos no serializables (Timestamp, datetime, numpy) a tipos JSON."""
    # pandas Timestamp
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    # datetime/date
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    # numpy scalars
    if _np is not None:
        if isinstance(obj, (_np.integer,)):
            return int(obj)
        if isinstance(obj, (_np.floating,)):
            return float(obj)
        if isinstance(obj, (_np.bool_,)):
            return bool(obj)
    # containers
    if isinstance(obj, dict):
        # Fuerza claves a str para compatibilidad JSON
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _norm_label(text: Any) -> str:
    s = str(text).strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = " ".join(s.split())
    return s


def _parse_month_label(name: str) -> Optional[pd.Timestamp]:
    nm = _norm_label(name)
    if not nm:
        return None
    ts = pd.to_datetime(nm, errors="coerce")
    if pd.notna(ts):
        return ts
    months = {
        "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
        "jul": 7, "ago": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dic": 12,
    }
    m = re.match(r"^(ene|feb|mar|abr|may|jun|jul|ago|sep|sept|oct|nov|dic)[a-z]*[\s\-_/\.]*([0-9]{2,4})$", nm)
    if m:
        month = months.get(m.group(1))
        year = int(m.group(2))
        if year < 100:
            year += 2000
        if month:
            return pd.Timestamp(year=year, month=month, day=1)
    m = re.match(r"^([0-9]{4})[\s\-_/\.]*([0-9]{1,2})$", nm)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        if 1 <= month <= 12:
            return pd.Timestamp(year=year, month=month, day=1)
    m = re.match(r"^([0-9]{1,2})[\s\-_/\.]*([0-9]{4})$", nm)
    if m:
        month = int(m.group(1))
        year = int(m.group(2))
        if 1 <= month <= 12:
            return pd.Timestamp(year=year, month=month, day=1)
    return None


def build_snapshot(
    df_er: pd.DataFrame,
    df_esf: pd.DataFrame,
    df_cert: pd.DataFrame,
    v_er: Dict[str, Any],
    v_esf: Dict[str, Any],
    doc_checks: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Añade metadatos temporales para ayudar al LLM
    temporal: Dict[str, Any] = {}
    try:
        # Detectar meses en columnas de ER (ignorar totales)
        if df_er is not None and not df_er.empty and df_er.shape[1] > 1:
            cols = [str(c) for c in df_er.columns[1:]]
            months = []
            for name in cols:
                nm = _norm_label(name)
                if ("acumul" in nm) or ("promedio" in nm) or ("dolar" in nm) or ("dólar" in nm):
                    continue
                ts = _parse_month_label(name)
                if ts is not None and pd.notna(ts):
                    months.append(pd.Period(ts, freq="M"))
            if months:
                months = sorted(set(months))
                temporal["er_months"] = [p.to_timestamp(how="end").date().isoformat() for p in months]
                temporal["last_er_month"] = months[-1].to_timestamp(how="end").date().isoformat()
                try:
                    full = list(pd.period_range(months[0], months[-1], freq="M"))
                    missing = [p for p in full if p not in months]
                    temporal["er_missing_months"] = [p.to_timestamp(how="end").date().isoformat() for p in missing]
                    temporal["er_months_consecutive"] = len(missing) == 0
                except Exception:
                    pass
        # Fecha de certificación desde hoja Certificacion
        cert_date = None
        try:
            from generators.utils import extract_cert_fields  # type: ignore
            cert = extract_cert_fields(df_cert)
            cd = cert.get("fecha_certificacion")
            if isinstance(cd, pd.Timestamp):
                cert_date = cd.date().isoformat()
            else:
                ts = pd.to_datetime(cd, errors="coerce")
                if pd.notna(ts):
                    cert_date = ts.date().isoformat()
        except Exception:
            pass
        temporal["cert_date"] = cert_date
        if temporal.get("cert_date") and temporal.get("last_er_month"):
            cd = pd.to_datetime(temporal["cert_date"])  # fecha certificación
            lm = pd.to_datetime(temporal["last_er_month"])  # último mes ER (fin de mes)
            temporal["cert_after_last_er"] = bool(cd >= lm)
    except Exception:
        temporal = {}

    snap: Dict[str, Any] = {
        "cert_sample": _df_head(df_cert, 25),
        "er_sample": _df_head(df_er, 20),
        "esf_sample": _df_head(df_esf, 30),
        "er_validation": v_er,
        "esf_validation": v_esf,
        "doc_validation": doc_checks or {},
        "temporal": temporal,
    }
    return snap


def llm_validate(
    snapshot: Dict[str, Any],
    *,
    model: str = "gpt-4o-mini",
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retorna un dict con estructura:
    {
      "ok": bool,
      "issues": [{"id": str, "severity": "alta|media|baja", "description": str, "evidence": str, "suggestion": str}],
      "summary": str,
      "model": str
    }
    """
    load_dotenv()
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        return {"ok": False, "issues": [], "summary": "Falta OPENAI_API_KEY", "model": model}

    # Importar aquí para no requerir el paquete si no se usa
    from openai import OpenAI  # type: ignore

    client = OpenAI(api_key=key)
    system = (
        "Eres auditor financiero y corrector de estilo. Revisa coherencia del texto y plausibilidad general.\n"
        "No recalcules sumas — confía en los resultados determinísticos. Devuelve un JSON estricto."
    )
    user = (
        "Instrucciones: \n- Si er_validation.ok y esf_validation.ok son True, no reportes errores de cálculo.\n- Ignora y NO reportes como issues: valores 0.0 o NaN en 'Check List' dentro de cert_sample.\n- Ignora y NO reportes filas con 'Descripción' = NaN en er_sample o esf_sample (son separadores/ruido).\n- No reportes 'fecha de inicio futura' como problema.\n- Valida que las columnas de er_sample que representan meses formen una secuencia mensual consecutiva sin saltos.\n  Considera como meses las columnas que parezcan fechas (YYYY-MM-DD, etc.). Ignora columnas de totales como 'Acumulado' o 'Promedio'.\n  Si existe temporal.er_months y temporal.er_missing_months, usa esos valores y no infieras desde er_sample.\n  Si temporal.er_months_consecutive es True, NO reportes huecos en la secuencia mensual.\n  Si hay huecos, crea un issue 'medium' con evidencia: found=[meses_detectados], missing=[meses_faltantes].\n- Valida que la fecha de certificación (en cert_sample) no sea anterior al último mes reportado en er_sample; si lo es, genera un issue 'medium' con evidencia: cert_date=..., last_er_month=....\n\nSnapshot: "
        "Responde con JSON: {ok, summary, issues:[{id,severity,description,evidence,suggestion}]}.\n" +
        json.dumps(_sanitize(snapshot), ensure_ascii=False)
    )

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    try:
        payload = resp.choices[0].message.content
        data = json.loads(payload)
    except Exception:
        data = {"ok": False, "issues": [], "summary": "Respuesta LLM no parseable", "model": model}
    # Post-procesamiento: filtrar falsos positivos usando metadatos temporales
    try:
        temporal = snapshot.get("temporal", {}) if isinstance(snapshot, dict) else {}
        if isinstance(data.get("issues"), list):
            filt = []
            missing = temporal.get("er_missing_months")
            months = temporal.get("er_months")
            for it in data["issues"]:
                txt = (str(it.get("description", "")) + " " + str(it.get("evidence", ""))).lower()
                if temporal.get("cert_after_last_er") is True:
                    if ("fecha de certific" in txt or "cert_date" in txt) and ("last_er_month" in txt or "ultimo mes" in txt or "último mes" in txt):
                        # filtra el falso positivo si ya sabemos que la cert >= ultimo mes
                        continue
                is_month_gap = ("mes" in txt) and ("secuencia" in txt or "faltan" in txt or "missing" in txt or "hueco" in txt or "huecos" in txt)
                if temporal.get("er_months_consecutive") is True and is_month_gap:
                    continue
                if isinstance(missing, list):
                    if not missing and is_month_gap:
                        continue
                    if missing and is_month_gap:
                        it["evidence"] = f"found={months or []}, missing={missing}"
                filt.append(it)
            data["issues"] = filt
            if data.get("ok") is False and not data["issues"]:
                data["ok"] = True
    except Exception:
        pass
    data["model"] = model
    return data


def llm_extract_cedula_from_text(
    ocr_front: str,
    ocr_back: Optional[str] = None,
    *,
    model: str = "gpt-4o-mini",
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extrae campos de una cédula nicaragüense a partir de texto OCR.
    Retorna un JSON con: { cedula, nombres, apellidos, nombre_completo }.
    """
    load_dotenv()
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        return {"ok": False, "error": "Falta OPENAI_API_KEY", "fields": {}}

    from openai import OpenAI  # type: ignore

    client = OpenAI(api_key=key)
    system = (
        "Eres experto extrayendo datos de cédulas nicaragüenses. Devuelve JSON estricto. "
        "Los nombres suelen estar en mayúsculas y sin acentos; también puede venir una MRZ con '<'."
    )
    user = {
        "task": "Extrae campos de la cédula desde OCR de frente y reverso.",
        "constraints": [
            "Si hay múltiples candidatos, prioriza etiquetas 'Nombres' y 'Apellidos'.",
            "Si falta alguno, usa la MRZ (parte con '<') para completar.",
            "No inventes datos que no estén en el texto.",
        ],
        "ocr_front": ocr_front or "",
        "ocr_back": ocr_back or "",
        "response_schema": {
            "cedula": "string|null",
            "nombres": "string|null",
            "apellidos": "string|null",
            "nombre_completo": "string|null",
        },
    }
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    try:
        payload = resp.choices[0].message.content
        data = json.loads(payload)
    except Exception:
        data = {"ok": False, "error": "Respuesta LLM no parseable", "fields": {}}
    return data



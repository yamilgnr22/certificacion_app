from __future__ import annotations

from typing import Any, Dict, Optional

from generators.utils import extract_cert_fields
from llm_vision import extract_cedula_with_vision, extract_matricula_with_vision
from difflib import SequenceMatcher

import unicodedata
import re


def _norm(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = " ".join(s.split())
    return s


_ADDRESS_NUM_TOKEN_MAP = {
    # Cardinales comunes
    "un": "1",
    "uno": "1",
    "una": "1",
    "dos": "2",
    "tres": "3",
    "cuatro": "4",
    "cinco": "5",
    "seis": "6",
    "siete": "7",
    "ocho": "8",
    "nueve": "9",
    "diez": "10",
    # Ordinales comunes
    "primer": "1",
    "primero": "1",
    "primera": "1",
    "segundo": "2",
    "segunda": "2",
    "tercer": "3",
    "tercero": "3",
    "tercera": "3",
    "cuarto": "4",
    "cuarta": "4",
    "quinto": "5",
    "quinta": "5",
    "sexto": "6",
    "sexta": "6",
    "septimo": "7",
    "septima": "7",
    "octavo": "8",
    "octava": "8",
    "noveno": "9",
    "novena": "9",
    "decimo": "10",
    "decima": "10",
}


def _canonicalize_address_token(tok: str) -> str:
    t = _norm(tok)
    if not t:
        return ""
    if t in _ADDRESS_NUM_TOKEN_MAP:
        return _ADDRESS_NUM_TOKEN_MAP[t]
    # Soporta formas como 1ero, 1er, 1ro, 2do, etc.
    m = re.fullmatch(r"(\d+)(?:er|ero|ra|ro|do|da|to|ta|mo|ma|vo|va)?", t)
    if m:
        return m.group(1)
    return t


def _norm_id_text(s: Any) -> str:
    t = _norm(s)
    t = re.sub(r"r\W*o\W*c", "roc", t)
    t = re.sub(r"r\W*n\W*v\W*d", "rnvd", t)
    return t


def _extract_identifiers(text: Any) -> set[str]:
    if not text:
        return set()
    t = _norm_id_text(text)
    tokens: set[str] = set()
    for m in re.finditer(r"\brnvd[\s\-:]*([0-9]{4,})\b", t):
        num = m.group(1)
        tokens.add(num)
        tokens.add(f"rnvd-{num}")
    for m in re.finditer(r"\broc(?:\s*no\.?)?[\s\-:]*([0-9]{4,})\b", t):
        num = m.group(1)
        tokens.add(num)
        tokens.add(f"roc-{num}")
    for m in re.finditer(r"\b[0-9]{5,}\b", t):
        tokens.add(m.group(0))
    return tokens


def _check_identifier(expected: str, got: Optional[str], *, label: str) -> Dict[str, Any]:
    if not expected:
        return {"field": label, "ok": False, "error": "Dato esperado vacio"}
    if not got:
        return {"field": label, "ok": False, "error": "No encontrado"}
    exp_tokens = _extract_identifiers(expected)
    got_tokens = _extract_identifiers(got)
    matched = exp_tokens & got_tokens
    ok = bool(matched)
    if not ok:
        ne = _norm_id_text(expected)
        ng = _norm_id_text(got)
        if ne and ng and (ne in ng or ng in ne):
            ok = True
    return {
        "field": label,
        "ok": ok,
        "expected": expected,
        "got": got,
        "matched": sorted(matched),
    }


def _normalize_address(text: Any) -> str:
    t = _norm(text)
    # Unifica marcadores frecuentes en documentos: "# 1" -> "numero 1"
    t = t.replace("#", " numero ")
    t = re.sub(r"\bkm\.?\b", "kilometro", t)
    # Separa tokens unidos por OCR/LLM: "75vrs" <-> "75 vrs"
    t = re.sub(r"(?<=\d)(?=[a-z])", " ", t)
    t = re.sub(r"(?<=[a-z])(?=\d)", " ", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    toks = [_canonicalize_address_token(tok) for tok in t.split()]
    return " ".join(tok for tok in toks if tok)


def _address_similarity(expected: str, got: str) -> Dict[str, float]:
    ne = _normalize_address(expected)
    ng = _normalize_address(got)
    if not ne or not ng:
        return {"score": 0.0, "ratio": 0.0, "coverage": 0.0, "contain": 0.0}
    stop = {
        "de", "del", "la", "el", "y", "a", "al", "los", "las",
        "no", "numero", "num", "municipio", "departamento", "depto",
        "dpto", "provincia", "distrito", "dist", "barrio", "colonia",
        "sector", "zona", "ciudad", "n", "nro",
    }
    exp_tokens = [t for t in ne.split() if t and t not in stop]
    got_tokens = [t for t in ng.split() if t and t not in stop]
    exp_filtered = " ".join(exp_tokens)
    got_filtered = " ".join(got_tokens)

    ratio = SequenceMatcher(None, exp_filtered or ne, got_filtered or ng).ratio()
    token_ratio = SequenceMatcher(
        None,
        " ".join(sorted(set(exp_tokens))),
        " ".join(sorted(set(got_tokens))),
    ).ratio()
    coverage = 0.0
    if exp_tokens:
        got_set = set(got_tokens)
        coverage = len([t for t in exp_tokens if t in got_set]) / len(exp_tokens)

    contain = 0.0
    if exp_filtered and got_filtered:
        if exp_filtered in got_filtered or got_filtered in exp_filtered:
            contain = 1.0
    elif ne and ng and (ne in ng or ng in ne):
        contain = 1.0

    # Control adicional: consistencia de números de dirección (casa, km, etc.)
    exp_nums = set(re.findall(r"\b\d+\b", ne))
    got_nums = set(re.findall(r"\b\d+\b", ng))
    if exp_nums:
        numeric_coverage = len(exp_nums & got_nums) / len(exp_nums)
    else:
        # Neutral cuando no hay números esperados
        numeric_coverage = 1.0

    return {
        "score": max(ratio, token_ratio, coverage, contain),
        "ratio": ratio,
        "token_ratio": token_ratio,
        "coverage": coverage,
        "contain": contain,
        "numeric_coverage": numeric_coverage,
    }


def _check_address(expected: str, got: Optional[str], *, label: str, threshold: float = 0.75) -> Dict[str, Any]:
    if not expected:
        return {"field": label, "ok": False, "error": "Dato esperado vacio"}
    if not got:
        return {"field": label, "ok": False, "error": "No encontrado"}
    sim = _address_similarity(expected, got)
    # Requiere similitud textual y coherencia razonable de números si existen.
    numeric_ok = sim.get("numeric_coverage", 1.0) >= 0.5
    return {
        "field": label,
        "ok": (sim["score"] >= threshold) and numeric_ok,
        "expected": expected,
        "got": got,
        "similarity": sim,
    }


def _check(expected: str, got: Optional[str], *, label: str) -> Dict[str, Any]:
    if not expected:
        return {"field": label, "ok": False, "error": "Dato esperado vacío"}
    if not got:
        return {"field": label, "ok": False, "error": "No encontrado"}
    if label == "cedula":
        ok = _norm(expected) == _norm(got)
        return {"field": label, "ok": ok, "expected": expected, "got": got}
    return {"field": label, "ok": _norm(expected) == _norm(got), "expected": expected, "got": got}


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _check_fuzzy(expected: str, got: Optional[str], *, label: str, threshold: float = 0.75) -> Dict[str, Any]:
    if not expected:
        return {"field": label, "ok": False, "error": "Dato esperado vacío"}
    if not got:
        return {"field": label, "ok": False, "error": "No encontrado"}
    s = _similar(expected, got)
    return {"field": label, "ok": s >= threshold, "expected": expected, "got": got, "similarity": s}


def _tokenize_name(text: str) -> set[str]:
    t = _norm(text)
    for ch in [",", ".", "-", ":", ";", "="]:
        t = t.replace(ch, " ")
    toks = [w for w in t.split() if w and w not in {"de", "del", "la", "el", "y"}]
    return set(toks)


def _check_name(expected_full: str, _unused: str, got: Optional[str], *, policy: str = "balanced") -> Dict[str, Any]:
    label = "nombre"
    if not got:
        return {"field": label, "ok": False, "error": "No encontrado"}
    toks_exp = _tokenize_name(expected_full or "")
    toks_got = _tokenize_name(got or "")

    def cov(a: set[str], b: set[str]) -> float:
        return 0.0 if not a else len(a & b) / max(1, len(a))

    c_full = cov(toks_exp, toks_got)
    ok = c_full >= (0.6 if policy == "balanced" else 0.8)
    return {"field": label, "ok": ok, "expected": expected_full, "got": got, "coverage": {"full": c_full}, "policy": policy}


def validate_cedula_vision(
    df_cert,
    *,
    cedula_front: Optional[str] = None,
    cedula_back: Optional[str] = None,
    model: str = "gpt-4o-mini",
) -> Dict[str, Any]:
    """Valida cédula contra la hoja 'Certificacion' usando LLM Visión.

    Retorna dict: { ok, checks: [...], fields: {...} }
    """
    cert = extract_cert_fields(df_cert)
    expected_ced = str(cert.get("cedula") or "")
    expected_full = str(
        cert.get("nombre_completo") or f"{cert.get('nombre') or ''} {cert.get('apellido') or ''}"
    ).strip()

    if not cedula_front:
        return {"ok": False, "checks": [], "fields": {}, "error": "Falta imagen de frente"}

    res = extract_cedula_with_vision(cedula_front, cedula_back, model=model)
    fields = res.get("fields", {}) if isinstance(res, dict) else {}

    got_full = (fields.get("nombre_completo") or ((fields.get("nombres") or "") + " " + (fields.get("apellidos") or ""))).strip()

    checks = [
        {"doc": "cedula_vision", **_check(expected_ced, fields.get("cedula"), label="cedula")},
        {"doc": "cedula_vision", **_check_name(expected_full, "", got_full, policy="balanced")},
    ]
    ok = all(c.get("ok", False) for c in checks)
    return {"ok": ok, "checks": checks, "fields": fields}


def _extract_expected_from_cert(df_cert) -> Dict[str, Optional[str]]:
    """Extrae campos relevantes desde la hoja Certificacion sin depender de utils.
    Busca sinónimos comunes: direccion_negocio, ruc, matricula/roc.
    """
    out = {"direccion_negocio": None, "ruc": None, "matricula_num": None}
    try:
        import pandas as pd  # type: ignore
        def n(s):
            return _norm(s)
        for i in range(len(df_cert.index)):
            key = n(df_cert.iloc[i, 0])
            val = df_cert.iloc[i, 1]
            if not key:
                continue
            if any(k in key for k in ["direccion del negocio", "direccion negocio", "direccion"]):
                out["direccion_negocio"] = str(val)
            if any(k in key for k in ["ruc", "no ruc", "numero ruc", "num ruc"]):
                out["ruc"] = str(val)
            if any(k in key for k in ["matricula", "roc", "registro contable", "r.o.c"]):
                out["matricula_num"] = str(val)
    except Exception:
        pass
    return out


def validate_matricula_vision(
    df_cert,
    *,
    matricula_path: str,
    model: str = "gpt-4o-mini",
    address_threshold: float = 0.75,
) -> Dict[str, Any]:
    """Valida matrícula/registro contable contra la hoja 'Certificacion' usando LLM Visión.

    Compara:
      - Número de matrícula/ROC (si existe en Certificación) vs 'roc' del documento
      - Dirección del negocio vs 'direccion' extraída (fuzzy)
    """
    if not matricula_path:
        return {"ok": False, "checks": [], "fields": {}, "error": "Falta imagen de matrícula"}

    cert = extract_cert_fields(df_cert)
    # Extraer dirección esperada de utils y, si falta, usar escaneo directo
    expected_addr = str(cert.get("direccion_negocio") or "")
    extras = _extract_expected_from_cert(df_cert)
    expected_matricula = extras.get("matricula_num")
    expected_ruc = extras.get("ruc")

    res = extract_matricula_with_vision(matricula_path, model=model)
    fields = res.get("fields", {}) if isinstance(res, dict) else {}

    checks = []
    # Número de matrícula (si existe en Certificación)
    if expected_matricula:
        checks.append({"doc": "matricula_vision", **_check_identifier(expected_matricula, fields.get("roc"), label="matricula")})
    # Dirección (fuzzy)
    checks.append({"doc": "matricula_vision", **_check_address(expected_addr, fields.get("direccion"), label="direccion", threshold=address_threshold)})
    # RUC opcional (si disponible)
    if expected_ruc and fields.get("ruc"):
        checks.append({"doc": "matricula_vision", **_check(expected_ruc, fields.get("ruc"), label="ruc")})

    ok = True
    for c in checks:
        if not c.get("ok", False):
            ok = False
            break
    return {"ok": ok, "checks": checks, "fields": fields}

from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any, Dict, Mapping, Optional

from dotenv import load_dotenv

from llm_vision import _images_to_content, _pdf_to_images


NOT_VISIBLE = "No visible en la imagen"
DEFAULT_DOCUMENT_MODEL = "gpt-5-mini"
DEFAULT_DOCUMENT_FALLBACK_MODEL = "gpt-4o"


def extract_client_documents(
    *,
    cedula_front: Optional[str] = None,
    cedula_back: Optional[str] = None,
    matricula_path: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_side_px: int = 1800,
) -> Dict[str, Any]:
    """Extract app client fields from cedula and matricula support images."""
    load_dotenv()
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        return {"ok": False, "error": "Falta OPENAI_API_KEY para extraer datos desde imagenes."}
    if not any([cedula_front, cedula_back, matricula_path]):
        return {"ok": False, "error": "Adjunte al menos una imagen de cedula o matricula."}

    from openai import OpenAI  # type: ignore

    image_paths: list[str] = []
    temps: list[str] = []

    def add_image(label: str, path: Optional[str]) -> None:
        if not path:
            return
        image_paths.append(f"__LABEL__:{label}")
        if path.lower().endswith(".pdf"):
            converted = _pdf_to_images([path])
            temps.extend(converted)
            image_paths.extend(converted)
        else:
            image_paths.append(path)

    add_image("Cedula de identidad - anverso/frente", cedula_front)
    add_image("Cedula de identidad - reverso", cedula_back)
    add_image("Constancia de matricula comercial", matricula_path)

    content: list[dict[str, Any]] = [{"type": "text", "text": _document_prompt()}]
    current_batch: list[str] = []
    for item in image_paths:
        if item.startswith("__LABEL__:"):
            if current_batch:
                content += _images_to_content(current_batch, max_side_px=max_side_px)
                current_batch = []
            content.append({"type": "text", "text": item.replace("__LABEL__:", "")})
        else:
            current_batch.append(item)
    if current_batch:
        content += _images_to_content(current_batch, max_side_px=max_side_px)

    client = OpenAI(api_key=key)
    try:
        document_model = model or os.getenv("OPENAI_MODEL_DOCUMENTS", DEFAULT_DOCUMENT_MODEL)
        resp = _chat_completion_json(
            client,
            model=document_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Actua como analista documental especializado en documentos oficiales de Nicaragua. "
                        "Extrae solo informacion visible, no inventes y devuelve JSON valido segun el esquema."
                    ),
                },
                {"role": "user", "content": content},
            ],
            response_format={"type": "json_schema", "json_schema": _document_schema()},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        verification = _verify_cedula_name(
            client,
            cedula_front=cedula_front,
            cedula_back=cedula_back,
            model=document_model,
            max_side_px=max_side_px,
        )
        if verification:
            data["name_verification"] = verification
            _apply_name_verification(data)
        patch = build_client_patch(data)
        return {
            "ok": True,
            "documents": data.get("documents") or {},
            "name_verification": data.get("name_verification") or {},
            "client_patch": patch,
            "raw": data,
        }
    except Exception as exc:
        return {"ok": False, "error": f"No se pudo extraer datos: {type(exc).__name__}: {exc}"}
    finally:
        for temp in temps:
            try:
                os.remove(temp)
            except Exception:
                pass


def _verify_cedula_name(
    client: Any,
    *,
    cedula_front: Optional[str],
    cedula_back: Optional[str],
    model: str,
    max_side_px: int,
) -> dict[str, Any]:
    if not cedula_front and not cedula_back:
        return {}
    paths: list[str] = []
    temps: list[str] = []

    def add(label: str, path: Optional[str]) -> None:
        if not path:
            return
        paths.append(f"__LABEL__:{label}")
        if path.lower().endswith(".pdf"):
            converted = _pdf_to_images([path])
            temps.extend(converted)
            paths.extend(converted)
        else:
            paths.append(path)

    add("Cedula de identidad - anverso/frente", cedula_front)
    add("Cedula de identidad - reverso", cedula_back)

    content: list[dict[str, Any]] = [{"type": "text", "text": _name_verification_prompt()}]
    batch: list[str] = []
    try:
        for item in paths:
            if item.startswith("__LABEL__:"):
                if batch:
                    content += _images_to_content(batch, max_side_px=max_side_px)
                    batch = []
                content.append({"type": "text", "text": item.replace("__LABEL__:", "")})
            else:
                batch.append(item)
        if batch:
            content += _images_to_content(batch, max_side_px=max_side_px)
        resp = _chat_completion_json(
            client,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Actua como verificador documental. Lee solo el bloque Nombres/Apellidos visible "
                        "en cedulas nicaraguenses y devuelve JSON valido."
                    ),
                },
                {"role": "user", "content": content},
            ],
            response_format={"type": "json_schema", "json_schema": _name_verification_schema()},
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        return {}
    finally:
        for temp in temps:
            try:
                os.remove(temp)
            except Exception:
                pass


def _chat_completion_json(client: Any, *, model: str, messages: list[dict[str, Any]], response_format: dict[str, Any]) -> Any:
    fallback = os.getenv("OPENAI_MODEL_DOCUMENTS_FALLBACK", DEFAULT_DOCUMENT_FALLBACK_MODEL)
    models = [item for item in [model, fallback] if item]
    unique_models = list(dict.fromkeys(models))
    last_exc: Exception | None = None
    for candidate in unique_models:
        try:
            return _chat_completion_json_once(
                client,
                model=candidate,
                messages=messages,
                response_format=response_format,
            )
        except Exception as exc:
            last_exc = exc
            if not _is_model_unavailable_error(exc):
                raise
    if last_exc:
        raise last_exc
    raise RuntimeError("No hay modelo de documentos configurado.")


def _chat_completion_json_once(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    response_format: dict[str, Any],
) -> Any:
    kwargs = {
        "model": model,
        "messages": messages,
        "response_format": response_format,
        "temperature": 0,
    }
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as exc:
        message = str(exc).lower()
        if "temperature" not in message and "unsupported" not in message:
            raise
        kwargs.pop("temperature", None)
        return client.chat.completions.create(**kwargs)


def _is_model_unavailable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(fragment in text for fragment in ["model_not_found", "does not have access to model", "model `"])


def build_client_patch(extraction: Mapping[str, Any]) -> Dict[str, Any]:
    documents = extraction.get("documents") if isinstance(extraction, Mapping) else {}
    cedula = (documents or {}).get("cedula") or {}
    matricula = (documents or {}).get("matricula") or {}
    name_meta = _name_metadata(extraction)
    patch = {
        "nombre_completo": name_meta.get("nombre_completo")
        or _normalize_person_name(_clean(matricula.get("nombre_contribuyente"))),
        "nombres_raw": name_meta.get("nombres_raw"),
        "apellidos_raw": name_meta.get("apellidos_raw"),
        "name_review_required": name_meta.get("name_review_required"),
        "name_review_reason": name_meta.get("name_review_reason"),
        "name_candidates": name_meta.get("name_candidates"),
        "selected_name_source": name_meta.get("selected_name_source"),
        "cedula": _format_cedula(_clean(cedula.get("numero_cedula")) or _clean(matricula.get("ruc"))),
        "fecha_nacimiento": _normalize_date(_clean(cedula.get("fecha_nacimiento"))),
        "lugar_nacimiento": _normalize_person_name(_clean(cedula.get("lugar_nacimiento"))),
        "fecha_emision_cedula": _normalize_date(_clean(cedula.get("fecha_emision"))),
        "fecha_expiracion_cedula": _normalize_date(_clean(cedula.get("fecha_expiracion"))),
        "sexo": _normalize_sex(_clean(cedula.get("sexo"))),
        "domicilio": _normalize_domicilio(_clean(cedula.get("domicilio_formal"))),
        "direccion_personal": _normalize_address_text(_clean(cedula.get("direccion_formal"))),
        "regimen": _clean(matricula.get("modalidad")),
        "matricula": _build_matricula_summary(matricula),
        "direccion_negocio": _normalize_address_text(_clean(matricula.get("direccion_negocio_formal"))),
        "giro_negocio": _clean(matricula.get("actividad_economica")),
    }
    return {key: value for key, value in patch.items() if value is not None and value != "" and value != []}


def _apply_name_verification(extraction: dict[str, Any]) -> None:
    documents = extraction.setdefault("documents", {})
    cedula = documents.setdefault("cedula", {})
    meta = _name_metadata(extraction)
    if meta.get("nombre_completo"):
        cedula["nombre_completo"] = meta["nombre_completo"]
    cedula["nombres_raw"] = meta.get("nombres_raw") or cedula.get("nombres_raw")
    cedula["apellidos_raw"] = meta.get("apellidos_raw") or cedula.get("apellidos_raw")
    cedula["name_review_required"] = bool(meta.get("name_review_required"))
    cedula["name_review_reason"] = meta.get("name_review_reason") or None
    cedula["name_candidates"] = meta.get("name_candidates") or []
    cedula["source_quality"] = "needs_review" if meta.get("name_review_required") else "verified"


def _name_metadata(extraction: Mapping[str, Any]) -> dict[str, Any]:
    documents = extraction.get("documents") if isinstance(extraction, Mapping) else {}
    cedula = (documents or {}).get("cedula") or {}
    matricula = (documents or {}).get("matricula") or {}
    verification = extraction.get("name_verification") if isinstance(extraction, Mapping) else {}

    primary = _candidate_from_parts(
        "cedula_general",
        _clean(cedula.get("nombres_raw")),
        _clean(cedula.get("apellidos_raw")),
        _clean(cedula.get("nombre_completo")),
    )
    verified = _candidate_from_parts(
        "cedula_verificacion",
        _clean((verification or {}).get("nombres_raw")),
        _clean((verification or {}).get("apellidos_raw")),
        _clean((verification or {}).get("nombre_completo")),
    )
    matricula_candidate = _candidate_from_parts(
        "matricula",
        "",
        "",
        _clean(matricula.get("nombre_contribuyente")),
    )

    candidates = _dedupe_name_candidates(
        _candidate_list(cedula.get("name_candidates")) + [primary, verified, matricula_candidate]
    )
    primary_name = primary.get("nombre_completo") if primary else ""
    verified_name = verified.get("nombre_completo") if verified else ""
    matricula_name = matricula_candidate.get("nombre_completo") if matricula_candidate else ""
    chosen = verified or primary or matricula_candidate or {}
    reasons: list[str] = []

    if _truthy(cedula.get("name_review_required")):
        reasons.append(_clean(cedula.get("name_review_reason")) or "La extraccion general marco el nombre para revision.")
    if primary_name and verified_name and _name_compare_key(primary_name) != _name_compare_key(verified_name):
        reasons.append("La lectura general y la verificacion enfocada no coincidieron.")
    if primary_name and matricula_name and _name_compare_key(primary_name) != _name_compare_key(matricula_name):
        reasons.append("El nombre de la cedula y el nombre de la matricula no coinciden.")
    if (primary_name or verified_name) and not matricula_name and not _name_has_independent_confirmation(candidates):
        reasons.append("No hay una fuente independiente que confirme el nombre extraido; revise contra la imagen.")

    return {
        "nombre_completo": chosen.get("nombre_completo") or "",
        "nombres_raw": chosen.get("nombres_raw") or "",
        "apellidos_raw": chosen.get("apellidos_raw") or "",
        "name_review_required": bool(reasons),
        "name_review_reason": " ".join(dict.fromkeys(reasons)),
        "name_candidates": candidates,
        "selected_name_source": chosen.get("source") or "",
    }


def _document_prompt() -> str:
    return f"""
Extrae la informacion visible de las imagenes adjuntas.

Reglas generales:
- Leer UNICAMENTE informacion visible en la imagen.
- NO inventar, NO asumir, NO completar datos faltantes.
- Si un dato no aparece, usa exactamente "{NOT_VISIBLE}".
- Convierte abreviaturas de direccion a lenguaje formal nicaraguense cuando sea claro: RES. -> Residencial, BO. -> Barrio.
- Mantener nombres propios como aparecen; solo agrega tildes evidentes cuando corresponda.
- No extraigas el nombre de matricula en mayuscula si puedes normalizarlo a nombre propio.
- En cedulas, el frente suele traer "Nombres" y "Apellidos" en lineas separadas. El campo nombre_completo debe ser
  Nombres + Apellidos, en ese orden, y en formato de nombre propio, no todo en mayusculas.
- Devuelve tambien nombres_raw y apellidos_raw separados exactamente desde esas lineas cuando sean visibles.
- Si la lectura del nombre no es clara, no inventes: llena name_review_required=true y explica la duda.
- Lee nombres y apellidos caracter por caracter. No sustituyas letras visibles por apellidos "parecidos";
  si hay duda, conserva exactamente la secuencia visible en la imagen.
- Devuelve fechas visibles en formato ISO YYYY-MM-DD. Si la imagen muestra 28-01-1999, devuelve 1999-01-28.

Para cedula extrae: nombre completo, numero de cedula, fecha de nacimiento, lugar de nacimiento, sexo, direccion formal,
domicilio formal con municipio y departamento, fecha de emision y fecha de expiracion.

Para matricula extrae: alcaldia, anio, nombre del contribuyente, modalidad, RUC, cuenta fiscal, direccion del negocio formal,
actividad economica exacta, distrito, ROC, fecha de emision, fecha de constancia, codigo interno y resumen de una linea
con formato: CODIGO; ROC No. NUMERO.
""".strip()


def _name_verification_prompt() -> str:
    return """
Lee solo el bloque de la cedula donde aparecen las etiquetas "Nombres" y "Apellidos".

Reglas:
- Devuelve nombres_raw exactamente con los nombres visibles.
- Devuelve apellidos_raw exactamente con los apellidos visibles.
- Construye nombre_completo como nombres_raw + apellidos_raw.
- Lee caracter por caracter. No sustituyas apellidos por palabras parecidas.
- Si una letra no es confiable, manten la mejor lectura visible y agrega esa posicion en uncertain_characters.
- Si el bloque no se ve, usa "No visible en la imagen".
""".strip()


def _document_schema() -> Dict[str, Any]:
    string_or_null = {"type": ["string", "null"]}
    candidate_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source": string_or_null,
            "nombres_raw": string_or_null,
            "apellidos_raw": string_or_null,
            "nombre_completo": string_or_null,
        },
        "required": ["source", "nombres_raw", "apellidos_raw", "nombre_completo"],
    }
    cedula_props = {
        "nombres_raw": string_or_null,
        "apellidos_raw": string_or_null,
        "nombre_completo": string_or_null,
        "numero_cedula": string_or_null,
        "fecha_nacimiento": string_or_null,
        "lugar_nacimiento": string_or_null,
        "sexo": string_or_null,
        "direccion_formal": string_or_null,
        "domicilio_formal": string_or_null,
        "fecha_emision": string_or_null,
        "fecha_expiracion": string_or_null,
        "source_quality": string_or_null,
        "name_review_required": {"type": "boolean"},
        "name_review_reason": string_or_null,
        "name_candidates": {"type": "array", "items": candidate_schema},
    }
    matricula_props = {
        "alcaldia": string_or_null,
        "anio": string_or_null,
        "nombre_contribuyente": string_or_null,
        "modalidad": string_or_null,
        "ruc": string_or_null,
        "cuenta_fiscal": string_or_null,
        "direccion_negocio_formal": string_or_null,
        "actividad_economica": string_or_null,
        "distrito": string_or_null,
        "roc": string_or_null,
        "fecha_emision": string_or_null,
        "fecha_constancia": string_or_null,
        "codigo_interno": string_or_null,
        "resumen_linea": string_or_null,
    }
    return {
        "name": "nicaragua_document_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "documents": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "cedula": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": cedula_props,
                            "required": list(cedula_props),
                        },
                        "matricula": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": matricula_props,
                            "required": list(matricula_props),
                        },
                    },
                    "required": ["cedula", "matricula"],
                }
            },
            "required": ["documents"],
        },
    }


def _name_verification_schema() -> Dict[str, Any]:
    string_or_null = {"type": ["string", "null"]}
    return {
        "name": "nicaragua_cedula_name_verification",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "nombres_raw": string_or_null,
                "apellidos_raw": string_or_null,
                "nombre_completo": string_or_null,
                "uncertain_characters": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["nombres_raw", "apellidos_raw", "nombre_completo", "uncertain_characters"],
        },
    }


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == NOT_VISIBLE.lower():
        return ""
    return " ".join(text.split())


def _candidate_from_parts(source: str, nombres_raw: str, apellidos_raw: str, nombre_completo: str) -> dict[str, str]:
    names = _clean(nombres_raw)
    surnames = _clean(apellidos_raw)
    full_from_parts = " ".join(part for part in [names, surnames] if part).strip()
    full = full_from_parts or _clean(nombre_completo)
    if not full:
        return {}
    return {
        "source": source,
        "nombres_raw": _normalize_person_name(names) if names else "",
        "apellidos_raw": _normalize_person_name(surnames) if surnames else "",
        "nombre_completo": _normalize_person_name(full),
    }


def _candidate_list(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        candidate = _candidate_from_parts(
            _clean(item.get("source")) or "cedula_candidate",
            _clean(item.get("nombres_raw")),
            _clean(item.get("apellidos_raw")),
            _clean(item.get("nombre_completo")),
        )
        if candidate:
            out.append(candidate)
    return out


def _dedupe_name_candidates(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for candidate in candidates:
        if not candidate:
            continue
        key = f"{candidate.get('source')}::{_name_compare_key(candidate.get('nombre_completo', ''))}"
        if not key.endswith("::") and key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "si", "sí", "yes"}


def _name_compare_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _name_has_independent_confirmation(candidates: list[dict[str, str]]) -> bool:
    return any(
        _name_compare_key(candidate.get("nombre_completo", ""))
        for candidate in candidates
        if candidate.get("source") in {"cedula_mrz", "matricula"}
    )


def _normalize_sex(value: str) -> str:
    text = value.strip().lower()
    if text in {"f", "femenino", "mujer"}:
        return "Femenino"
    if text in {"m", "masculino", "hombre"}:
        return "Masculino"
    return value


def _format_cedula(value: str) -> str:
    text = value.strip().upper()
    compact = "".join(ch for ch in text if ch.isalnum())
    if len(compact) == 14 and compact[:13].isdigit():
        return f"{compact[:3]}-{compact[3:9]}-{compact[9:]}"
    return text


def _normalize_date(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    iso = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if iso:
        year, month, day = iso.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    numeric = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", text)
    if numeric:
        day, month, year = numeric.groups()
        year_int = int(year)
        if year_int < 100:
            year_int += 2000 if year_int < 40 else 1900
        return f"{year_int:04d}-{int(month):02d}-{int(day):02d}"
    months = {
        "enero": 1,
        "febrero": 2,
        "marzo": 3,
        "abril": 4,
        "mayo": 5,
        "junio": 6,
        "julio": 7,
        "agosto": 8,
        "septiembre": 9,
        "setiembre": 9,
        "octubre": 10,
        "noviembre": 11,
        "diciembre": 12,
    }
    words = re.fullmatch(r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})", text.lower())
    if words and words.group(2) in months:
        return f"{int(words.group(3)):04d}-{months[words.group(2)]:02d}-{int(words.group(1)):02d}"
    return text


def _build_matricula_summary(matricula: Mapping[str, Any]) -> str:
    explicit = _clean(matricula.get("resumen_linea"))
    code = _extract_internal_code(_clean(matricula.get("codigo_interno")) or explicit)
    roc = _extract_roc(_clean(matricula.get("roc")) or explicit)
    if not roc:
        roc = _extract_roc(explicit)
    if code and roc:
        return f"{code}; ROC No. {roc}"
    return code or (f"ROC No. {roc}" if roc else "")


def _extract_internal_code(value: str) -> str:
    text = value.upper()
    match = re.search(r"\b(?:RNVD|RENNEG|MRH|REN|RMC|MRC)[-\s]*\d{3,}\b", text)
    if match:
        return _normalize_internal_code(match.group(0))
    match = re.search(r"\b[A-Z]{2,10}[-\s]*\d{3,}\b", text)
    if match:
        return _normalize_internal_code(match.group(0))
    return ""


def _normalize_internal_code(value: str) -> str:
    text = re.sub(r"\s+", "", value.upper())
    match = re.fullmatch(r"([A-Z]{2,10})-?(\d{3,})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return text


def _extract_roc(value: str) -> str:
    text = value.upper()
    match = re.search(r"R\.?\s*O\.?\s*C\.?\s*(?:NO\.?)?\s*[:#-]?\s*(\d{4,})", text)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{7,})\b", text)
    return match.group(1) if match else ""


def _normalize_domicilio(value: str) -> str:
    text = _normalize_address_text(value)
    normalized = text.lower()
    if "managua" in normalized:
        return "Municipio de Managua, Departamento de Managua."
    if "municipio" in normalized and "departamento" in normalized:
        return text
    parts = [part.strip(" .") for part in text.split(",") if part.strip(" .")]
    if len(parts) >= 2:
        return f"Municipio de {parts[-2]}, Departamento de {parts[-1]}."
    return text


def _normalize_address_text(value: str) -> str:
    text = value.strip()
    replacements = [
        (r"\bRESD?\.?\b", "Residencial"),
        (r"\bBO\.?\b", "Barrio"),
        (r"\b1\s*C\.?\b", "1 cuadra"),
        (r"\b1/2\s*C\.?\b", "media cuadra"),
        (r"½\s*C\.?", "media cuadra"),
        (r"\bN\.?\b", "norte"),
        (r"\bS\.?\b", "sur"),
        (r"\bE\.?\b", "este"),
        (r"\bO\.?\b", "oeste"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return _normalize_person_name(" ".join(text.split()))


def _normalize_person_name(value: str) -> str:
    text = _title_case_person_name(value)
    replacements = {
        "Gonzalez": "González",
        "Chavarria": "Chavarría",
        "Economia": "Economía",
        "Garcia": "García",
        "Martinez": "Martínez",
        "Lopez": "López",
        "Perez": "Pérez",
        "Sanchez": "Sánchez",
        "Hernandez": "Hernández",
        "Rodriguez": "Rodríguez",
        "Ramirez": "Ramírez",
    }
    for plain, accented in replacements.items():
        text = re.sub(rf"\b{plain}\b", accented, text, flags=re.IGNORECASE)
    return text


def _title_case_person_name(value: str) -> str:
    particles = {"de", "del", "la", "las", "los", "y"}
    words = [word for word in str(value or "").strip().split() if word]
    normalized: list[str] = []
    for index, word in enumerate(words):
        parts = word.split("-")
        titled_parts = []
        for part in parts:
            lower = part.lower()
            if index > 0 and lower in particles:
                titled_parts.append(lower)
            else:
                titled_parts.append(lower[:1].upper() + lower[1:])
        normalized.append("-".join(titled_parts))
    return " ".join(normalized)

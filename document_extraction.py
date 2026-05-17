from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Mapping, Optional

from dotenv import load_dotenv

from llm_vision import _images_to_content, _pdf_to_images


NOT_VISIBLE = "No visible en la imagen"


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
        resp = client.chat.completions.create(
            model=model or os.getenv("OPENAI_MODEL_DOCUMENTS", "gpt-4o-mini"),
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
            temperature=0,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        patch = build_client_patch(data)
        return {
            "ok": True,
            "documents": data.get("documents") or {},
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


def build_client_patch(extraction: Mapping[str, Any]) -> Dict[str, str]:
    documents = extraction.get("documents") if isinstance(extraction, Mapping) else {}
    cedula = (documents or {}).get("cedula") or {}
    matricula = (documents or {}).get("matricula") or {}
    patch = {
        "nombre_completo": _normalize_person_name(
            _clean(cedula.get("nombre_completo")) or _clean(matricula.get("nombre_contribuyente"))
        ),
        "cedula": _format_cedula(_clean(cedula.get("numero_cedula")) or _clean(matricula.get("ruc"))),
        "sexo": _normalize_sex(_clean(cedula.get("sexo"))),
        "domicilio": _normalize_domicilio(_clean(cedula.get("domicilio_formal"))),
        "direccion_personal": _normalize_address_text(_clean(cedula.get("direccion_formal"))),
        "regimen": _clean(matricula.get("modalidad")),
        "matricula": _build_matricula_summary(matricula),
        "direccion_negocio": _normalize_address_text(_clean(matricula.get("direccion_negocio_formal"))),
        "giro_negocio": _clean(matricula.get("actividad_economica")),
    }
    return {key: value for key, value in patch.items() if value}


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

Para cedula extrae: nombre completo, numero de cedula, fecha de nacimiento, lugar de nacimiento, sexo, direccion formal,
domicilio formal con municipio y departamento, fecha de emision y fecha de expiracion.

Para matricula extrae: alcaldia, anio, nombre del contribuyente, modalidad, RUC, cuenta fiscal, direccion del negocio formal,
actividad economica exacta, distrito, ROC, fecha de emision, fecha de constancia, codigo interno y resumen de una linea
con formato: CODIGO; ROC No. NUMERO.
""".strip()


def _document_schema() -> Dict[str, Any]:
    string_or_null = {"type": ["string", "null"]}
    cedula_props = {
        "nombre_completo": string_or_null,
        "numero_cedula": string_or_null,
        "fecha_nacimiento": string_or_null,
        "lugar_nacimiento": string_or_null,
        "sexo": string_or_null,
        "direccion_formal": string_or_null,
        "domicilio_formal": string_or_null,
        "fecha_emision": string_or_null,
        "fecha_expiracion": string_or_null,
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


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == NOT_VISIBLE.lower():
        return ""
    return " ".join(text.split())


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


def _build_matricula_summary(matricula: Mapping[str, Any]) -> str:
    explicit = _clean(matricula.get("resumen_linea"))
    if explicit and _extract_internal_code(explicit):
        return explicit
    code = _extract_internal_code(_clean(matricula.get("codigo_interno")) or explicit)
    roc = _extract_roc(_clean(matricula.get("roc")) or explicit)
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
    text = value
    for plain, accented in replacements.items():
        text = re.sub(rf"\b{plain}\b", accented, text, flags=re.IGNORECASE)
    return text

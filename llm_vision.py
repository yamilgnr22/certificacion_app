from __future__ import annotations

import base64
import io
import mimetypes
import os
import tempfile
from typing import Dict, List, Optional

from dotenv import load_dotenv


def _b64_data_url(path: str, *, max_side_px: Optional[int] = None, jpeg_quality: int = 90) -> str:
    """Devuelve un data URL base64 para una imagen.
    Si `max_side_px` está definido, reescala con Pillow y codifica como JPEG
    optimizado para reducir el tamaño.
    """
    if max_side_px:
        try:
            from PIL import Image  # type: ignore
            with Image.open(path) as im:
                im = im.convert("RGB")
                w, h = im.size
                m = max(w, h)
                if m > max_side_px:
                    scale = max_side_px / float(m)
                    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                return f"data:image/jpeg;base64,{b64}"
        except Exception:
            # Fallback a lectura directa si Pillow no está disponible o falla
            pass

    mime, _ = mimetypes.guess_type(path)
    mime = mime or "image/jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _pdf_to_images(paths: List[str]) -> List[str]:
    """Convierte la primera página de cada PDF a imágenes JPEG temporales.
    Devuelve rutas a archivos que deben limpiarse tras su uso.
    """
    try:
        from pdf2image import convert_from_path  # type: ignore
    except Exception:
        return []
    poppler_path = os.getenv("POPPLER_PATH")
    out_files: List[str] = []
    for p in paths:
        try:
            imgs = convert_from_path(
                p, dpi=300, first_page=1, last_page=1,
                poppler_path=poppler_path if poppler_path else None,
            )
            if imgs:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                    tmp_path = tmp.name
                imgs[0].save(tmp_path, format="JPEG", quality=95)
                out_files.append(tmp_path)
        except Exception:
            continue
    return out_files


def _images_to_content(paths: List[str], *, max_side_px: Optional[int] = None) -> List[dict]:
    content: List[dict] = []
    for path in paths:
        content.append({
            "type": "image_url",
            "image_url": {"url": _b64_data_url(path, max_side_px=max_side_px)},
        })
    return content


def extract_cedula_with_vision(
    front_path: str,
    back_path: Optional[str] = None,
    *,
    model: str = "gpt-4o-mini",
    api_key: Optional[str] = None,
    max_side_px: Optional[int] = 1600,
) -> Dict:
    """
    Extrae campos de cédula nicaragüense usando un modelo con visión.
    - Acepta imágenes (JPG/PNG/TIFF) y PDF (convierte 1ra página a imagen).
    - Reescala opcionalmente las imágenes a `max_side_px` (por defecto 1600 px)
      para reducir latencia/costo.
    Retorna: {ok, fields|error}
    """
    load_dotenv()
    from openai import OpenAI  # type: ignore

    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        return {"ok": False, "error": "Falta OPENAI_API_KEY", "fields": {}}
    client = OpenAI(api_key=key)

    instr = (
        "Extrae campos de una cédula nicaragüense. Devuelve JSON estricto con:\n"
        "{cedula, nombres, apellidos, nombre_completo, nacimiento, sexo, emision, expiracion, lugar_nacimiento}.\n"
        "- Usa literalmente lo que se ve en la tarjeta; si no aparece, usa null.\n"
        "- No infieras nombres; si hay MRZ, úsala para apellidos/nombres.\n"
        "- Formatea cedula como NNN-NNNNNN-NNNNX si es posible.\n"
    )

    # Preparar paths de imágenes (manejar PDFs y limpiar temporales)
    raw_inputs = [front_path] + ([back_path] if back_path else [])
    image_paths: List[str] = []
    temps: List[str] = []
    for p in [x for x in raw_inputs if x]:
        if p.lower().endswith('.pdf'):
            tmp_imgs = _pdf_to_images([p])
            temps.extend(tmp_imgs)
            image_paths.extend(tmp_imgs)
        else:
            image_paths.append(p)

    content: List[dict] = [{"type": "text", "text": instr}]
    content += _images_to_content(image_paths, max_side_px=max_side_px)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        import json
        data = json.loads(resp.choices[0].message.content)
        return {"ok": True, "fields": data}
    except Exception as e:
        return {"ok": False, "error": f"Respuesta no JSON: {e}", "raw": getattr(resp.choices[0].message, "content", None)}
    finally:
        for t in temps:
            try:
                os.remove(t)
            except Exception:
                pass


def extract_matricula_with_vision(
    path: str,
    *,
    model: str = "gpt-4o-mini",
    api_key: Optional[str] = None,
    max_side_px: Optional[int] = 1600,
) -> Dict:
    """
    Extrae campos clave de una constancia de matrícula/registro contable.
    Retorna: {ok, fields|error} con campos: {roc, direccion, ruc, nombre, fecha_emision}.
    """
    load_dotenv()
    from openai import OpenAI  # type: ignore

    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        return {"ok": False, "error": "Falta OPENAI_API_KEY", "fields": {}}
    client = OpenAI(api_key=key)

    instr = (
        "Extrae campos de una constancia de matrícula/registro contable municipal.\n"
        "Devuelve JSON estricto con: {roc, direccion, ruc, nombre, fecha_emision}.\n"
        "- 'roc' es el número de matrícula/registro (p.ej., R.O.C No.).\n"
        "- 'direccion' es la dirección del negocio.\n"
        "- Si un campo no aparece, usa null. No infieras datos.\n"
    )

    # Preparar imagen (PDF -> imagen si aplica)
    image_paths: List[str] = []
    temps: List[str] = []
    if path.lower().endswith('.pdf'):
        tmp_imgs = _pdf_to_images([path])
        temps.extend(tmp_imgs)
        image_paths.extend(tmp_imgs)
    else:
        image_paths.append(path)

    content: List[dict] = [{"type": "text", "text": instr}]
    content += _images_to_content(image_paths, max_side_px=max_side_px)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        import json
        data = json.loads(resp.choices[0].message.content)
        return {"ok": True, "fields": data}
    except Exception as e:
        return {"ok": False, "error": f"Respuesta no JSON: {e}", "raw": getattr(resp.choices[0].message, "content", None)}
    finally:
        for t in temps:
            try:
                os.remove(t)
            except Exception:
                pass

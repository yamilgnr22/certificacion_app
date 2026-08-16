"""Extraccion de deudas desde reportes de credito (TransUnion / SIBOIF).

Dos fuentes, mismo destino (el panel DEUDAS del Motor V2). Un solo endpoint
auto-detecta cual es y aplica el lector correcto (la IA EXTRAE, Python
normaliza, el CPA confirma):

  • TransUnion (TUCA): PDF con capa de texto. Detallado: una obligacion por
    fila con numero, entidad, fecha y cuota. pdfplumber saca el texto y el
    LLM lo estructura.
  • SIBOIF: normalmente foto/captura de pantalla. Agregado: una fila por
    (tipo credito, destino, moneda) con cuantas instituciones y el saldo,
    SIN numero/entidad/cuota por deuda. Se lee con vision (OpenAI) y se
    dejan vacios los campos que el reporte no trae, para que el CPA los
    complete a mano (decision del usuario: necesita el detalle).

Router:
  PDF con texto  -> detectar fuente por palabras clave -> LLM sobre el texto.
  Imagen / PDF escaneado -> vision (prompt unico que reconoce ambas fuentes).
"""

from __future__ import annotations

import io
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from llm.provider import LLMProvider, LLMProviderError, OpenAIProvider


class DeudaExtractionError(RuntimeError):
    """Error de extraccion con mensaje apto para mostrar al usuario."""


_MIN_TEXTO = 200  # menos que esto = PDF escaneado o vacio -> vision
_EXT_IMAGEN = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


# ------------------------------------------------------------------ pdf -> texto

def _texto_pdf(data: bytes) -> str:
    """Texto plano de todas las paginas ('' si no tiene capa de texto)."""
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            paginas = [p.extract_text() or "" for p in pdf.pages]
    except Exception as exc:
        raise DeudaExtractionError(f"No se pudo leer el PDF: {exc}") from exc
    texto = "\n".join(paginas).replace("‐", "-")  # guion unicode -> ASCII
    return unicodedata.normalize("NFC", texto)


def extraer_texto_pdf(stream_o_path) -> str:
    """Compat: texto de un PDF; lanza si no tiene capa de texto (escaneado)."""
    if hasattr(stream_o_path, "read"):
        data = stream_o_path.read()
    else:
        data = Path(stream_o_path).read_bytes()
    texto = _texto_pdf(data)
    if len(texto.strip()) < _MIN_TEXTO:
        raise DeudaExtractionError(
            "El PDF no tiene texto extraible (parece escaneado). "
            "Descarga el reporte original en PDF, no una foto/scan."
        )
    return texto


def detectar_fuente(texto: str) -> str | None:
    """'tuca' | 'siboif' | None a partir del texto del reporte."""
    t = (texto or "").upper()
    if "SIBOIF" in t:
        return "siboif"
    if "TRANSUNION" in t or "HISTORIAL CREDITICIO" in t:
        return "tuca"
    return None


# ------------------------------------------------------------------ prompts

_SYSTEM_PROMPT_TUCA = """Eres un asistente que extrae datos de reportes de \
historial crediticio de TransUnion Nicaragua para una contadora publica. \
Recibes el texto plano del PDF y devuelves SOLO un objeto JSON valido.

Estructura del reporte (secciones relevantes):
- "Datos Generales": nombre y cedula del titular.
- "Historico de Obligaciones Vigentes": por cada obligacion viva: Entidad, \
Numero (4 digitos), fecha de Otorgamiento (DD/MM/AAAA), Tipo, Vencimiento y \
Actualizado (MM/AAAA).
- "Saldos y Cupos Obligaciones Vigentes": por obligacion, una o dos lineas de \
moneda (NIO y/o USD) con Limite, Saldo, Mora y Cuota.
- IGNORA "Historico de Obligaciones Cerradas" (canceladas) y "Consultas".

Reglas:
1. Una entrada por cada linea de moneda de una obligacion VIGENTE con Saldo o \
Cuota mayor que 0. Si una obligacion vigente no tiene ninguna linea con saldo \
ni cuota, emite UNA entrada con saldo y cuota en 0.
2. Los montos salen de "Saldos y Cupos"; copialos tal cual, sin calcular.
3. Fechas de otorgamiento/vencimiento salen de "Historico Vigentes"; casa por \
Entidad + Numero.
4. CRITICO - Vencimiento vs Actualizado: en la linea del Tipo de credito \
aparecen DOS fechas al final. La PRIMERA (formato completo DD/MM/AAAA, o \
--/--/---- si no tiene) es el VENCIMIENTO. La SEGUNDA (formato corto MM/AAAA, \
sin dia) es el ACTUALIZADO. Ejemplo: 'CARTERA COMERCIAL ... 04/09/2031 05/2026' \
=> fecha_vencimiento=04/09/2031, fecha_actualizado=05/2026. NUNCA pongas la \
fecha corta MM/AAAA (Actualizado) como fecha_vencimiento. Si el vencimiento es \
--/--/---- (tipico de tarjetas), deja fecha_vencimiento vacio.
5. Si el texto viene duplicado (paginas repetidas), NO dupliques entradas.
6. confianza: "alta"/"media"/"baja".

Responde exactamente:
{
  "fuente": "tuca",
  "titular": {"nombre": str, "cedula": str},
  "fecha_reporte": "AAAA-MM-DD o vacio",
  "deudas": [
    {"numero": str, "entidad": str, "tipo_credito": str, "moneda": "NIO"|"USD",
     "limite": num, "saldo": num, "cuota": num,
     "fecha_otorgamiento": "DD/MM/AAAA", "fecha_vencimiento": "DD/MM/AAAA o vacio",
     "fecha_actualizado": "MM/AAAA", "confianza": "alta"|"media"|"baja"}
  ]
}"""

_SYSTEM_PROMPT_SIBOIF = """Eres un asistente que extrae datos de un reporte de \
credito SIBOIF de Nicaragua para una contadora publica. El reporte puede venir \
como imagen (foto o captura de pantalla). Devuelves SOLO un objeto JSON valido.

Estructura:
- "Informacion Personal": Identificacion (cedula), Nombre, y a la derecha \
Saldo General, Interes General y Monto Cuota Mensual (TOTAL de todas las deudas).
- "Informacion de Creditos": una fila por grupo, con columnas: Fecha, Tipo \
Credito, Destino Cred., Moneda, Situacion, Cant. Instit (numero de \
instituciones), Int. Corrientes, Int. Vencidos, Saldo.

SIBOIF es AGREGADO: cada fila resume varias obligaciones del mismo tipo/moneda; \
NO trae numero de tarjeta, entidad individual, ni cuota por deuda.

Reglas:
1. Una entrada por cada fila de "Informacion de Creditos" con Situacion \
"Vigente". Ignora filas saneadas/canceladas.
2. moneda: "Nacional con Mantenimiento de Valor" -> "NIO"; "Extranjera (US$ \
Dolares)" -> "USD".
3. Copia los montos tal cual (Saldo, Int. Corrientes). No calcules.
4. Lee con cuidado los numeros de la foto; marca confianza "baja" si dudas.

Responde exactamente:
{
  "fuente": "siboif",
  "titular": {"nombre": str, "cedula": str},
  "fecha_reporte": "AAAA-MM-DD o vacio",
  "resumen": {"saldo_general": num, "interes_general": num, "cuota_mensual_total": num},
  "deudas": [
    {"tipo_credito": str, "destino": str, "moneda": "NIO"|"USD",
     "cant_instituciones": num, "saldo": num, "interes_corriente": num,
     "confianza": "alta"|"media"|"baja"}
  ]
}"""

_SYSTEM_PROMPT_VISION = """Eres un asistente que lee reportes de credito de \
Nicaragua a partir de IMAGENES (fotos o capturas) para una contadora publica. \
El reporte puede ser de dos fuentes:
- SIBOIF: titulo 'REPORTE SIBOIF' o seccion 'Informacion de Creditos' con \
columnas Fecha / Tipo Credito / Destino / Moneda / Situacion / Cant. Instit / \
Int. Corrientes / Saldo. Es AGREGADO (una fila por tipo+moneda).
- TransUnion (TUCA): 'Reporte de Historial Crediticio', obligaciones \
individuales con numero de 4 digitos y entidad.

Identifica la fuente y devuelve SOLO un objeto JSON. Copia los numeros tal \
cual; marca confianza 'baja' si la foto es dudosa.

IMPORTANTE: las deudas van SIEMPRE en el arreglo 'deudas' de PRIMER NIVEL, \
NUNCA dentro de 'resumen'.

Si es SIBOIF, responde exactamente:
{
  "fuente": "siboif",
  "titular": {"nombre": str, "cedula": str},
  "resumen": {"saldo_general": num, "interes_general": num, "cuota_mensual_total": num},
  "deudas": [
    {"tipo_credito": str, "destino": str, "moneda": "NIO"|"USD",
     "cant_instituciones": num, "saldo": num, "interes_corriente": num,
     "confianza": "alta"|"media"|"baja"}
  ]
}
(moneda: 'Nacional con Mantenimiento de Valor' -> 'NIO'; 'Extranjera (US$ \
Dolares)' -> 'USD'. La cedula esta en 'Identificacion'.)

Si es TransUnion, responde exactamente:
{
  "fuente": "tuca",
  "titular": {"nombre": str, "cedula": str},
  "deudas": [
    {"numero": str, "entidad": str, "tipo_credito": str, "moneda": "NIO"|"USD",
     "limite": num, "saldo": num, "cuota": num,
     "fecha_otorgamiento": "DD/MM/AAAA", "fecha_vencimiento": "DD/MM/AAAA o vacio",
     "confianza": "alta"|"media"|"baja"}
  ]
}"""


def _provider_default() -> LLMProvider:
    """Provider para documentos: usa OPENAI_MODEL_DOCUMENTS si esta definido."""
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    except ImportError:
        pass
    modelo = (os.getenv("OPENAI_MODEL_DOCUMENTS") or "").strip() or None
    return OpenAIProvider(model=modelo)


# ------------------------------------------------------------------ normalizacion

def _num(v: Any) -> float:
    if v in (None, "", "-"):
        return 0.0
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).replace(",", "").replace(" ", "").strip()
    try:
        return round(float(s), 2)
    except ValueError:
        return 0.0


def _fecha_iso(v: Any) -> str | None:
    """DD/MM/AAAA | MM/AAAA | AAAA-MM-DD -> 'AAAA-MM-DD'. Vacio/basura -> None."""
    s = str(v or "").strip()
    if not s or "--" in s:
        return None
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        dd, mm, aaaa = m.groups()
        return f"{aaaa}-{mm}-{dd}"
    m = re.fullmatch(r"(\d{2})/(\d{4})", s)
    if m:
        mm, aaaa = m.groups()
        return f"{aaaa}-{mm}-01"
    m = re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", s)
    if m:
        return s if len(s) == 10 else s + "-01"
    return None


def _moneda(v: Any) -> str:
    t = str(v or "").upper()
    return "USD" if ("USD" in t or "DOLAR" in t or "EXTRANJERA" in t) else "NIO"


def _estrategia(tipo_credito: str) -> str:
    t = (tipo_credito or "").upper()
    return "revolving" if ("TARJETA" in t or "ROTATIVA" in t) else "amortizable"


def _normalizar_tuca(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    vistas: set[tuple[str, str, str]] = set()
    for d in raw.get("deudas") or []:
        numero = str(d.get("numero") or "").strip()
        entidad = str(d.get("entidad") or "").strip()
        moneda = _moneda(d.get("moneda"))
        if not numero:
            continue
        clave = (entidad.upper(), numero, moneda)
        if clave in vistas:
            continue
        vistas.add(clave)
        tipo = str(d.get("tipo_credito") or "").strip()
        venc = _fecha_iso(d.get("fecha_vencimiento"))
        actualizado = _fecha_iso(d.get("fecha_actualizado"))
        confianza = str(d.get("confianza") or "media")
        # Defensa: si el vencimiento cae en el MISMO mes que 'Actualizado', casi
        # seguro el LLM confundio los dos campos (el vencimiento real de un
        # prestamo es a anios; 'Actualizado' es el mes del reporte). Lo anulamos
        # para que el motor estime el plazo por cuota en vez de usar una fecha
        # falsa y corta, y bajamos la confianza para que el CPA revise.
        if venc and actualizado and venc[:7] == actualizado[:7]:
            venc = None
            confianza = "baja"
        out.append({
            "numero": numero,
            "entidad": entidad,
            "tipo_credito": tipo,
            "estrategia": _estrategia(tipo),
            "moneda": moneda,
            "valor_inicial": _num(d.get("limite")),
            "saldo_reportado": _num(d.get("saldo")),
            "cuota": _num(d.get("cuota")),
            "fecha_otorgamiento": _fecha_iso(d.get("fecha_otorgamiento")),
            "fecha_vencimiento": venc,
            "fecha_actualizado": actualizado,
            "incluir_en_er": True,
            "confianza": confianza,
            "fuente": "tuca",
        })
    return _filtrar_lineas_vacias(out)


def _filtrar_lineas_vacias(deudas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Quita lineas de moneda sin saldo NI cuota, salvo la unica de su
    obligacion (para que la deuda no desaparezca del panel)."""
    con_mov = {
        (d["entidad"].upper(), d["numero"])
        for d in deudas if d["saldo_reportado"] > 0 or d["cuota"] > 0
    }
    out = []
    for d in deudas:
        clave = (d["entidad"].upper(), d["numero"])
        if d["saldo_reportado"] > 0 or d["cuota"] > 0 or clave not in con_mov:
            out.append(d)
    return out


def _normalizar_siboif(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Filas agregadas SIBOIF -> filas de deuda con los campos que el reporte
    NO trae (numero, entidad, cuota, fecha) vacios y una nota para el CPA."""
    out: list[dict[str, Any]] = []
    for d in raw.get("deudas") or []:
        saldo = _num(d.get("saldo"))
        interes = _num(d.get("interes_corriente"))
        if saldo <= 0 and interes <= 0:
            continue
        tipo = str(d.get("tipo_credito") or "").strip()
        destino = str(d.get("destino") or "").strip()
        tipo_full = f"{tipo} - {destino}".strip(" -") if destino else tipo
        cant = d.get("cant_instituciones")
        try:
            cant = int(float(cant)) if cant not in (None, "") else None
        except (TypeError, ValueError):
            cant = None
        nota = "SIBOIF (agregado): completa numero, entidad, cuota y fecha por deuda."
        if cant:
            nota = f"SIBOIF: {cant} institucion(es). " + nota
        if interes:
            nota += f" Interes corriente reportado: {interes:,.2f}."
        out.append({
            "numero": "",
            "entidad": (f"{cant} institucion(es)" if cant else ""),
            "tipo_credito": tipo_full,
            "estrategia": _estrategia(tipo_full),
            "moneda": _moneda(d.get("moneda")),
            "valor_inicial": 0.0,
            "saldo_reportado": saldo,
            "cuota": 0.0,
            "fecha_otorgamiento": None,
            "fecha_vencimiento": None,
            "fecha_actualizado": None,
            "incluir_en_er": True,
            "confianza": str(d.get("confianza") or "media"),
            "fuente": "siboif",
            "notas": nota,
        })
    return out


def _rescatar_deudas(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Defensa: algunos modelos (sobre todo en vision) anidan las deudas
    dentro de 'resumen' u otra clave en vez del arreglo 'deudas' de primer
    nivel. Si 'deudas' viene vacio pero hay una lista de deudas en otro lado,
    la subimos para que la normalizacion la vea."""
    if raw.get("deudas"):
        return dict(raw)
    d = dict(raw)
    for clave in ("resumen", "creditos", "obligaciones", "cuentas"):
        anidado = raw.get(clave)
        if isinstance(anidado, Mapping) and isinstance(anidado.get("deudas"), list):
            d["deudas"] = anidado["deudas"]
            break
        if isinstance(anidado, list) and anidado and isinstance(anidado[0], Mapping):
            d["deudas"] = anidado
            break
    return d


def vencimientos_desde_texto(texto: str, numeros) -> dict[str, str]:
    """Fecha de vencimiento por credito, leida del bloque 'Historico Vigentes'.

    Por que en Python y no con el LLM: el dato de un credito viene partido en
    DOS tablas del reporte ("Saldos y Cupos" trae montos y cuota; "Historico
    Vigentes" trae otorgamiento y vencimiento) y el modelo casa las dos de
    forma inconsistente — en un reporte real trajo 2 vencimientos de 9. El
    bloque tiene formato fijo, asi que un parser lo lee completo y siempre
    igual:

        BANCO DE AMERICA CENTRAL 5176 18/12/2012 MENSUAL
        CARTERA HIPOTECARIA HIPOTECA 05/12/2027 07/2026
                                     ^ vencimiento

    Solo busca los numeros de credito que el LLM ya reconocio, para no
    inventar deudas ni tomar fechas de otra parte del documento. Devuelve
    ISO (AAAA-MM-DD); los creditos con '--/--/----' (tarjetas) no aparecen.
    """
    lineas = [l.strip() for l in (texto or "").splitlines()]
    buscados = {str(n).strip() for n in numeros if str(n).strip()}
    out: dict[str, str] = {}
    for i, linea in enumerate(lineas[:-1]):
        for numero in buscados - set(out):
            # Linea de encabezado: ...NUMERO DD/MM/AAAA PERIODICIDAD
            cab = re.search(
                rf"\b{re.escape(numero)}\s+(\d{{2}}/\d{{2}}/\d{{4}})\b", linea
            )
            if not cab:
                continue
            # La siguiente linea abre con el tipo y la garantia; la PRIMERA
            # fecha completa que trae es el vencimiento (la segunda, en
            # formato corto MM/AAAA, es 'Actualizado' y no matchea aca).
            fechas = re.findall(r"\b\d{2}/\d{2}/\d{4}\b", lineas[i + 1])
            if not fechas:
                continue
            venc, otorg = _fecha_iso(fechas[0]), _fecha_iso(cab.group(1))
            # Sanity: un vencimiento anterior al otorgamiento es otra cosa
            # (una fecha de consulta, un pago), no el plazo del credito.
            if venc and otorg and venc > otorg:
                out[numero] = venc
    return out


def _resultado(
    raw: Mapping[str, Any], fuente: str, retries: int, texto: str = ""
) -> dict[str, Any]:
    raw = _rescatar_deudas(raw)
    titular = raw.get("titular") or {}
    deudas = _normalizar_siboif(raw) if fuente == "siboif" else _normalizar_tuca(raw)
    if fuente == "tuca" and texto:
        # El parser manda sobre el LLM para este campo: es deterministico y
        # lee el bloque completo. Solo rellena lo que falta o corrige lo que
        # el modelo dejo mal.
        venc = vencimientos_desde_texto(texto, [d.get("numero") for d in deudas])
        for d in deudas:
            f = venc.get(str(d.get("numero") or "").strip())
            if f:
                d["fecha_vencimiento"] = f
    out = {
        "ok": True,
        "fuente": fuente,
        "titular": {
            "nombre": str(titular.get("nombre") or "").strip(),
            "cedula": str(titular.get("cedula") or "").strip(),
        },
        "fecha_reporte": _fecha_iso(raw.get("fecha_reporte")),
        "deudas": deudas,
        "llm_retries": retries,
    }
    if fuente == "siboif":
        r = raw.get("resumen") or {}
        out["resumen"] = {
            "saldo_general": _num(r.get("saldo_general")),
            "interes_general": _num(r.get("interes_general")),
            "cuota_mensual_total": _num(r.get("cuota_mensual_total")),
        }
    return out


# ------------------------------------------------------------------ extraccion

def extraer_deudas(texto: str, provider: LLMProvider | None = None) -> dict[str, Any]:
    """Compat TUCA: texto de reporte TransUnion -> resultado normalizado."""
    provider = provider or _provider_default()
    try:
        raw = provider.complete_json(
            system_prompt=_SYSTEM_PROMPT_TUCA,
            user_prompt="Texto del reporte TransUnion:\n\n" + texto,
        )
    except LLMProviderError as exc:
        raise DeudaExtractionError(f"La extraccion con IA fallo: {exc}") from exc
    return _resultado(raw, "tuca", getattr(provider, "last_retries", 0), texto)


def extraer_deudas_de_texto(texto: str, provider: LLMProvider | None = None) -> dict[str, Any]:
    """Reporte con capa de texto: detecta fuente y estructura con el LLM."""
    fuente = detectar_fuente(texto) or "tuca"
    provider = provider or _provider_default()
    prompt = _SYSTEM_PROMPT_SIBOIF if fuente == "siboif" else _SYSTEM_PROMPT_TUCA
    try:
        raw = provider.complete_json(
            system_prompt=prompt,
            user_prompt="Texto del reporte de credito:\n\n" + texto,
        )
    except LLMProviderError as exc:
        raise DeudaExtractionError(f"La extraccion con IA fallo: {exc}") from exc
    return _resultado(raw, fuente, getattr(provider, "last_retries", 0), texto)


def _vision_json(image_paths: list[str], *, model: str | None = None) -> dict[str, Any]:
    """Llama a OpenAI vision con el prompt unico y devuelve el JSON crudo."""
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    except ImportError:
        pass
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise DeudaExtractionError("Falta OPENAI_API_KEY para leer la imagen del reporte.")
    from openai import OpenAI  # type: ignore

    from llm_vision import _images_to_content

    modelo = model or (os.getenv("OPENAI_MODEL_DOCUMENTS") or "").strip() or "gpt-4o-mini"
    content: list[dict] = [{"type": "text", "text": _SYSTEM_PROMPT_VISION}]
    content += _images_to_content(image_paths, max_side_px=2000)
    try:
        resp = OpenAI(api_key=key).chat.completions.create(
            model=modelo,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        import json

        return json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:
        raise DeudaExtractionError(f"La lectura de la imagen fallo: {exc}") from exc


def extraer_deudas_de_imagenes(image_paths: list[str], *, model: str | None = None) -> dict[str, Any]:
    """Fotos/capturas de un reporte -> resultado normalizado (auto-fuente)."""
    raw = _vision_json(image_paths, model=model)
    fuente = str(raw.get("fuente") or "").lower()
    if fuente not in ("tuca", "siboif"):
        fuente = "siboif" if raw.get("resumen") else "tuca"
    return _resultado(raw, fuente, 0)


# ------------------------------------------------------------------ router

def procesar_reporte(filename: str, data: bytes) -> dict[str, Any]:
    """Auto-detecta formato+fuente y devuelve las deudas normalizadas.

    PDF con texto -> LLM sobre el texto (TUCA o SIBOIF por palabras clave).
    Imagen o PDF escaneado -> vision (prompt unico, detecta la fuente)."""
    ext = Path(filename or "").suffix.lower()
    if ext == ".pdf":
        texto = _texto_pdf(data)
        if len(texto.strip()) >= _MIN_TEXTO:
            return extraer_deudas_de_texto(texto)
        # PDF escaneado -> primera pagina a imagen -> vision
        import tempfile

        from llm_vision import _pdf_to_images

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(data)
            tmp_pdf = tmp.name
        imgs = _pdf_to_images([tmp_pdf])
        try:
            if not imgs:
                raise DeudaExtractionError(
                    "El PDF no tiene texto y no se pudo convertir a imagen "
                    "(revisa POPPLER_PATH)."
                )
            return extraer_deudas_de_imagenes(imgs)
        finally:
            for p in [tmp_pdf, *imgs]:
                try:
                    os.remove(p)
                except OSError:
                    pass
    if ext in _EXT_IMAGEN:
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(data)
            tmp_img = tmp.name
        try:
            return extraer_deudas_de_imagenes([tmp_img])
        finally:
            try:
                os.remove(tmp_img)
            except OSError:
                pass
    raise DeudaExtractionError(
        "Formato no soportado. Subi el reporte como PDF (TransUnion) o imagen "
        "JPG/PNG (SIBOIF)."
    )

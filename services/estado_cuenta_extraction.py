"""Extraccion de cuentas bancarias desde estados de cuenta (Nota 1).

Para la "Integracion del Efectivo" el CPA necesita, por cada cuenta del
cliente: banco, tipo de cuenta, moneda, numero y SALDO FINAL al corte.
No se parsean transacciones (fragil y no hace falta): solo el resumen.

Mismo patron que deudas: PDF con texto -> pdfplumber + LLM; foto o PDF
escaneado -> vision OpenAI. La IA extrae, Python normaliza, el CPA revisa.
El cuadre con el ESF ('Efectivo en Caja' residuo) es 100% determinista y
vive en motor/notas.py — nunca lo decide la IA.
"""

from __future__ import annotations

import io
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from llm.provider import LLMProvider, LLMProviderError, OpenAIProvider
from services.deuda_extraction import DeudaExtractionError

_MIN_TEXTO = 150
_EXT_IMAGEN = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


class EstadoCuentaError(DeudaExtractionError):
    """Error de extraccion de estado de cuenta (mensaje apto para UI)."""


_SYSTEM_PROMPT = """Eres un asistente que extrae datos de ESTADOS DE CUENTA \
bancarios de Nicaragua (BAC, LAFISE, Banpro, Ficohsa, BDF, Atlantida, etc.) \
para una contadora publica. Recibes el texto (o imagen) del estado de cuenta \
y devuelves SOLO un objeto JSON valido.

Que extraer (SOLO el resumen; ignora el detalle de transacciones):
- banco: nombre corto de la entidad (p.ej. "LAFISE", "BAC", "Banpro").
- Por cada cuenta que aparezca en el documento:
  - tipo: "Cuenta Corriente" | "Cuenta de Ahorro" | el tipo que indique.
  - moneda: "NIO" (cordobas; "COR" tambien significa cordobas) | "USD".
  - numero: el numero de cuenta tal cual aparece (completo, IBAN si es lo
    que muestra el documento).
  - saldo_final: el saldo AL CIERRE DEL PERIODO del estado. Copialo tal
    cual, sin calcular.
  - fecha_corte: la fecha FINAL del rango del estado ("Hasta"/"Fecha hasta"),
    en AAAA-MM-DD.
- titular: nombre del cliente si aparece.

REGLAS CRITICAS para saldo_final (errores comunes):
1. Los saldos de ENCABEZADO o resumen tipo "Disponible", "Saldo en libros",
   "Saldo disponible" o "Balance de la cuenta" (Banpro, BAC) son el saldo AL
   DIA DE GENERACION/IMPRESION del documento, NO al cierre del periodo.
   NO los uses si el documento trae transacciones con columna Saldo/Balance.
2. Si hay fila resumen DEL PERIODO (p.ej. Banpro: "Saldo anterior / Total
   creditos / Total debitos / SALDO TOTAL / Saldo Disponible"), el saldo
   final del periodo es "Saldo Total" (no "Disponible").
3. Si hay lista de movimientos con columna Saldo/Balance, el saldo final es
   el de la transaccion con FECHA MAS RECIENTE dentro del periodo. OJO: la
   lista puede venir DESCENDENTE (la mas reciente es la PRIMERA, p.ej.
   LAFISE) o ascendente (la mas reciente es la ultima, p.ej. BAC). Compara
   las FECHAS, no te guies por la posicion. En el formato LAFISE
   ("Movimientos / Desde ... Hasta ...", fechas DD/MMM/AAAA descendentes),
   el saldo final es el "Saldo" de la PRIMERA fila que aparece justo despues
   del encabezado de columnas ("Fecha ... Debito Credito Saldo ...").
4. Si hay "Saldo Final" explicito del periodo, usa ese. OJO: puede venir en
   la MISMA linea que el saldo anterior ("Saldo Anterior: US$ 0.00 Saldo
   Final: US$ 1,200.01" -> saldo_final es 1,200.01, el numero que sigue a
   "Saldo Final:"). NUNCA uses el "Saldo Anterior".
5. NO uses la fecha de generacion/impresion del documento como fecha_corte.
6. Si el texto trae el marcador "[... transacciones intermedias omitidas ...]",
   el saldo final de una lista DESCENDENTE esta ANTES del marcador (primeras
   filas) y el de una lista ascendente esta DESPUES (ultimas filas del
   periodo, antes del resumen de totales).
7. banco: NO confundas la sucursal o unidad de atencion (p.ej. "MERCADO
   ORIENTAL") con el banco. Si el nombre del banco no aparece, deducilo del
   IBAN por su codigo: PRCB=Banpro, BCCE=LAFISE, BAMC=BAC, BDFI=BDF,
   FICH=Ficohsa; si no se puede, deja banco vacio.

confianza por cuenta: "alta" si el saldo se lee claro, "media"/"baja" si dudas.

Responde exactamente:
{
  "banco": str,
  "titular": str,
  "cuentas": [
    {"tipo": str, "moneda": "NIO"|"USD", "numero": str,
     "saldo_final": num, "fecha_corte": "AAAA-MM-DD o vacio",
     "confianza": "alta"|"media"|"baja"}
  ]
}"""

# Estados largos (25+ paginas de transacciones): el resumen vive al INICIO
# (encabezado, "Balance de la cuenta") y al FINAL (fila de totales, ultimas
# transacciones). El medio son transacciones que no aportan y cuestan tokens.
_MAX_TEXTO = 24_000
_CHUNK = 11_000


def _reparar_anios_partidos(texto: str) -> str:
    """pdfplumber a veces trunca el anio de una fila y baja el ultimo digito
    a la LINEA SIGUIENTE (LAFISE): '31/DIC/202 5059263 Pagos ... 199,402.94'
    y abajo '5' (o '5 cuentas' si la descripcion tambien se partio). Reunir
    el anio ('31/DIC/2025 ...') para que el LLM pueda comparar fechas."""
    lineas = texto.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lineas):
        ln = lineas[i]
        m = re.search(r"/(20\d)(?=\s|$)", ln)  # anio truncado a 3 digitos
        if m and i + 1 < len(lineas):
            m2 = re.match(r"^\s*(\d)(?:\s+(.*))?$", lineas[i + 1])
            if m2:
                pos = m.end(1)  # posicion tras '20X' truncado
                ln = ln[:pos] + m2.group(1) + ln[pos:]
                out.append(ln)
                resto = (m2.group(2) or "").strip()
                if resto:
                    out.append(resto)
                i += 2
                continue
        out.append(ln)
        i += 1
    return "\n".join(out)


def _recortar_texto(texto: str) -> str:
    if len(texto) <= _MAX_TEXTO:
        return texto
    return (
        texto[:_CHUNK]
        + "\n\n[... transacciones intermedias omitidas ...]\n\n"
        + texto[-_CHUNK:]
    )


_MESES_ES = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
             "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12}
_RE_FECHA = re.compile(
    r"^\s*(\d{1,2})[/-](\d{1,2}|[A-Z]{3})[/-](\d{4})\b", re.IGNORECASE
)
_RE_MONTO = re.compile(r"\d[\d,]*\.\d{2}")


def _hint_ultima_transaccion(texto: str) -> str | None:
    """Localiza DETERMINISTICAMENTE la fila de transaccion con la fecha mas
    reciente (la que trae el saldo final del periodo) para pasarsela al LLM
    como ayuda. Los LLM confunden listas descendentes (LAFISE) con
    ascendentes (BAC); comparar fechas en Python no falla."""
    candidatas: list[tuple[tuple[int, int, int], int, str]] = []
    for idx, ln in enumerate(texto.split("\n")):
        m = _RE_FECHA.match(ln.strip())
        if not m or len(_RE_MONTO.findall(ln)) < 1:
            continue
        dd, mes_s, aaaa = m.group(1), m.group(2).upper(), m.group(3)
        mes = _MESES_ES.get(mes_s) if not mes_s.isdigit() else int(mes_s)
        if not mes or not (1 <= mes <= 12):
            continue
        candidatas.append(((int(aaaa), mes, int(dd)), idx, ln.strip()))
    if not candidatas:
        return None
    fecha_max = max(c[0] for c in candidatas)
    filas_max = [c for c in candidatas if c[0] == fecha_max]
    # Orden de la lista: descendente si la primera fila del doc es la mas
    # reciente -> entre varias filas del mismo dia, la PRIMERA es la ultima
    # operacion; ascendente -> la ULTIMA.
    descendente = candidatas[0][0] >= candidatas[-1][0]
    fila = filas_max[0] if descendente else filas_max[-1]
    return fila[2]


def _provider_default() -> LLMProvider:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    except ImportError:
        pass
    modelo = (os.getenv("OPENAI_MODEL_DOCUMENTS") or "").strip() or None
    return OpenAIProvider(model=modelo)


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
    s = str(v or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        dd, mm, aaaa = m.groups()
        return f"{aaaa}-{mm}-{dd}"
    m = re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", s)
    if m:
        return s if len(s) == 10 else s + "-01"
    return None


def _normalizar(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    banco = str(raw.get("banco") or "").strip()
    out: list[dict[str, Any]] = []
    for c in raw.get("cuentas") or []:
        numero = str(c.get("numero") or "").strip()
        saldo = _num(c.get("saldo_final"))
        if not numero and saldo == 0:
            continue
        out.append({
            "banco": banco,
            "tipo": str(c.get("tipo") or "").strip() or "Cuenta",
            "moneda": "USD" if str(c.get("moneda") or "NIO").upper() == "USD" else "NIO",
            "numero": numero,
            "saldo": saldo,
            "fecha_corte": _fecha_iso(c.get("fecha_corte")),
            "confianza": str(c.get("confianza") or "media"),
        })
    return out


def _resultado(raw: Mapping[str, Any], retries: int) -> dict[str, Any]:
    return {
        "ok": True,
        "banco": str(raw.get("banco") or "").strip(),
        "titular": str(raw.get("titular") or "").strip(),
        "cuentas": _normalizar(raw),
        "llm_retries": retries,
    }


def extraer_cuentas_de_texto(texto: str, provider: LLMProvider | None = None) -> dict[str, Any]:
    provider = provider or _provider_default()
    texto = _reparar_anios_partidos(texto)
    hint = _hint_ultima_transaccion(texto)
    prompt = "Texto del estado de cuenta:\n\n" + _recortar_texto(texto)
    if hint:
        prompt += (
            "\n\nAYUDA DETERMINISTA (calculada por software, confiable): la "
            "transaccion con la fecha MAS RECIENTE del periodo es esta linea:\n"
            f"  {hint}\n"
            "Si el documento no trae un 'Saldo Final' explicito del periodo, el "
            "saldo_final es la columna Saldo/Balance de ESA linea (segun el "
            "encabezado de columnas de la tabla)."
        )
    try:
        raw = provider.complete_json(system_prompt=_SYSTEM_PROMPT, user_prompt=prompt)
    except LLMProviderError as exc:
        raise EstadoCuentaError(f"La extraccion con IA fallo: {exc}") from exc
    return _resultado(raw, getattr(provider, "last_retries", 0))


def _vision_json(image_paths: list[str]) -> dict[str, Any]:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    except ImportError:
        pass
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise EstadoCuentaError("Falta OPENAI_API_KEY para leer la imagen del estado de cuenta.")
    from openai import OpenAI  # type: ignore

    from llm_vision import _images_to_content

    modelo = (os.getenv("OPENAI_MODEL_DOCUMENTS") or "").strip() or "gpt-4o-mini"
    content: list[dict] = [{"type": "text", "text": _SYSTEM_PROMPT}]
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
        raise EstadoCuentaError(f"La lectura de la imagen fallo: {exc}") from exc


def procesar_estado_cuenta(filename: str, data: bytes) -> dict[str, Any]:
    """PDF con texto -> LLM sobre texto; imagen o PDF escaneado -> vision."""
    ext = Path(filename or "").suffix.lower()
    if ext == ".pdf":
        import pdfplumber

        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                texto = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception as exc:
            raise EstadoCuentaError(f"No se pudo leer el PDF: {exc}") from exc
        texto = unicodedata.normalize("NFC", texto.replace("‐", "-"))
        if len(texto.strip()) >= _MIN_TEXTO:
            return extraer_cuentas_de_texto(texto)
        # PDF escaneado -> primera pagina a imagen -> vision
        import tempfile

        from llm_vision import _pdf_to_images

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(data)
            tmp_pdf = tmp.name
        imgs = _pdf_to_images([tmp_pdf])
        try:
            if not imgs:
                raise EstadoCuentaError(
                    "El PDF no tiene texto y no se pudo convertir a imagen (revisa POPPLER_PATH)."
                )
            return _resultado(_vision_json(imgs), 0)
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
            return _resultado(_vision_json([tmp_img]), 0)
        finally:
            try:
                os.remove(tmp_img)
            except OSError:
                pass
    raise EstadoCuentaError(
        "Formato no soportado. Subi el estado de cuenta como PDF o imagen JPG/PNG."
    )

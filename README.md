# Certificación CPA – Generación y Validación

Herramienta para generar un documento de certificación (DOCX) a partir de un Excel con hojas financieras (ESF/ER/Datos/Certificación), incluyendo:
- Validaciones contables determinísticas (ER/ESF)
- Extracción y validación de cédula con LLM Visión (OpenAI)
- Validación opcional con LLM (OpenAI) para coherencia semántica
- Interfaz web (Flask) y CLI

## Requisitos
- Python 3.10 o superior
- Dependencias Python: `pip install -r requirements.txt`
- Dependencia opcional del sistema (para PDFs):
  - Poppler (Windows) para `pdf2image` (rasterizar PDFs a imágenes antes de enviarlas al modelo de visión)

Variables de entorno (opcionales):
- `OPENAI_API_KEY`: para validación LLM
- `POPPLER_PATH`: carpeta de binarios de Poppler (Windows), por ejemplo `C:\\poppler-xx\\Library\\bin`

## Instalación rápida
```
pip install -r requirements.txt
```

## Uso – Web
Inicia el servidor y abre la UI moderna en tu navegador:
```
python main.py
```
- Abre: http://localhost:8000
- Carga el Excel (obligatorio) y, si aplica, Cédula/Matrícula (opcionales)
- Configura opciones (Tipo de ESF: Al corte o Mensual; estrictos, tolerancia, LLM)
- Ejecuta Validación o Genera el documento

## Uso – CLI
```
python generate_from_excel.py \
  --excel ruta.xlsx \
  --out salida.docx \
  [--plantilla plantilla_smartArt.docx] \
  [--cedula cedula.jpg|.pdf] \
  [--strict] [--doc-strict] [--tol 1.0] \
  [--llm] [--llm-model gpt-4o-mini] \
  [--esf corte|mensual]
```

La CLI guarda además un reporte JSON junto al DOCX (`*.validation.json`).

## Estructura del Excel esperada (resumen)
- Hoja `ER` (Estado de Resultados):
  - Primera columna: descripciones; siguientes columnas: valores por periodo
  - Se reconocen anclas como "(=) Ingresos Brutos", "Total costos", "(-) Gastos operativos", "Total gastos operativos", "Utilidad Bruta", "Utilidad Operativa"/"Neta"
- Hoja `ESF`/`ESF_Corte` (Situación Financiera al corte): lado izquierdo Activos (col 0/1), derecho Pasivo+Patrimonio (col 3/4); anclas típicas "Corrientes", "Total Corrientes", "No Corrientes", "Total No Corrientes", "Total Activos", "Pasivos", "Total Pasivos", "Patrimonio", "Total Patrimonio", "Total Pasivo + Patrimonio"
- Hoja `ESF_Mensual` (Situación Financiera mensual): columna 0 = descripciones; columnas 1..N = valores por mes.
- Hoja `Datos`: datos auxiliares
- Hoja `Certificacion`: pares clave/valor (etiqueta en col 0, valor en col 1). Se aceptan sinónimos robustos (ver `generators/utils.py`).

## Notebooks
El notebook principal se movió a `notebooks/FlujoValidacion.ipynb`.

Para reordenarlo automáticamente:
```
python scripts/reorder_notebook.py notebooks/FlujoValidacion.ipynb
```

## Generación del documento
La orquestación principal está en `document_generator.py` y submódulos en `generators/` (certificación, tablas ER/ESF, datos, y tabla de documentos del cliente). La plantilla SmartArt se fusiona al final (`plantilla_smartArt.docx`).

## Cédula con Visión
- Imágenes: se envían al modelo de visión (se recomienda reescalar a ~1600 px lado mayor)
- PDFs: se rasteriza la primera página con `pdf2image` (Poppler) y se envía la imagen al modelo
- Validación: `vision_validation.py` compara los campos detectados con la hoja `Certificacion`

## Validación con LLM (opcional)
- Requiere `OPENAI_API_KEY`
- Snapshot de datos en `llm_validation.py` (`build_snapshot` + `llm_validate`)

## Estado del código
- El archivo legado `word_generator.py` fue archivado en `archived/word_generator.py` y no se usa. La generación actual se realiza con `document_generator.py` + `generators/*`.

## Estructura sugerida (resumen actual)
- `main.py` (GUI), `generate_from_excel.py` (CLI)
- `document_generator.py`, `generators/` (secciones DOCX)
- `validators.py`, `vision_validation.py`, `llm_validation.py`, `llm_vision.py`
- `excel_reader.py`, `path_utils.py`, `report_utils.py`, `word_helpers.py`
- `assets/` (plantillas) – actualmente `plantilla_smartArt.docx` en raíz
- `notebooks/` (Jupyter)
- `scripts/` (utilidades)

## Licencia y créditos
Configura aquí la licencia si aplica. Revisa y parametriza cualquier dato personal (encabezados/firma) en las plantillas.

Nota: Se eliminó el soporte de OCR clásico (Tesseract/pytesseract). El flujo usa únicamente Visión.

## UI Web (HTML) — Nueva

Se agregó una interfaz web moderna basada en Flask con:
- Carga de Excel y documentos (Cédula y Matrícula/ROC opcionales)
- Validación paso a paso con progreso en vivo (ER, ESF, Documentos, LLM)
- Descarga del documento Word ya validado

Iniciar:
```
pip install -r requirements.txt
python web_server.py
```
Luego abrir:
```
http://localhost:8000/
```
Notas:
- Para la validación LLM, exporta `OPENAI_API_KEY`.
- Directorio de subidas configurable con `CERTAPP_UPLOAD_DIR`. Por defecto usa el directorio temporal del sistema (p.ej., `%TEMP%/certapp_uploads`).
- Borrado automático de subidas: TTL configurable con `CERTAPP_UPLOAD_TTL_SECONDS` (por defecto 7200s). También se limpian los archivos del token al finalizar la generación del documento.
- La descarga del DOCX incluye a la par un `*.validation.json` con el desglose de validaciones.

### Configuración con archivo .env

Puedes crear un archivo `.env` en la raíz del proyecto para configurar el servidor sin tocar el código ni variables de tu consola. El servidor carga `.env` al arrancar.

Ejemplo de `.env`:
```
# Clave para validación LLM (opcional)
OPENAI_API_KEY=sk-...

# Carpeta donde guardar Excel/Cédula/Matrícula subidos
CERTAPP_UPLOAD_DIR=D:\\certapp_uploads

# Tiempo de vida (en segundos) para limpiar subidas inactivas
CERTAPP_UPLOAD_TTL_SECONDS=7200
```

Después de guardar `.env`, inicia el servidor normalmente con `python main.py`.


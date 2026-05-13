from __future__ import annotations

import os
import json
import uuid
import tempfile
from pathlib import Path
from time import time
from typing import Dict, Any, Optional

from flask import Flask, request, Response, send_file, render_template, after_this_request
from dotenv import load_dotenv

from excel_reader import ExcelData
from validators import validate_er, validate_esf
from vision_validation import validate_cedula_vision, validate_matricula_vision
from llm_validation import build_snapshot, llm_validate
from document_generator import generar_documento_completo
from report_utils import build_report, save_report_json


load_dotenv()
BASE_DIR = Path(__file__).parent.resolve()
# Directorio de subidas configurable: CERTAPP_UPLOAD_DIR. Por defecto, temp del SO.
_default_upload = Path(tempfile.gettempdir()) / "certapp_uploads"
UPLOAD_DIR = Path(os.getenv("CERTAPP_UPLOAD_DIR", str(_default_upload)))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# TTL configurable (segundos) para limpiar subidas antiguas
TTL_SECONDS = int(os.getenv("CERTAPP_UPLOAD_TTL_SECONDS", "7200"))  # 2h por defecto


app = Flask(
    __name__,
    static_folder=str(BASE_DIR / "webui" / "static"),
    template_folder=str(BASE_DIR / "webui" / "templates"),
)

# En memoria, simple. Para producción usar almacenamiento persistente o DB.
JOBS: Dict[str, Dict[str, Any]] = {}


def _safe_unlink(path: Optional[str]) -> None:
    if not path:
        return
    try:
        p = Path(path)
        if p.exists():
            p.unlink(missing_ok=True)
    except Exception:
        pass


def _cleanup_job_files(job: Dict[str, Any]) -> None:
    files = (job or {}).get("files", {})
    _safe_unlink(files.get("excel"))
    _safe_unlink(files.get("cedula"))
    _safe_unlink(files.get("cedula_front"))
    _safe_unlink(files.get("cedula_back"))
    _safe_unlink(files.get("matricula"))


def prune_uploads() -> None:
    """Elimina trabajos/archivos expirados y archivos huérfanos viejos en UPLOAD_DIR."""
    now = time()
    # Limpiar trabajos en memoria
    expired: list[str] = []
    for token, job in list(JOBS.items()):
        created = float(job.get("created", now))
        active = bool(job.get("active", False))
        if not active and (now - created) > TTL_SECONDS:
            _cleanup_job_files(job)
            expired.append(token)
    for t in expired:
        JOBS.pop(t, None)
    # Limpiar archivos huérfanos
    try:
        for p in UPLOAD_DIR.glob("*"):
            try:
                if not p.is_file():
                    continue
                age = now - p.stat().st_mtime
                if age > TTL_SECONDS:
                    p.unlink(missing_ok=True)
            except Exception:
                continue
    except Exception:
        pass


def _sse(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\n" f"data: {payload}\n\n"


def _save_upload(file_storage, suffix: str) -> str:
    if not file_storage:
        return ""
    # Mantener la extensión si existe
    orig = file_storage.filename or f"file{suffix}"
    name = f"{uuid.uuid4().hex}_{Path(orig).name}"
    path = UPLOAD_DIR / name
    file_storage.save(path)
    return str(path)


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/upload")
def upload():
    """Sube archivos y devuelve un token para usarlos en validación/generación."""
    form = request.form
    excel = request.files.get("excel")
    cedula_front = request.files.get("cedula_front") or request.files.get("cedula")
    cedula_back = request.files.get("cedula_back")
    matricula = request.files.get("matricula")
    if not excel:
        return {"ok": False, "error": "Falta archivo Excel"}, 400

    prune_uploads()
    token = uuid.uuid4().hex
    excel_path = _save_upload(excel, ".xlsx")
    ced_front_path = _save_upload(cedula_front, ".png") if cedula_front else None
    ced_back_path = _save_upload(cedula_back, ".png") if cedula_back else None
    mat_path = _save_upload(matricula, ".png") if matricula else None

    # Opciones por defecto: validaciones siempre activas; LLM habilitado si hay API key
    def _bool(v: Optional[str], default: bool) -> bool:
        if v is None:
            return default
        s = str(v).strip().lower()
        return s in {"1", "true", "yes", "on"}

    opts = {
        "strict_contable": _bool(form.get("strict_contable"), True),
        "strict_docs": _bool(form.get("strict_docs"), True),
        "use_llm": (form.get("use_llm") == "true") or bool(os.getenv("OPENAI_API_KEY")),
        "tolerancia": float(form.get("tolerancia", "1.0") or 1.0),
        # Tipo de ESF: 'corte' (por defecto) o 'mensual'
        "esf_tipo": (form.get("esf_tipo") or "corte").lower(),
    }

    JOBS[token] = {
        "files": {
            "excel": excel_path,
            "cedula_front": ced_front_path,
            "cedula_back": ced_back_path,
            # compatibilidad: campo antiguo 'cedula'
            "cedula": ced_front_path if (ced_front_path and not ced_back_path) else None,
            "matricula": mat_path,
        },
        "opts": opts,
        "results": {},
        "created": time(),
        "active": False,
    }
    return {"ok": True, "token": token}


@app.get("/api/validate/stream")
def validate_stream():
    """Inicia validación en streaming SSE usando un token previo de upload."""
    token = request.args.get("token")
    if not token or token not in JOBS:
        return {"ok": False, "error": "Token inválido"}, 400

    job = JOBS[token]
    files = job["files"]
    opts = job["opts"]

    job["active"] = True

    def run():
        try:
            yield _sse("status", {"message": "Cargando Excel"})
            data = ExcelData(files["excel"])
            esf_tipo = (opts.get("esf_tipo") or "corte").lower()
            df_esf = data.get_situacion_financiera(esf_tipo)
            df_er = data.get_resultados()
            df_datos = data.get_datos()
            df_cert = data.get_certificacion()

            tol = float(opts.get("tolerancia", 1.0) or 1.0)

            # ER
            yield _sse("progress", {"step": "er", "status": "running"})
            v_er = validate_er(df_er, tolerance=tol)
            yield _sse("result", {"step": "er", "result": v_er})

            # ESF
            yield _sse("progress", {"step": "esf", "status": "running"})
            v_esf = validate_esf(df_esf, tolerance=tol, mode=esf_tipo)
            yield _sse("result", {"step": "esf", "result": v_esf})

            # Documentos: Cédula
            v_ced = None
            # Preferir frente/reverso si existen; compat: 'cedula' único
            ced_front = files.get("cedula_front") or files.get("cedula")
            ced_back = files.get("cedula_back")
            if ced_front:
                yield _sse("progress", {"step": "docs_cedula", "status": "running"})
                v_ced = validate_cedula_vision(
                    df_cert,
                    cedula_front=ced_front,
                    cedula_back=ced_back,
                )
                yield _sse("result", {"step": "docs_cedula", "result": v_ced})

            # Documentos: Matrícula/ROC
            v_mat = None
            if files.get("matricula"):
                yield _sse("progress", {"step": "docs_matricula", "status": "running"})
                v_mat = validate_matricula_vision(
                    df_cert,
                    matricula_path=files.get("matricula"),
                )
                yield _sse("result", {"step": "docs_matricula", "result": v_mat})

            # LLM opcional
            v_llm = None
            if bool(opts.get("use_llm")):
                yield _sse("progress", {"step": "llm", "status": "running"})
                snap = build_snapshot(df_er, df_esf, df_cert, v_er, v_esf, v_ced)
                v_llm = llm_validate(snap)
                yield _sse("result", {"step": "llm", "result": v_llm})

            # Guardar resultados en job
            results = {
                "er": v_er,
                "esf": v_esf,
                "docs_cedula": v_ced,
                "docs_matricula": v_mat,
                "llm": v_llm,
            }
            job["results"] = results

            summary = {
                "er_ok": v_er.get("ok", False) if v_er else False,
                "esf_ok": v_esf.get("ok", False) if v_esf else False,
                "docs_ok": (v_ced.get("ok", False) if v_ced else True) and (v_mat.get("ok", False) if v_mat else True),
                "llm_issues": len(v_llm.get("issues", [])) if v_llm else 0,
            }
            yield _sse("done", {"ok": True, "summary": summary})
        except Exception as exc:
            yield _sse("error", {"type": type(exc).__name__, "message": str(exc)})
        finally:
            job["active"] = False

    headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"}
    return Response(run(), headers=headers)


@app.get("/api/generate")
def generate_doc():
    prune_uploads()
    token = request.args.get("token")
    if not token or token not in JOBS:
        return {"ok": False, "error": "Token inválido"}, 400
    job = JOBS[token]
    files = job["files"]
    opts = job["opts"]

    # Cargar Excel
    data = ExcelData(files["excel"])
    esf_tipo = (opts.get("esf_tipo") or "corte").lower()
    df_esf = data.get_situacion_financiera(esf_tipo)
    df_er = data.get_resultados()
    df_datos = data.get_datos()
    df_cert = data.get_certificacion()

    # Usar resultados previos si existen; si no, revalidar mínimo ER/ESF
    res = job.get("results", {}) or {}
    tol = float(opts.get("tolerancia", 1.0) or 1.0)
    v_er = res.get("er") or validate_er(df_er, tolerance=tol)
    v_esf = res.get("esf") or validate_esf(df_esf, tolerance=tol, mode=esf_tipo)
    # Revalidar documentos si hay archivos disponibles
    ced_front = files.get("cedula_front") or files.get("cedula")
    ced_back = files.get("cedula_back")
    if ced_front:
        from vision_validation import validate_cedula_vision  # import local por peso
        v_docs = validate_cedula_vision(df_cert, cedula_front=ced_front, cedula_back=ced_back)
    else:
        v_docs = None
    # LLM opcional: ejecutar si está habilitado y tenemos API key
    if bool(opts.get("use_llm")):
        snap = build_snapshot(df_er, df_esf, df_cert, v_er, v_esf, v_docs)
        v_llm = llm_validate(snap)
    else:
        v_llm = None

    # Generar documento en temporal
    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
        out_path = tmp.name

    generar_documento_completo(
        df_esf,
        df_er,
        df_datos,
        df_cert,
        out_path,
        incluir_validacion=False,
        tolerancia_validacion=tol,
        detener_si_error=bool(opts.get("strict_contable")),
        validacion_documentos=v_docs,
        validacion_llm=v_llm,
        esf_tipo=esf_tipo,
    )

    # Guardar reporte JSON junto al DOCX en la misma carpeta temporal
    report = build_report(v_er=v_er, v_esf=v_esf, v_docs=v_docs, v_llm=v_llm, meta={"token": token, "esf_tipo": esf_tipo})
    save_report_json(report, out_path)

    filename = f"certificacion_{token[:8]}.docx"
    # Programar limpieza del archivo generado tras la respuesta
    @after_this_request
    def _remove_generated(response):
        try:
            Path(out_path).unlink(missing_ok=True)
        except Exception:
            pass
        return response

    # Limpiar subidas asociadas al token y remover job (post-generación)
    try:
        _cleanup_job_files(job)
    finally:
        JOBS.pop(token, None)

    return send_file(out_path, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=True)

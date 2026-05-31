from __future__ import annotations

import os
import json
import uuid
import tempfile
from pathlib import Path
from time import time
from typing import Dict, Any, Optional

from flask import Flask, request, Response, send_file, render_template, after_this_request, g
from dotenv import load_dotenv

from excel_reader import ExcelData
from validators import validate_er, validate_esf
from vision_validation import validate_cedula_vision, validate_matricula_vision
from llm_validation import build_snapshot, llm_validate
from document_generator import generar_documento_completo
from report_utils import build_report, save_report_json
from financial_model import build_financial_model, result_to_json
from accounting_model import get_account_ledger, get_trace, reverse_voucher
from chat_controller import ChatCommandError, handle_chat_command
from model_chat import ModelChatError, preview_chat_adjustment
from document_extraction import extract_client_documents
from model_storage import (
    ModelStorageError,
    delete_draft,
    duplicate_final,
    final_document_path,
    get_draft,
    get_final,
    list_drafts,
    list_finals,
    save_draft,
    save_final,
)
from db.engine import get_engine, session_factory
from db.runtime import DatabaseNotInitialized, DatabaseOutOfDate, require_alembic_version
from services import (
    AccountCatalogService,
    AgentCommandService,
    AgentConfigError,
    AgentNotFoundError,
    AgentProposalConflictError,
    AgentServiceError,
    AgentValidationError,
    ClienteService,
    GiroService,
    PeriodoConflictError,
    PeriodoNotFoundError,
    PeriodoService,
    PeriodoValidationError,
    PlantillaService,
    ServiceConflictError,
    ServiceValidationError,
)
from repositories import AgentRepository


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
# Recargar templates Jinja2 al modificarlos sin reiniciar el server.
# Si querés cachear en produccion, exportá CERTAPP_TEMPLATE_CACHE=1.
if os.getenv("CERTAPP_TEMPLATE_CACHE", "0").strip().lower() not in {"1", "true", "yes", "on"}:
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    # Que el navegador no cachee estaticos: re-pide app.js/styles.css en cada refresh
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# En memoria, simple. Para producción usar almacenamiento persistente o DB.
JOBS: Dict[str, Dict[str, Any]] = {}
_DB_ENGINE = None


def _get_db_engine():
    global _DB_ENGINE
    configured = app.config.get("DB_ENGINE")
    if configured is not None:
        return configured
    if _DB_ENGINE is None:
        _DB_ENGINE = get_engine()
    return _DB_ENGINE


def _db_requires_alembic() -> bool:
    if "DB_REQUIRE_ALEMBIC" in app.config:
        return bool(app.config["DB_REQUIRE_ALEMBIC"])
    return os.getenv("CERTAPP_DB_REQUIRE_ALEMBIC", "1").strip().lower() not in {"0", "false", "no", "off"}


def _is_db_api_path(path: str) -> bool:
    if path == "/api/clientes/extract-from-docs":
        return False
    return (
        path.startswith("/api/clientes")
        or path.startswith("/api/giros")
        or path.startswith("/api/periodos")
        or path.startswith("/api/audit")
        or path.startswith("/api/agent")
        or path.startswith("/api/catalogo")
    )


@app.before_request
def _open_db_session_for_api():
    if not _is_db_api_path(request.path):
        return None
    try:
        engine = _get_db_engine()
        if _db_requires_alembic():
            require_alembic_version(engine)
        g.db_session = session_factory(engine)()
    except (DatabaseNotInitialized, DatabaseOutOfDate) as exc:
        return {"ok": False, "error": str(exc)}, 500
    except Exception as exc:
        return {"ok": False, "error": f"No se pudo abrir la base de datos: {type(exc).__name__}: {exc}"}, 500
    return None


@app.teardown_request
def _close_db_session(exc):
    session = g.pop("db_session", None)
    if session is None:
        return
    try:
        if exc is not None:
            session.rollback()
    finally:
        session.close()


def _db_session():
    session = getattr(g, "db_session", None)
    if session is None:
        raise RuntimeError("Sesion de base de datos no disponible")
    return session


def _cpa_user() -> str:
    return request.headers.get("X-CPA-User", "system").strip() or "system"


def _increment_legacy_chat_counter(endpoint: str) -> None:
    """Best-effort: no bloquea el endpoint legacy si DB/migracion no esta lista."""
    try:
        engine = _get_db_engine()
        if _db_requires_alembic():
            require_alembic_version(engine)
        session = session_factory(engine)()
        try:
            AgentRepository(session).increment_legacy_counter(endpoint)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception:
        app.logger.debug("No se pudo incrementar contador legacy %s", endpoint, exc_info=True)


def _json_body() -> dict:
    body = request.get_json(silent=True) or {}
    return body if isinstance(body, dict) else {}


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


def _service_error_response(exc: Exception):
    if isinstance(exc, AgentNotFoundError):
        return {"ok": False, "error": str(exc), "assistant_message": str(exc)}, 404
    if isinstance(exc, AgentProposalConflictError):
        return {"ok": False, "error": str(exc), "assistant_message": str(exc)}, 409
    if isinstance(exc, (AgentConfigError, AgentValidationError)):
        return {"ok": False, "error": str(exc), "assistant_message": str(exc)}, 400
    if isinstance(exc, AgentServiceError):
        return {"ok": False, "error": str(exc), "assistant_message": str(exc)}, 400
    if isinstance(exc, (ServiceConflictError, PeriodoConflictError)):
        return {"ok": False, "error": str(exc)}, 409
    if isinstance(exc, (ServiceValidationError, PeriodoValidationError)):
        return {"ok": False, "error": str(exc)}, 400
    if isinstance(exc, PeriodoNotFoundError):
        return {"ok": False, "error": str(exc)}, 404
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400


@app.get("/api/giros")
def api_list_giros():
    try:
        return {"ok": True, "giros": GiroService(_db_session()).list_active()}
    except Exception as exc:
        return _service_error_response(exc)


@app.get("/api/giros/<giro_id>")
def api_get_giro(giro_id: str):
    try:
        giro = GiroService(_db_session()).get(giro_id)
        if not giro:
            return {"ok": False, "error": "Giro no encontrado"}, 404
        return {"ok": True, "giro": giro}
    except Exception as exc:
        return _service_error_response(exc)


@app.get("/api/catalogo")
def api_list_account_catalog():
    try:
        service = AccountCatalogService(_db_session())
        recurring_arg = str(request.args.get("recurring") or "").strip().lower()
        recurring = True if recurring_arg in {"1", "true", "yes", "si"} else None
        postable_arg = str(request.args.get("postable") or "").strip().lower()
        postable = True if postable_arg in {"1", "true", "yes", "si"} else (False if postable_arg in {"0", "false", "no"} else None)
        accounts = service.list(
            query=request.args.get("q", ""),
            account_type=request.args.get("type", "") or request.args.get("account_type", ""),
            section=request.args.get("section", ""),
            recurring=recurring,
            postable=postable,
        )
        return {"ok": True, "accounts": accounts, "summary": service.summary()}
    except Exception as exc:
        return _service_error_response(exc)


@app.get("/api/clientes")
def api_list_clientes():
    try:
        service = ClienteService(_db_session())
        clientes = service.list(query=request.args.get("q", ""), giro_id=request.args.get("giro") or None)
        return {"ok": True, "clientes": clientes}
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/clientes")
def api_create_cliente():
    try:
        cliente = ClienteService(_db_session()).create(_json_body(), cpa_user=_cpa_user())
        return {"ok": True, "cliente": cliente}, 201
    except Exception as exc:
        return _service_error_response(exc)


@app.get("/api/clientes/<cliente_id>")
def api_get_cliente(cliente_id: str):
    try:
        detail = ClienteService(_db_session()).get_detail(cliente_id)
        if not detail:
            return {"ok": False, "error": "Cliente no encontrado"}, 404
        return {"ok": True, **detail}
    except Exception as exc:
        return _service_error_response(exc)


@app.put("/api/clientes/<cliente_id>")
def api_update_cliente(cliente_id: str):
    try:
        cliente = ClienteService(_db_session()).update(cliente_id, _json_body(), cpa_user=_cpa_user())
        if not cliente:
            return {"ok": False, "error": "Cliente no encontrado"}, 404
        return {"ok": True, "cliente": cliente}
    except Exception as exc:
        return _service_error_response(exc)


@app.delete("/api/clientes/<cliente_id>")
def api_delete_cliente(cliente_id: str):
    try:
        deleted = ClienteService(_db_session()).soft_delete(cliente_id, cpa_user=_cpa_user())
        if not deleted:
            return {"ok": False, "error": "Cliente no encontrado"}, 404
        return {"ok": True}
    except Exception as exc:
        return _service_error_response(exc)


@app.get("/api/clientes/<cliente_id>/plantilla-gastos")
def api_get_cliente_plantilla(cliente_id: str):
    try:
        detail = ClienteService(_db_session()).get_detail(cliente_id)
        if not detail:
            return {"ok": False, "error": "Cliente no encontrado"}, 404
        return {"ok": True, "plantilla_gastos": detail["plantilla_gastos"]}
    except Exception as exc:
        return _service_error_response(exc)


@app.put("/api/clientes/<cliente_id>/plantilla-gastos")
def api_set_cliente_plantilla(cliente_id: str):
    try:
        body = _json_body()
        plantilla = body.get("plantilla") if isinstance(body.get("plantilla"), dict) else body
        result = ClienteService(_db_session()).set_plantilla(cliente_id, plantilla, cpa_user=_cpa_user())
        if not result:
            return {"ok": False, "error": "Cliente no encontrado"}, 404
        return {"ok": True, "plantilla_gastos": result}
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/clientes/extract-from-docs")
def api_cliente_extract_from_docs():
    prune_uploads()
    cedula_front = request.files.get("cedula_front")
    cedula_back = request.files.get("cedula_back")
    matricula = request.files.get("matricula")
    if not any([cedula_front, cedula_back, matricula]):
        return {"ok": False, "error": "Adjunte al menos una imagen de cedula o matricula."}, 400

    saved: list[str] = []
    try:
        ced_front_path = _save_upload(cedula_front, ".png") if cedula_front else None
        ced_back_path = _save_upload(cedula_back, ".png") if cedula_back else None
        mat_path = _save_upload(matricula, ".png") if matricula else None
        saved = [p for p in [ced_front_path, ced_back_path, mat_path] if p]
        data = extract_client_documents(
            cedula_front=ced_front_path,
            cedula_back=ced_back_path,
            matricula_path=mat_path,
        )
        status = 200 if data.get("ok") else 400
        return data, status
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400
    finally:
        for path in saved:
            _safe_unlink(path)


@app.get("/api/clientes/<cliente_id>/periodos")
def api_list_periodos(cliente_id: str):
    try:
        service = PeriodoService(_db_session())
        return {"ok": True, "periodos": service.list_for_cliente(cliente_id)}
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/clientes/<cliente_id>/periodos")
def api_create_periodo(cliente_id: str):
    try:
        body = _json_body()
        result = PeriodoService(_db_session()).create(cliente_id, body, cpa_user=_cpa_user())
        return {"ok": True, **result}, 201
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/clientes/<cliente_id>/rollforward-preview")
def api_rollforward_preview(cliente_id: str):
    try:
        body = _json_body()
        mes_inicial = str(body.get("mes_inicial") or "").strip()
        result = PeriodoService(_db_session()).rollforward_preview(cliente_id, mes_inicial)
        return {"ok": True, "rollforward": result}
    except Exception as exc:
        return _service_error_response(exc)


@app.get("/api/periodos/editables")
def api_list_editables():
    try:
        records = PeriodoService(_db_session()).list_editables()
        return {"ok": True, "periodos": records}
    except Exception as exc:
        return _service_error_response(exc)


@app.get("/api/periodos/<periodo_id>")
def api_get_periodo(periodo_id: str):
    try:
        detail = PeriodoService(_db_session()).get_detail(periodo_id)
        if not detail:
            return {"ok": False, "error": "Periodo no encontrado"}, 404
        return {"ok": True, **detail}
    except Exception as exc:
        return _service_error_response(exc)


@app.put("/api/periodos/<periodo_id>")
def api_update_periodo(periodo_id: str):
    try:
        result = PeriodoService(_db_session()).update(periodo_id, _json_body(), cpa_user=_cpa_user())
        return {"ok": True, **result}
    except Exception as exc:
        return _service_error_response(exc)


@app.put("/api/periodos/<periodo_id>/payload")
def api_update_periodo_payload(periodo_id: str):
    try:
        body = _json_body()
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
        result = PeriodoService(_db_session()).update_payload(periodo_id, payload, cpa_user=_cpa_user())
        return {"ok": True, **result}
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/periodos/<periodo_id>/preview")
def api_preview_periodo(periodo_id: str):
    try:
        rendered = PeriodoService(_db_session()).preview(periodo_id)
        return {"ok": True, "render": rendered}
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/periodos/<periodo_id>/finalizar")
def api_finalize_periodo(periodo_id: str):
    try:
        result = PeriodoService(_db_session()).finalize(periodo_id, cpa_user=_cpa_user())
        return {"ok": True, **result}
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/periodos/<periodo_id>/duplicar")
def api_duplicate_periodo(periodo_id: str):
    try:
        result = PeriodoService(_db_session()).duplicate(periodo_id, cpa_user=_cpa_user())
        return {"ok": True, **result}, 201
    except Exception as exc:
        return _service_error_response(exc)


@app.delete("/api/periodos/<periodo_id>")
def api_delete_periodo(periodo_id: str):
    try:
        ok = PeriodoService(_db_session()).hard_delete(periodo_id, cpa_user=_cpa_user())
        if not ok:
            return {"ok": False, "error": "Periodo no encontrado"}, 404
        return {"ok": True}
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/periodos/<periodo_id>/generar-documento")
def api_generate_periodo_document(periodo_id: str):
    try:
        result = PeriodoService(_db_session()).generate_document(periodo_id, cpa_user=_cpa_user())
        return {"ok": True, **result}
    except Exception as exc:
        return _service_error_response(exc)


@app.get("/api/periodos/<periodo_id>/documento")
def api_download_periodo_document(periodo_id: str):
    try:
        path = PeriodoService(_db_session()).get_document_path(periodo_id)
        if not path:
            return {"ok": False, "error": "Documento no generado o archivo no encontrado"}, 404
        from pathlib import Path
        filename = Path(path).name
        return send_file(path, as_attachment=True, download_name=filename)
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/agent/command")
def api_agent_command():
    """Nuevo asistente contable para periodos SQLite."""
    try:
        body = _json_body()
        provider = app.config.get("AGENT_LLM_PROVIDER")
        data = AgentCommandService(_db_session(), provider=provider).handle_command(
            periodo_id=str(body.get("periodo_id") or ""),
            message=str(body.get("message") or ""),
            ui_context=body.get("ui_context") if isinstance(body.get("ui_context"), dict) else {},
            current_payload=body.get("current_payload") if isinstance(body.get("current_payload"), dict) else None,
            is_dirty=bool(body.get("is_dirty")),
            cpa_user=_cpa_user(),
        )
        return data, 200
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/agent/proposals/<proposal_id>/apply")
def api_agent_apply_proposal(proposal_id: str):
    try:
        data = AgentCommandService(_db_session()).apply_proposal(proposal_id, cpa_user=_cpa_user())
        return data, 200
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/agent/proposals/<proposal_id>/discard")
def api_agent_discard_proposal(proposal_id: str):
    try:
        data = AgentCommandService(_db_session()).discard_proposal(proposal_id, cpa_user=_cpa_user())
        return data, 200
    except Exception as exc:
        return _service_error_response(exc)


@app.get("/api/agent/plans/<plan_id>")
def api_agent_get_plan(plan_id: str):
    try:
        data = AgentCommandService(_db_session()).get_plan(plan_id, cpa_user=_cpa_user())
        return data, 200
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/agent/plans/<plan_id>/apply")
def api_agent_apply_plan(plan_id: str):
    try:
        data = AgentCommandService(_db_session()).apply_plan(plan_id, cpa_user=_cpa_user())
        return data, 200
    except Exception as exc:
        return _service_error_response(exc)


@app.post("/api/agent/plans/<plan_id>/discard")
def api_agent_discard_plan(plan_id: str):
    try:
        data = AgentCommandService(_db_session()).discard_plan(plan_id, cpa_user=_cpa_user())
        return data, 200
    except Exception as exc:
        return _service_error_response(exc)


@app.get("/api/audit")
def api_list_audit():
    try:
        from services import AuditService
        entity_type = request.args.get("entity_type", "").strip()
        entity_id = request.args.get("entity_id", "").strip()
        if not entity_type or not entity_id:
            return {"ok": False, "error": "entity_type y entity_id son requeridos"}, 400
        records = AuditService(_db_session()).list_for(entity_type, entity_id)
        return {"ok": True, "records": records}
    except Exception as exc:
        return _service_error_response(exc)


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

    try:
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
    except Exception as exc:
        import traceback
        app.logger.error("Error generando documento: %s\n%s", exc, traceback.format_exc())
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, 400

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


@app.post("/api/model/preview")
def model_preview():
    """Calcula ER/ESF desde inputs de la app y devuelve una vista previa JSON."""
    try:
        payload = request.get_json(silent=True) or {}
        result = build_financial_model(payload)
        return result_to_json(result)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400


def _payload_from_model_reference() -> Dict[str, Any]:
    draft_id = request.args.get("draft_id") or ""
    final_id = request.args.get("final_id") or ""
    if draft_id:
        return dict(get_draft(draft_id).get("payload") or {})
    if final_id:
        return dict(get_final(final_id).get("payload") or {})
    body = request.get_json(silent=True) or {}
    return dict(body.get("payload") or body or {})


@app.get("/api/model/vouchers")
def model_vouchers():
    try:
        payload = _payload_from_model_reference()
        result = build_financial_model(payload)
        return {"ok": True, "vouchers": result.accounting.get("vouchers", [])}
    except ModelStorageError as exc:
        return {"ok": False, "error": str(exc)}, 404
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400


@app.get("/api/model/vouchers/<voucher_id>")
def model_voucher_detail(voucher_id: str):
    try:
        payload = _payload_from_model_reference()
        result = build_financial_model(payload)
        voucher = next((item for item in result.accounting.get("vouchers", []) if item.get("voucher_id") == voucher_id), None)
        if not voucher:
            return {"ok": False, "error": "Comprobante no encontrado"}, 404
        return {"ok": True, "voucher": voucher}
    except ModelStorageError as exc:
        return {"ok": False, "error": str(exc)}, 404
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400


@app.post("/api/model/vouchers/<voucher_id>/reverse")
def model_voucher_reverse(voucher_id: str):
    try:
        payload = _payload_from_model_reference()
        result = build_financial_model(payload)
        voucher = next((item for item in result.accounting.get("vouchers", []) if item.get("voucher_id") == voucher_id), None)
        if not voucher:
            return {"ok": False, "error": "Comprobante no encontrado"}, 404
        reversal = reverse_voucher(voucher)
        adjusted_payload = dict(payload)
        accounting = dict(adjusted_payload.get("accounting") or {})
        vouchers = list(accounting.get("vouchers") or [])
        vouchers.append(reversal)
        accounting["vouchers"] = vouchers
        adjusted_payload["accounting"] = accounting
        return {"ok": True, "reversal": reversal, "adjusted_payload": adjusted_payload}
    except ModelStorageError as exc:
        return {"ok": False, "error": str(exc)}, 404
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400


@app.get("/api/model/accounts/<account>/ledger")
def model_account_ledger(account: str):
    try:
        payload = _payload_from_model_reference()
        result = build_financial_model(payload)
        return {"ok": True, "account": account, "ledger": get_account_ledger(result.accounting, account)}
    except ModelStorageError as exc:
        return {"ok": False, "error": str(exc)}, 404
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400


@app.get("/api/model/trace")
def model_trace():
    try:
        payload = _payload_from_model_reference()
        account = request.args.get("account") or ""
        month = request.args.get("month") or ""
        result = build_financial_model(payload)
        return {"ok": True, "trace": get_trace(result.accounting, account, month)}
    except ModelStorageError as exc:
        return {"ok": False, "error": str(exc)}, 404
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400


@app.post("/api/model/chat/preview")
def model_chat_preview():
    """Interpreta una instruccion de chat y devuelve una propuesta de ajuste."""
    try:
        body = request.get_json(silent=True) or {}
        payload = body.get("payload") or {}
        message = body.get("message") or ""
        scope = body.get("scope") or {}
        data = preview_chat_adjustment(payload, message, scope=scope)
        status = 200 if data.get("ok") else (422 if data.get("needs_clarification") or data.get("not_viable") else 400)
        return data, status
    except ModelChatError as exc:
        return {"ok": False, "error": str(exc)}, exc.status_code
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400


@app.post("/api/model/chat/command")
def model_chat_command():
    """Orquesta instrucciones del asistente contable para consultas, UI y propuestas."""
    try:
        _increment_legacy_chat_counter("/api/model/chat/command")
        body = request.get_json(silent=True) or {}
        payload = body.get("payload") or {}
        message = body.get("message") or ""
        ui_context = body.get("ui_context") or {}
        scope = body.get("scope") or ui_context.get("scope") or {}
        data = handle_chat_command(payload, message, ui_context=ui_context, scope=scope)
        status = 200 if data.get("ok") else (422 if data.get("needs_clarification") else 400)
        return data, status
    except ChatCommandError as exc:
        return {"ok": False, "assistant_message": str(exc), "error": str(exc)}, exc.status_code
    except Exception as exc:
        return {"ok": False, "assistant_message": f"{type(exc).__name__}: {exc}", "error": f"{type(exc).__name__}: {exc}"}, 400


@app.post("/api/model/documents/extract")
def model_documents_extract():
    """Extrae datos del cliente desde cedula y matricula para el flujo sin Excel."""
    prune_uploads()
    cedula_front = request.files.get("cedula_front")
    cedula_back = request.files.get("cedula_back")
    matricula = request.files.get("matricula")
    if not any([cedula_front, cedula_back, matricula]):
        return {"ok": False, "error": "Adjunte al menos una imagen de cedula o matricula."}, 400

    saved: list[str] = []
    try:
        ced_front_path = _save_upload(cedula_front, ".png") if cedula_front else None
        ced_back_path = _save_upload(cedula_back, ".png") if cedula_back else None
        mat_path = _save_upload(matricula, ".png") if matricula else None
        saved = [p for p in [ced_front_path, ced_back_path, mat_path] if p]
        data = extract_client_documents(
            cedula_front=ced_front_path,
            cedula_back=ced_back_path,
            matricula_path=mat_path,
        )
        status = 200 if data.get("ok") else 400
        return data, status
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400
    finally:
        for path in saved:
            _safe_unlink(path)


@app.post("/api/model/drafts")
def model_save_draft():
    try:
        body = request.get_json(silent=True) or {}
        payload = body.get("payload") or body
        draft_id = body.get("draft_id") or body.get("id")
        record = save_draft(payload, draft_id=draft_id)
        return {"ok": True, "record": record}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400


@app.get("/api/model/drafts")
def model_list_drafts():
    return {"ok": True, "records": list_drafts()}


@app.get("/api/model/drafts/<record_id>")
def model_get_draft(record_id: str):
    try:
        return {"ok": True, "record": get_draft(record_id)}
    except ModelStorageError as exc:
        return {"ok": False, "error": str(exc)}, 404


@app.delete("/api/model/drafts/<record_id>")
def model_delete_draft(record_id: str):
    try:
        delete_draft(record_id)
        return {"ok": True}
    except ModelStorageError as exc:
        return {"ok": False, "error": str(exc)}, 404


@app.get("/api/model/finals")
def model_list_finals():
    return {"ok": True, "records": list_finals()}


@app.get("/api/model/finals/<record_id>")
def model_get_final(record_id: str):
    try:
        return {"ok": True, "record": get_final(record_id)}
    except ModelStorageError as exc:
        return {"ok": False, "error": str(exc)}, 404


@app.post("/api/model/finals/<record_id>/duplicate")
def model_duplicate_final(record_id: str):
    try:
        return {"ok": True, "record": duplicate_final(record_id)}
    except ModelStorageError as exc:
        return {"ok": False, "error": str(exc)}, 404


@app.get("/api/model/finals/<record_id>/document")
def model_final_document(record_id: str):
    try:
        path, filename = final_document_path(record_id)
        return send_file(path, as_attachment=True, download_name=filename)
    except ModelStorageError as exc:
        return {"ok": False, "error": str(exc)}, 404


@app.post("/api/model/generate")
def model_generate_doc():
    """Genera el DOCX sin Excel, usando el modelo financiero interno."""
    try:
        payload = request.get_json(silent=True) or {}
        result = build_financial_model(payload)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400

    allow_errors = bool(payload.get("allow_errors", False))
    ok = (
        result.validations["er"].get("ok")
        and result.validations["esf"].get("ok")
        and result.validations["balance"].get("ok")
    )
    if not ok and not allow_errors:
        return {
            "ok": False,
            "error": "El modelo tiene errores de validacion",
            "validations": result_to_json(result).get("validations"),
        }, 422

    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
        out_path = tmp.name

    generar_documento_completo(
        result.df_esf_mensual,
        result.df_er,
        result.df_datos,
        result.df_certificacion,
        out_path,
        incluir_validacion=False,
        tolerancia_validacion=1.0,
        detener_si_error=False,
        validacion_documentos=None,
        validacion_llm=None,
        statement_blocks=result.statement_blocks if len(result.statement_blocks) > 1 else None,
        esf_tipo="mensual",
    )

    report = build_report(
        v_er=result.validations["er"],
        v_esf=result.validations["esf"],
        v_docs=None,
        v_llm=None,
        meta={"source": "app_model", **result.summary},
    )
    report_path = save_report_json(report, out_path)

    seed_label = str(result.summary.get("seed", "app"))
    safe_seed = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in seed_label)[:80]
    safe_filename = f"certificacion_modelo_{safe_seed or 'app'}.docx"

    @after_this_request
    def _remove_model_generated(response):
        try:
            Path(out_path).unlink(missing_ok=True)
            Path(report_path).unlink(missing_ok=True)
        except Exception:
            pass
        return response

    return send_file(out_path, as_attachment=True, download_name=safe_filename)


@app.post("/api/model/finals")
def model_save_final():
    """Genera el DOCX sin Excel y guarda una version final inmutable."""
    try:
        body = request.get_json(silent=True) or {}
        payload = body.get("payload") or body
        result = build_financial_model(payload)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400

    ok = (
        result.validations["er"].get("ok")
        and result.validations["esf"].get("ok")
        and result.validations["balance"].get("ok")
    )
    if not ok:
        return {
            "ok": False,
            "error": "El modelo tiene errores de validacion",
            "validations": result_to_json(result).get("validations"),
        }, 422

    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
        out_path = tmp.name

    try:
        generar_documento_completo(
            result.df_esf_mensual,
            result.df_er,
            result.df_datos,
            result.df_certificacion,
            out_path,
            incluir_validacion=False,
            tolerancia_validacion=1.0,
            detener_si_error=False,
            validacion_documentos=None,
            validacion_llm=None,
            statement_blocks=result.statement_blocks if len(result.statement_blocks) > 1 else None,
            esf_tipo="mensual",
        )
        result_json = result_to_json(result)
        seed_label = str(result.summary.get("seed", "app"))
        safe_seed = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in seed_label)[:80]
        safe_filename = f"certificacion_modelo_{safe_seed or 'app'}.docx"
        record = save_final(payload, result_json=result_json, document_path=out_path, filename=safe_filename)
        return {"ok": True, "record": record}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400
    finally:
        Path(out_path).unlink(missing_ok=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    debug = os.environ.get("CERTAPP_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)

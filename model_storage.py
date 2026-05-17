from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


BASE_DIR = Path(__file__).parent.resolve()
MODEL_STORE_DIR = Path(os.getenv("CERTAPP_MODEL_STORE_DIR", str(BASE_DIR / "data" / "models")))


class ModelStorageError(ValueError):
    pass


def save_draft(payload: Mapping[str, Any], *, draft_id: Optional[str] = None) -> Dict[str, Any]:
    payload = _clean_payload(payload)
    now = _utc_now()
    existing = find_record("drafts", draft_id) if draft_id else None
    record_id = draft_id or f"draft_{_stamp()}_{uuid.uuid4().hex[:8]}"
    meta = _metadata_from_payload(payload)
    client_slug = existing.get("client_slug") if existing else meta["client_slug"]
    record = {
        "id": record_id,
        "type": "draft",
        "status": "draft",
        "created_at": existing.get("created_at") if existing else now,
        "updated_at": now,
        **meta,
        "client_slug": client_slug,
        "payload": payload,
    }
    path = _record_path(client_slug, "drafts", record_id)
    _atomic_write_json(path, record)
    if existing and Path(existing["_path"]) != path:
        Path(existing["_path"]).unlink(missing_ok=True)
    return _public_record(record)


def list_drafts() -> list[Dict[str, Any]]:
    return _list_records("drafts")


def get_draft(record_id: str) -> Dict[str, Any]:
    record = find_record("drafts", record_id)
    if not record:
        raise ModelStorageError("Borrador no encontrado")
    return _public_record(record, include_payload=True)


def delete_draft(record_id: str) -> None:
    record = find_record("drafts", record_id)
    if not record:
        raise ModelStorageError("Borrador no encontrado")
    Path(record["_path"]).unlink(missing_ok=True)


def save_final(
    payload: Mapping[str, Any],
    *,
    result_json: Mapping[str, Any],
    document_path: str,
    filename: str,
) -> Dict[str, Any]:
    payload = _clean_payload(payload)
    now = _utc_now()
    record_id = f"final_{_stamp()}_{uuid.uuid4().hex[:8]}"
    meta = _metadata_from_payload(payload)
    client_slug = meta["client_slug"]
    doc_name = f"{record_id}.docx"
    doc_dest = _record_dir(client_slug, "documents") / doc_name
    doc_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(document_path, doc_dest)
    record = {
        "id": record_id,
        "type": "final",
        "status": "final",
        "created_at": now,
        "updated_at": now,
        **meta,
        "payload": payload,
        "summary": result_json.get("summary") or {},
        "full_summary": result_json.get("full_summary") or {},
        "period_blocks": result_json.get("period_blocks") or [],
        "validations": result_json.get("validations") or {},
        "document": {
            "filename": filename,
            "stored_filename": doc_name,
            "path": str(doc_dest),
        },
    }
    _atomic_write_json(_record_path(client_slug, "finals", record_id), record)
    return _public_record(record)


def list_finals() -> list[Dict[str, Any]]:
    return _list_records("finals")


def get_final(record_id: str) -> Dict[str, Any]:
    record = find_record("finals", record_id)
    if not record:
        raise ModelStorageError("Historico no encontrado")
    return _public_record(record, include_payload=True, include_result=True)


def duplicate_final(record_id: str) -> Dict[str, Any]:
    final = get_final(record_id)
    payload = final.get("payload") or {}
    return save_draft(payload)


def final_document_path(record_id: str) -> tuple[Path, str]:
    record = find_record("finals", record_id)
    if not record:
        raise ModelStorageError("Historico no encontrado")
    doc = record.get("document") or {}
    path = Path(str(doc.get("path") or ""))
    if not path.exists():
        raise ModelStorageError("Documento final no encontrado")
    return path, str(doc.get("filename") or path.name)


def find_record(kind: str, record_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not record_id:
        return None
    for path in _iter_record_paths(kind):
        if path.stem != record_id:
            continue
        record = _read_json(path)
        record["_path"] = str(path)
        return record
    return None


def _list_records(kind: str) -> list[Dict[str, Any]]:
    records = []
    for path in _iter_record_paths(kind):
        try:
            record = _read_json(path)
            record["_path"] = str(path)
            records.append(_public_record(record))
        except Exception:
            continue
    records.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    return records


def _iter_record_paths(kind: str) -> Iterable[Path]:
    if not MODEL_STORE_DIR.exists():
        return []
    return MODEL_STORE_DIR.glob(f"*/{kind}/*.json")


def _record_dir(client_slug: str, kind: str) -> Path:
    return MODEL_STORE_DIR / client_slug / kind


def _record_path(client_slug: str, kind: str, record_id: str) -> Path:
    return _record_dir(client_slug, kind) / f"{record_id}.json"


def _metadata_from_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    client = dict(payload.get("client") or {})
    period = dict(payload.get("period") or {})
    name = str(client.get("nombre_completo") or client.get("name") or "cliente").strip() or "cliente"
    cedula = str(client.get("cedula") or "").strip()
    banco = str(client.get("banco") or "").strip()
    start_month = str(period.get("start_month") or period.get("mes_inicio") or "").strip()[:7]
    end_month = str(period.get("end_month") or period.get("mes_final") or "").strip()[:7]
    client_slug = _slugify(f"{name}-{cedula}" if cedula else name)
    return {
        "client_slug": client_slug,
        "client_name": name,
        "cedula": cedula,
        "bank": banco,
        "start_month": start_month,
        "end_month": end_month,
        "period_label": _period_label(start_month, end_month),
    }


def _period_label(start_month: str, end_month: str) -> str:
    if start_month and end_month:
        return f"{start_month} a {end_month}"
    return start_month or end_month or ""


def _clean_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(dict(payload or {}), ensure_ascii=False, default=str))


def _public_record(
    record: Mapping[str, Any],
    *,
    include_payload: bool = False,
    include_result: bool = False,
) -> Dict[str, Any]:
    keys = [
        "id",
        "type",
        "status",
        "created_at",
        "updated_at",
        "client_slug",
        "client_name",
        "cedula",
        "bank",
        "start_month",
        "end_month",
        "period_label",
        "document",
    ]
    out = {key: record.get(key) for key in keys if key in record}
    if include_payload:
        out["payload"] = record.get("payload") or {}
    if include_result:
        out["summary"] = record.get("summary") or {}
        out["full_summary"] = record.get("full_summary") or {}
        out["period_blocks"] = record.get("period_blocks") or []
        out["validations"] = record.get("validations") or {}
    return out


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:80] or "cliente"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

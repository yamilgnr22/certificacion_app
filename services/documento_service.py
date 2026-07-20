"""Documentos soporte del cliente (imagenes de cedula/matricula).

Biblioteca POR CLIENTE: las imagenes se suben una vez y se acumulan
(cedula, matricula 2024, 2025, ...). Cada certificacion elige por id
cuales incluir en su DOCX (documentos_ids en los inputs guardados).

Persistencia:
  - Archivo fisico en data/soportes/<cliente_id>/<uuid>_<nombre>
    (override con CERTAPP_SOPORTES_DIR, para tests).
  - Fila en documentos_soporte (tabla existente desde la migracion 001).
  - Auditoria en cada subida/eliminacion.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import DocumentoSoporte
from repositories import ClienteRepository
from services.audit_service import AuditService
from services.periodo_service import PeriodoNotFoundError, PeriodoValidationError
from services.serializers import iso


# 'matricula' se mantiene por compatibilidad con documentos ya subidos; las
# certificaciones suelen llevar al menos 3 matriculas (una por anio), cada
# una con su soporte (constancia de tramite de Alcaldia).
TIPOS_VALIDOS = {
    "cedula_front", "cedula_back",
    "matricula", "matricula_1", "matricula_2", "matricula_3",
    "soporte_1", "soporte_2", "soporte_3",
    "foto_negocio",  # fotos del local: van a la hoja opcional "Fotografías del Negocio"
    "otro",
}
_EXTENSIONES_IMAGEN = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _soportes_dir() -> Path:
    base = os.getenv("CERTAPP_SOPORTES_DIR")
    if base:
        return Path(base)
    return Path(__file__).resolve().parents[1] / "data" / "soportes"


def _doc_to_dict(doc: DocumentoSoporte) -> dict[str, Any]:
    return {
        "id": doc.id,
        "cliente_id": doc.cliente_id,
        "tipo": doc.tipo,
        "original_filename": doc.original_filename,
        "created_at": iso(doc.created_at),
    }


class DocumentoService:
    def __init__(self, session: Session):
        self.session = session
        self.clientes = ClienteRepository(session)
        self.audit = AuditService(session)

    def subir(self, cliente_id: str, file_storage, tipo: str, *, cpa_user: str = "system") -> dict:
        cliente = self.clientes.get(cliente_id)
        if not cliente or not cliente.activo:
            raise PeriodoNotFoundError("Cliente no encontrado o inactivo")
        tipo = (tipo or "otro").strip().lower()
        if tipo not in TIPOS_VALIDOS:
            raise PeriodoValidationError(
                f"Tipo de documento invalido '{tipo}'. Validos: {sorted(TIPOS_VALIDOS)}"
            )
        if not file_storage or not file_storage.filename:
            raise PeriodoValidationError("Adjunte una imagen")
        ext = Path(file_storage.filename).suffix.lower()
        if ext not in _EXTENSIONES_IMAGEN:
            raise PeriodoValidationError(
                f"Extension no soportada '{ext}'. Usa una imagen: {sorted(_EXTENSIONES_IMAGEN)}"
            )

        out_dir = _soportes_dir() / cliente_id
        out_dir.mkdir(parents=True, exist_ok=True)
        nombre = f"{uuid.uuid4().hex}_{Path(file_storage.filename).name}"
        path = out_dir / nombre
        file_storage.save(str(path))

        try:
            doc = DocumentoSoporte(
                cliente_id=cliente_id,
                tipo=tipo,
                original_filename=file_storage.filename,
                path=str(path),
            )
            self.session.add(doc)
            self.session.flush()
            data = _doc_to_dict(doc)
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="documento",
                entity_id=doc.id,
                action="upload",
                summary=f"Subio documento {tipo} '{file_storage.filename}' de {cliente.nombre_completo}",
                after=data,
                metadata={"cliente_id": cliente_id, "path": str(path)},
            )
            self.session.commit()
            return data
        except Exception:
            self.session.rollback()
            path.unlink(missing_ok=True)
            raise

    def listar(self, cliente_id: str) -> list[dict]:
        stmt = (
            select(DocumentoSoporte)
            .where(DocumentoSoporte.cliente_id == cliente_id)
            .order_by(DocumentoSoporte.created_at)
        )
        return [_doc_to_dict(d) for d in self.session.scalars(stmt)]

    def ruta(self, doc_id: str) -> tuple[str, str] | None:
        """(path_absoluto, nombre_original) si el archivo existe, sino None."""
        doc = self.session.get(DocumentoSoporte, doc_id)
        if not doc:
            return None
        p = Path(doc.path)
        if not p.exists():
            return None
        return str(p), (doc.original_filename or p.name)

    def rutas_para(self, doc_ids: list[str]) -> list[str]:
        """Rutas existentes para los ids dados, preservando el orden pedido.

        Ids desconocidos o con archivo faltante se omiten en silencio: una
        certificacion vieja no debe romperse porque se borro una imagen."""
        rutas: list[str] = []
        for doc_id in doc_ids or []:
            r = self.ruta(str(doc_id))
            if r:
                rutas.append(r[0])
        return rutas

    def soportes_para(self, doc_ids: list[str]) -> list[dict]:
        """[{tipo, path}] existentes para los ids dados, en el orden pedido.

        Misma tolerancia que rutas_para: ids desconocidos o archivos
        faltantes se omiten en silencio. El tipo permite al generador
        emparejar cedula front/back y matricula_N con soporte_N."""
        out: list[dict] = []
        for doc_id in doc_ids or []:
            doc = self.session.get(DocumentoSoporte, str(doc_id))
            if not doc:
                continue
            p = Path(doc.path)
            if not p.exists():
                continue
            out.append({"tipo": doc.tipo, "path": str(p)})
        return out

    def eliminar(self, doc_id: str, *, cpa_user: str = "system") -> bool:
        doc = self.session.get(DocumentoSoporte, doc_id)
        if not doc:
            return False
        before = _doc_to_dict(doc)
        path = Path(doc.path)
        try:
            self.audit.log(
                cpa_user=cpa_user,
                entity_type="documento",
                entity_id=doc.id,
                action="delete",
                summary=f"Elimino documento {doc.tipo} '{doc.original_filename}'",
                before=before,
                metadata={"cliente_id": doc.cliente_id},
            )
            self.session.delete(doc)
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        # Best-effort: en Windows el archivo puede estar bloqueado por una
        # descarga en curso; el registro ya se borro (que es lo que manda) y
        # el archivo huerfano no afecta (no esta referenciado).
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return True

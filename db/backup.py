"""Respaldo best-effort de la base SQLite antes de operaciones importantes.

Las certificaciones finalizadas son el activo real de la app; antes de cada
Finalizar se deja una copia consistente de la DB (API de backup de sqlite3,
segura aunque haya conexiones abiertas) en `<carpeta de la DB>/backups/`
(override con CERTAPP_BACKUPS_DIR). Se conservan los ultimos RETENCION.

Nunca lanza: si el respaldo falla se registra un warning y la operacion
principal sigue (el backup protege, no bloquea).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

RETENCION = 20


def backup_sqlite(engine, *, motivo: str = "") -> Path | None:
    """Copia la DB del engine y devuelve la ruta del respaldo (o None).

    No-op silencioso para bases en memoria o dialectos no-sqlite (tests)."""
    try:
        url = engine.url
        if url.get_backend_name() != "sqlite":
            return None
        db_path = url.database
        if not db_path or db_path == ":memory:":
            return None
        src = Path(db_path)
        if not src.exists():
            return None

        out_dir = Path(os.getenv("CERTAPP_BACKUPS_DIR") or (src.parent / "backups"))
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        sufijo = f"_{motivo}" if motivo else ""
        dest = out_dir / f"{src.stem}_{stamp}{sufijo}.db"

        con_src = sqlite3.connect(str(src))
        try:
            con_dst = sqlite3.connect(str(dest))
            try:
                con_src.backup(con_dst)
            finally:
                con_dst.close()
        finally:
            con_src.close()

        _podar(out_dir, src.stem)
        logger.info("Respaldo de DB creado: %s", dest)
        return dest
    except Exception:
        logger.warning("No se pudo respaldar la DB antes de la operacion", exc_info=True)
        return None


def _podar(out_dir: Path, stem: str, keep: int = RETENCION) -> None:
    """Conserva solo los `keep` respaldos mas recientes de esta DB."""
    respaldos = sorted(out_dir.glob(f"{stem}_*.db"), key=lambda p: p.stat().st_mtime)
    if len(respaldos) <= keep:
        return
    for p in respaldos[: len(respaldos) - keep]:
        try:
            p.unlink()
        except OSError:
            pass

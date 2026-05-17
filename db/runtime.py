from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


class DatabaseNotInitialized(RuntimeError):
    pass


class DatabaseOutOfDate(RuntimeError):
    pass


def has_alembic_version(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
            return bool(row and row[0])
    except SQLAlchemyError:
        return False


def current_alembic_version(engine: Engine) -> str | None:
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
            return row[0] if row else None
    except SQLAlchemyError:
        return None


def latest_migration_id() -> str | None:
    """Lee el id de la migration mas reciente segun los archivos en disco."""
    versions_dir = Path(__file__).resolve().parent / "migrations" / "versions"
    if not versions_dir.is_dir():
        return None
    ids: list[str] = []
    for p in versions_dir.glob("*.py"):
        if p.name.startswith("_"):
            continue
        ids.append(p.stem)
    if not ids:
        return None
    # Convencion: nombres ordenables alfabeticamente (001_, 002_, 003_, ...)
    return sorted(ids)[-1]


def require_alembic_version(engine: Engine) -> None:
    if not has_alembic_version(engine):
        raise DatabaseNotInitialized("Base de datos no inicializada. Ejecuta: alembic upgrade head")
    current = current_alembic_version(engine)
    latest = latest_migration_id()
    if current and latest and current != latest:
        raise DatabaseOutOfDate(
            f"Base de datos desactualizada (revision {current}, esperada {latest}). "
            "Ejecuta: alembic upgrade head"
        )

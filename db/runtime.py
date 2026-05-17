from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


class DatabaseNotInitialized(RuntimeError):
    pass


def has_alembic_version(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
            return bool(row and row[0])
    except SQLAlchemyError:
        return False


def require_alembic_version(engine: Engine) -> None:
    if not has_alembic_version(engine):
        raise DatabaseNotInitialized("Base de datos no inicializada. Ejecuta: alembic upgrade head")

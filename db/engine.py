from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "certificacion_app.db"


def database_url() -> str:
    configured = os.getenv("CERTAPP_DATABASE_URL", "").strip()
    if configured:
        return configured
    return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"


def get_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    url = url or database_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, echo=echo, future=True, connect_args=connect_args)


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Devuelve un sessionmaker. Llamalo y despues llamalo de nuevo para obtener una Session.

    Uso: `session = session_factory(engine)()` o `with session_scope(engine) as session: ...`.
    """
    return sessionmaker(bind=engine or get_engine(), autoflush=False, expire_on_commit=False, future=True)


# Alias retro-compatible con DeprecationWarning. Se elimina en una fase posterior.
def get_session(engine: Engine | None = None) -> sessionmaker[Session]:
    import warnings
    warnings.warn(
        "get_session() esta deprecado, use session_factory() en su lugar.",
        DeprecationWarning,
        stacklevel=2,
    )
    return session_factory(engine)


def init_db(engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    if str(engine.url).startswith("sqlite"):
        db_path = Path(engine.url.database or "")
        if db_path and str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    factory = get_session(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

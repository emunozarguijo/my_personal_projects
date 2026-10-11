"""Engine, sesiones e inicialización del esquema."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from app.core.models import Base

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, settings: Settings):
        # prepare_threshold=None: sin prepared statements (necesario con poolers tipo PgBouncer, como el de Neon)
        engine = create_engine(
            settings.sqlalchemy_url, pool_pre_ping=True, future=True, connect_args={"prepare_threshold": None}
        )
        if settings.db_schema:
            # Todas las tablas del ORM se crean y consultan en este esquema, sin depender de search_path
            # (que el pooler no conserva entre transacciones).
            engine = engine.execution_options(schema_translate_map={None: settings.db_schema})
        self.engine = engine
        self._schema = settings.db_schema
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def init_schema(self) -> None:
        self._ensure_vector_extension()
        with self.engine.begin() as conn:
            Base.metadata.create_all(conn)
        logger.info("Esquema de base de datos listo (schema=%s)", self._schema or "por defecto")

    def _ensure_vector_extension(self) -> None:
        with self.engine.connect() as conn:
            if conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar():
                return
        try:  # en su propia transacción: si falla por permisos no deja nada a medias
            with self.engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:
            raise ConfigurationError(
                "La extensión pgvector no está instalada en esta base de datos y tu usuario no pudo crearla. "
                "Pide que ejecuten 'CREATE EXTENSION vector;' (en Neon también se activa desde la consola SQL)."
            ) from exc

    def ping(self) -> bool:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

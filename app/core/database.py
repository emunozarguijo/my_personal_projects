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
        self._ensure_schema_exists()
        with self.engine.begin() as conn:
            Base.metadata.create_all(conn)
        logger.info("Esquema de base de datos listo (schema=%s)", self._schema or "por defecto")

    def _ensure_schema_exists(self) -> None:
        """Falla con un mensaje claro (a qué BD se conectó y qué esquemas ve) si DB_SCHEMA no existe."""
        if not self._schema:
            return
        with self.engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": self._schema}
            ).scalar()
            if exists:
                return
            db, user, server = conn.execute(
                text("SELECT current_database(), current_user, inet_server_addr()")
            ).one()
            schemas = conn.execute(
                text(
                    "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
                    "AND nspname <> 'information_schema' ORDER BY 1"
                )
            ).scalars().all()
        raise ConfigurationError(
            f"El esquema {self._schema!r} no existe en la BD a la que se conectó la app "
            f"(database={db!r}, user={user!r}, server={server}, host={self.engine.url.host!r}). "
            f"Esquemas visibles: {', '.join(schemas)}. Revisa DB_HOST / DB_NAME / DB_SCHEMA "
            "(o DATABASE_URL, que tiene prioridad)."
        )

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

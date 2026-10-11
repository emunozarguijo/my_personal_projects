"""Entorno de Alembic: usa la misma configuración que la app y versiona dentro de DB_SCHEMA."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

config = context.config
settings = config.attributes.get("settings") or get_settings()
config.attributes["settings"] = settings
config.attributes["schema"] = settings.db_schema


def run_migrations_online() -> None:
    engine = create_engine(
        settings.sqlalchemy_url, poolclass=NullPool, connect_args={"prepare_threshold": None}
    )
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            version_table_schema=settings.db_schema,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Las migraciones offline no están soportadas; usa python -m app.migrate")
run_migrations_online()

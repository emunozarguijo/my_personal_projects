"""Aplica las migraciones pendientes:  python -m app.migrate

Si la BD ya tenía las tablas de antes de usar Alembic, primero las marca como la revisión base (stamp) y
después aplica el resto. Se ejecuta en Render antes de levantar uvicorn.
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.core.config import Settings, get_settings
from app.core.database import Database
from app.core.exceptions import AppError

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "0001_baseline"


def alembic_config(settings: Settings) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["settings"] = settings
    return cfg


def head_revision(settings: Settings) -> str:
    return ScriptDirectory.from_config(alembic_config(settings)).get_current_head()


def upgrade(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    db = Database(settings)
    db.ensure_schema_exists()
    db.ensure_vector_extension()
    with db.engine.connect() as conn:
        insp = inspect(conn)
        versioned = insp.has_table("alembic_version", schema=settings.db_schema)
        legacy = insp.has_table("statements", schema=settings.db_schema)
    cfg = alembic_config(settings)
    if legacy and not versioned:
        print(f"Tablas previas a Alembic detectadas: se marcan como {BASELINE}.")
        command.stamp(cfg, BASELINE)
    command.upgrade(cfg, "head")
    print(f"Base de datos al día (revisión {head_revision(settings)}).")


def main() -> int:
    try:
        upgrade()
    except AppError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
